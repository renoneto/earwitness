"""READ-PP: each member's alert read state is their own.

Two judges (editors A and B), the owner and a viewer share one workspace
with one alert that has 3 new, unread matches (3 ads). One judge's "Mark
all read" (or a read of one match) clears New for that judge only: the
others still see 3 New on Overview, the Alerts list and the alert page,
and each caller's counts agree on all three. The shared column
(customer_alert_events.read_state) is "read for everyone": the local
operator, and marks made before READ-PP, still write and show it for
every member. Each member's marks are the event ids of their own
customer_alert_read audit rows (no new table; design: launch-readiness
ws-app/READ-PP-DESIGN.md).

Run: PYTHONPATH=. python -m unittest tests.test_read_pp
"""
from __future__ import annotations

import inspect
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import access, collaboration, customer, customer_query, db, services
from earwitness.app import asgi, search

from tests import test_uat_dec_d1api as d1
from tests.test_read_cache import MEMBER, ReadCacheBase, fresh
from tests.test_uat_d1 import _republish


class ReadPPBase(ReadCacheBase):
    """ReadCacheBase's acme alert with 3 new, unread matches in 3 ads."""

    def setUp(self):
        super().setUp()
        d1._evidence(self.con, "r9", "e1", 150.0, 170.0, self.tv["cur"], "acme")
        d1._evidence(self.con, "r10", "e2", 60.0, 80.0, self.tv["tv2"], "acme")
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        self.everyone = (self.A, self.B, self.owner, self.viewer)

    def page(self, ctx):
        return services.customer_alert(ctx, self.con, MEMBER, self.aid)["alert"]

    def view(self, ctx):
        """What ctx sees of the alert on Overview, the Alerts list and the
        alert page: (overview unread, unread_ads, new_unread_ads), (list
        unread events, unread ads), (page unread events, unread ads), the
        page's event order and read marks, and Overview's recent events."""
        over = services.customer_overview(ctx, self.con, MEMBER)
        total = next(t for t in over["alert_totals"] if t["alert_id"] == self.aid)
        listed = next(a for a in services.customer_alerts(ctx, self.con, MEMBER)["alerts"]
                      if a["id"] == self.aid)
        page = self.page(ctx)
        return {"overview": (total["unread"], total["unread_ads"], total["new_unread_ads"]),
                "alerts": (listed["unread_event_count"], listed["unread_ad_count"]),
                "page": (page["unread_event_count"], page["unread_ad_count"]),
                "order": [e["id"] for e in customer._alert_page_order(page["events"])],
                "marks": {e["id"]: e["read_state"] for e in page["events"]},
                "list_marks": {e["id"]: e["read_state"] for e in listed["events"]},
                "recent": {e["id"]: e["read_state"] for e in over["recent_events"]
                           if e["alert_id"] == self.aid},
                "listing": [(e["id"], e["read_state"]) for e in
                            (total.get("listing") or {}).get("events", [])]}

    def mark_all(self, ctx):
        """The browser's Mark all read: the caller's own unread events."""
        full = services.customer_alert(ctx, self.con, MEMBER, self.aid, summaries=False)["alert"]
        unread = [e["id"] for e in full["events"] if not e["read_state"]]
        out = services.customer_alert_read(ctx, self.con, MEMBER, self.aid, unread)
        self.con.commit()
        return out

    def shared_marks(self):
        return [r[0] for r in self.con.execute(
            "SELECT read_state FROM customer_alert_events WHERE alert_id=? ORDER BY id",
            (self.aid,))]

    def assertNew(self, view, n, label):
        self.assertEqual(view["overview"], (n, n, n), label)
        self.assertEqual(view["alerts"], (n, n), label)
        self.assertEqual(view["page"], (n, n), label)

    def assertCountsAgree(self, view, label):
        """Overview's "N unread" (new_unread_ads), the Alerts list's "N new"
        (unread_ad_count) and the alert page's unread count agree."""
        self.assertEqual(view["overview"][2], view["alerts"][1], label)
        self.assertEqual(view["alerts"], view["page"], label)


class MarkAllReadTest(ReadPPBase):

    def test_everyone_starts_with_3_new(self):
        for ctx in self.everyone:
            v = self.view(ctx)
            self.assertNew(v, 3, ctx.principal_id)
            self.assertEqual(set(v["marks"].values()), {0}, ctx.principal_id)

    def test_mark_all_read_clears_new_for_the_caller_only(self):
        out = self.mark_all(self.A)
        self.assertEqual(out["marked"], 3)
        a = self.view(self.A)
        self.assertNew(a, 0, "A after marking")
        self.assertEqual(set(a["marks"].values()), {1})
        self.assertEqual(set(a["list_marks"].values()), {1})
        self.assertEqual(set(a["recent"].values()), {1})
        self.assertEqual({m for _i, m in a["listing"]}, {1})
        for ctx in (self.B, self.owner, self.viewer):
            v = self.view(ctx)
            self.assertNew(v, 3, ctx.principal_id)
            self.assertEqual(set(v["marks"].values()), {0}, ctx.principal_id)
            self.assertEqual(set(v["list_marks"].values()), {0}, ctx.principal_id)
            self.assertEqual(set(v["recent"].values()), {0}, ctx.principal_id)
            self.assertEqual({m for _i, m in v["listing"]}, {0}, ctx.principal_id)
            self.assertCountsAgree(v, ctx.principal_id)
        self.assertCountsAgree(a, "A")
        # The shared column is untouched: it is "read for everyone".
        self.assertEqual(self.shared_marks(), [0, 0, 0])

    def test_b_marks_later_and_a_is_unchanged(self):
        self.mark_all(self.A)
        self.mark_all(self.B)
        self.assertNew(self.view(self.B), 0, "B")
        self.assertNew(self.view(self.A), 0, "A")
        self.assertNew(self.view(self.owner), 3, "owner")

    def test_a_read_of_one_match_is_the_callers_own_and_orders_their_page(self):
        before = self.view(self.A)
        first = before["order"][0]
        services.customer_alert_read(self.A, self.con, MEMBER, self.aid, [first])
        self.con.commit()
        a, b = self.view(self.A), self.view(self.B)
        self.assertEqual(a["page"], (2, 2))
        self.assertEqual(a["overview"], (2, 2, 2))
        self.assertEqual(a["alerts"], (2, 2))
        self.assertEqual(a["marks"][first], 1)
        # Unread first: the read one moves to the end of A's page only.
        self.assertEqual(a["order"][-1], first)
        self.assertEqual(b["order"], before["order"])
        self.assertNew(b, 3, "B")

    def test_the_audit_row_records_the_callers_event_ids(self):
        self.mark_all(self.A)
        rows = self.con.execute(
            "SELECT actor, target_id, detail FROM audit WHERE action='customer_alert_read'"
            " ORDER BY id").fetchall()
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail"])
        self.assertEqual((rows[0]["actor"], rows[0]["target_id"]),
                         (self.A.principal_id, str(self.aid)))
        self.assertEqual(sorted(detail["event_ids"]), sorted(self.view(self.B)["marks"]))
        self.assertEqual(detail["marked"], 3)

    def test_another_alerts_events_are_not_marked(self):
        other = customer.save_alert(self.con, "beta watch",
                                    {"version": 2, "mode": "ads", "brands": ["beta"]})["id"]
        self.con.commit()
        d1._evidence(self.con, "r11", "e1", 180.0, 190.0, self.tv["cur"], "beta")
        _republish(self.con)
        customer.check_alert(self.con, other)
        self.con.commit()
        theirs = [e["id"] for e in customer.get_alert(self.con, other)["events"]]
        self.assertTrue(theirs)
        out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid, theirs)
        self.con.commit()
        self.assertEqual(out["marked"], 0)
        listed = {a["id"]: a for a in services.customer_alerts(self.A, self.con, MEMBER)["alerts"]}
        self.assertEqual(listed[other]["unread_event_count"], len(theirs))
        self.assertNew(self.view(self.A), 3, "A")

    def test_a_viewer_still_cannot_mark_read(self):
        with self.assertRaises(access.AuthorizationError):
            services.customer_alert_read(self.viewer, self.con, MEMBER, self.aid,
                                         list(self.view(self.viewer)["marks"]))
        self.assertNew(self.view(self.viewer), 3, "viewer")


class SharedMarksTest(ReadPPBase):

    def test_the_local_operator_marks_for_everyone(self):
        ids = list(self.view(self.A)["marks"])
        customer.mark_alert_events_read(self.con, self.aid, ids)
        self.con.commit()
        self.assertEqual(self.shared_marks(), [1, 1, 1])
        for ctx in self.everyone:
            self.assertNew(self.view(ctx), 0, ctx.principal_id)
        self.assertEqual(customer.get_alert(self.con, self.aid)["unread_event_count"], 0)

    def test_without_a_member_a_hosted_read_stays_shared(self):
        ids = list(self.view(self.A)["marks"])
        customer.mark_alert_events_read(self.con, self.aid, ids[:1], "hosted", member=None)
        self.con.commit()
        self.assertEqual(self.shared_marks()[0], 1)
        self.assertNew(self.view(self.B), 2, "B")

    def test_marks_made_before_read_pp_stay_read_for_everyone(self):
        """The upgrade: an event read before the switch (shared read_state,
        an audit row with no event_ids) stays read for every member; an
        event that arrives after it starts unread for each member."""
        first = min(self.view(self.A)["marks"])
        self.con.execute("UPDATE customer_alert_events SET read_state=1 WHERE id=?", (first,))
        db.audit(self.con, self.A.principal_id, "customer_alert_read", "customer_alert",
                 str(self.aid), {"marked": 1}, db.bump_revision(self.con))
        self.con.commit()
        for ctx in self.everyone:
            v = self.view(ctx)
            self.assertNew(v, 2, ctx.principal_id)
            self.assertEqual(v["marks"][first], 1, ctx.principal_id)
        d1._evidence(self.con, "r12", "e2", 120.0, 140.0, self.tv["tv2"], "acme")
        _republish(self.con)
        customer.check_alert(self.con, self.aid)
        self.con.commit()
        newest = max(self.view(self.A)["marks"])
        for ctx in self.everyone:
            v = self.view(ctx)
            self.assertNew(v, 3, ctx.principal_id)
            self.assertEqual(v["marks"][newest], 0, ctx.principal_id)
        self.mark_all(self.B)
        self.assertNew(self.view(self.B), 0, "B")
        self.assertNew(self.view(self.A), 3, "A")

    def test_workspace_wide_is_a_server_only_keyword(self):
        param = inspect.signature(services.customer_alert_read).parameters["workspace_wide"]
        self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(param.default, False)
        ids = list(self.view(self.A)["marks"])
        services.customer_alert_read(self.A, self.con, MEMBER, self.aid, ids, workspace_wide=True)
        self.con.commit()
        self.assertEqual(self.shared_marks(), [1, 1, 1])
        for ctx in self.everyone:
            self.assertNew(self.view(ctx), 0, ctx.principal_id)


class PerCallerCacheTest(ReadPPBase):
    """The READ-CACHE class bodies hold shared marks only; each caller's own
    marks are laid over them after every lookup, so no member is served
    another's marks, cached or fresh, in either order."""

    def everything(self, ctx):
        return {"overview": services.customer_overview(ctx, self.con, MEMBER),
                "alerts": services.customer_alerts(ctx, self.con, MEMBER),
                "alert": services.customer_alert(ctx, self.con, MEMBER, self.aid),
                "light": services.customer_alert(ctx, self.con, MEMBER, self.aid,
                                                 summaries=False)}

    def assertCachedEqualsFreshAll(self, label):
        for ctx in self.everyone:
            cached = self.everything(ctx)
            again = self.everything(ctx)
            with fresh():
                plain = self.everything(ctx)
            for name in cached:
                self.assertEqual(cached[name], plain[name], f"{label}: {name} {ctx.principal_id}")
                self.assertEqual(again[name], plain[name], f"{label}: {name} warm")

    def test_cached_equals_fresh_for_every_caller_before_and_after_a_mark(self):
        self.assertCachedEqualsFreshAll("before")
        self.mark_all(self.A)
        self.assertCachedEqualsFreshAll("after A")
        services.customer_alert_read(self.B, self.con, MEMBER, self.aid,
                                     [min(self.view(self.B)["marks"])])
        self.con.commit()
        self.assertCachedEqualsFreshAll("after B's one")

    def test_no_cross_member_reuse_in_either_order(self):
        self.mark_all(self.A)
        for order in ((self.A, self.B), (self.B, self.A)):
            customer_query._VIEW_MEMO.clear()
            seen = {ctx.principal_id: self.view(ctx) for ctx in order}
            self.assertNew(seen[self.A.principal_id], 0, f"A in {order[0].principal_id} first")
            self.assertNew(seen[self.B.principal_id], 3, f"B in {order[0].principal_id} first")

    def test_the_callers_own_alert_page_is_not_stale_after_their_mark(self):
        self.assertNew(self.view(self.A), 3, "A warm")
        self.mark_all(self.A)
        self.assertNew(self.view(self.A), 0, "A after")
        with fresh():
            self.assertNew(self.view(self.A), 0, "A fresh")

    def test_class_bodies_name_no_member(self):
        self.mark_all(self.A)
        for ctx in self.everyone:
            blob = json.dumps(self.everything(ctx), sort_keys=True, default=str)
            for other in self.everyone:
                self.assertNotIn(other.principal_id, blob)


class MembershipTest(ReadPPBase):

    def test_a_removed_then_re_added_member_keeps_their_own_marks(self):
        self.mark_all(self.A)
        collaboration.revoke_member(self.con, "local", self.A.principal_id,
                                    self.owner.principal_id)
        self.con.commit()
        with self.assertRaises(access.AuthorizationError):
            services.customer_alert_read(self.A, self.con, MEMBER, self.aid,
                                         list(self.view(self.B)["marks"]))
        self.assertNew(self.view(self.B), 3, "B while A is removed")
        self.con.execute("UPDATE memberships SET revoked=0 WHERE member=?", (self.A.principal_id,))
        self.con.commit()
        self.assertNew(self.view(self.A), 0, "A re-added")
        self.assertNew(self.view(self.B), 3, "B")



class OwnMarksTest(ReadPPBase):
    """Code r1 gap probes (RPP-R1-01/02/03), adopted."""

    def overview_row(self, ctx):
        over = services.customer_overview(ctx, self.con, MEMBER)
        t = next(t for t in over["alert_totals"] if t["alert_id"] == self.aid)
        return {"new_unread_ads": t["new_unread_ads"], "unread": t["unread"],
                "listing": [(e["id"], e["read_state"]) for e in
                            (t.get("listing") or {}).get("events", [])]}

    def test_two_successive_own_marks_keep_overview_listing_fresh(self):
        """RPP-R1-01: A reads one match, then a second; Overview's
        first-listed row (remembered per caller) equals a fresh one each time."""
        order = [e["id"] for e in customer._alert_page_order(self.page(self.A)["events"])]
        for n, eid in enumerate(order[:2], 1):
            self.overview_row(self.A)  # warm A's per-caller listing memo
            services.customer_alert_read(self.A, self.con, MEMBER, self.aid, [eid])
            self.con.commit()
            cached = self.overview_row(self.A)
            with fresh():
                plain = self.overview_row(self.A)
            self.assertEqual(cached, plain, f"after A's mark {n}")

    def test_unread_past_matches_are_not_new_for_a_member(self):
        """RPP-R1-02: with an unread past match (history imported, not
        marked), a member's new_unread_ads counts new matches only, as the
        shared path does (DEMO2-R5-N1)."""
        self.con.execute("UPDATE customer_alert_events SET historical=1 WHERE id=(SELECT MAX(id)"
                         " FROM customer_alert_events WHERE alert_id=?)", (self.aid,))
        db.bump_revision(self.con)
        self.con.commit()
        self.assertEqual(self.overview_row(self.A)["new_unread_ads"], 2)
        new_ids = [e["id"] for e in self.page(self.A)["events"] if not e["historical"]]
        services.customer_alert_read(self.A, self.con, MEMBER, self.aid, new_ids[:1])
        self.con.commit()
        self.assertEqual(self.overview_row(self.A)["new_unread_ads"], 1)

    def test_a_repeat_mark_records_no_ids(self):
        """RPP-R1-03: each event id is recorded at most once per member and
        alert, so a replayed Mark all read adds no ids."""
        ids = [e["id"] for e in self.page(self.A)["events"]]
        services.customer_alert_read(self.A, self.con, MEMBER, self.aid, ids)
        services.customer_alert_read(self.A, self.con, MEMBER, self.aid, ids + ids)
        self.con.commit()
        rows = [json.loads(r[0])["event_ids"] for r in self.con.execute(
            "SELECT detail FROM audit WHERE action='customer_alert_read' ORDER BY id")]
        self.assertEqual([sorted(r) for r in rows], [sorted(ids), []])


class ActivityFeedTest(ReadPPBase):

    def test_read_marks_do_not_empty_anyone_elses_activity(self):
        """RPP-SEC-R1-01: the feed reads the newest 40 audit rows; read marks
        are left out in SQL, so 40+ Mark all read presses leave the real
        activity in every member's Overview."""
        def activity(ctx):
            return services.customer_overview(ctx, self.con, MEMBER)["activity"]
        before = {ctx.principal_id: activity(ctx) for ctx in self.everyone}
        # Real alert activity (created, checked) shows for everyone.
        for ctx in self.everyone:
            self.assertIn(f"#/alert/{self.aid}",
                          [i["href"] for i in before[ctx.principal_id]], ctx.principal_id)
        ids = list(self.view(self.A)["marks"])
        for _ in range(45):
            services.customer_alert_read(self.A, self.con, MEMBER, self.aid, ids)
        self.con.commit()
        for ctx in self.everyone:
            self.assertEqual(activity(ctx), before[ctx.principal_id], ctx.principal_id)


    def test_hidden_actions_do_not_empty_anyone_elses_activity(self):
        """RPP-R2-03: the feed selects only the actions it shows, so 45
        saved-search saves, edits or deletes, or 45 monitor rows (or any
        other hidden action) leave the real activity in every member's
        Overview."""
        def activity(ctx):
            return services.customer_overview(ctx, self.con, MEMBER)["activity"]
        before = {ctx.principal_id: activity(ctx) for ctx in self.everyone}
        for action in ("customer_item_save", "customer_item_edit", "customer_item_delete",
                       "customer_alert_monitor", "customer_conversion_retire",
                       "placement_evaluate"):
            rev = db.aggregate_revision(self.con)
            for n in range(45):
                db.audit(self.con, self.A.principal_id, action, "customer_saved_item",
                         str(n), {"campaign": 7}, rev)
            db.bump_revision(self.con)
            self.con.commit()
            for ctx in self.everyone:
                self.assertEqual(activity(ctx), before[ctx.principal_id],
                                 f"45 {action} rows: {ctx.principal_id}")

    def test_real_evidence_attach_and_detach_rows_show_in_everyones_feed(self):
        """RPP-R3-01: campaigns.save_placement_evidence and
        remove_placement_evidence store the campaign as the audit target and
        {placement, ref} as detail. 45 real attach/detach actions show as
        "Campaign evidence updated" on that placement, and leave every
        member's Overview with real items, not an empty feed."""
        from tests.test_backend_allocation import _brief, _placement, _revs, _supplied
        from earwitness.app import campaigns, citations
        camp = _supplied(self.con, _brief(self.con))["campaign"]
        pid = _placement(self.con, camp["id"], camp["revision"])["placement"]["id"]
        ref = citations.current_source_ref(self.con, "evidence", "r8")
        for n in range(45):
            change = (campaigns.save_placement_evidence if n % 2 == 0
                      else campaigns.remove_placement_evidence)
            change(self.con, camp["id"], pid, ref, _revs(self.con, camp["id"])[0])
        self.con.commit()
        evidence = ("Campaign evidence updated", f"#/campaign/{camp['id']}/placement/{pid}")
        for ctx in self.everyone:
            overview = services.customer_overview(ctx, self.con, MEMBER)["activity"]
            self.assertEqual([(i["text"], i["href"]) for i in overview], [evidence] * 5,
                             ctx.principal_id)
            feed = customer._customer_activity_feed(self.con, ctx, 40)
            self.assertEqual([(i["text"], i["href"]) for i in feed], [evidence] * 40,
                             ctx.principal_id)

    def test_the_feed_equals_the_old_query_with_no_row_limit(self):
        """RPP-R2-03: for the owner, an editor, a viewer, the local operator
        and no caller, the feed is the old query (every customer_/placement_
        row, no 40-row limit) run through the same wording, with the private
        mistake-report rule (PRIVACY-FEED) applied on its own."""
        shown = {"customer_alert_save": {}, "customer_alert_check": {},
                 "customer_alert_lifecycle": {"lifecycle": "paused"},
                 "customer_alert_backfill": {}, "customer_report_freeze": {},
                 "customer_preferences_save": {}, "placement_save": {"campaign": 7},
                 "placement_update": {"campaign": 7},
                 # The shape campaigns.py really stores (RPP-R3-01).
                 "placement_evidence_attach": {"placement": "p", "ref": "r"},
                 "placement_evidence_detach": {"placement": "p", "ref": "r"}}
        hidden = ("customer_alert_read", "customer_item_save", "customer_item_edit",
                  "customer_item_delete", "customer_alert_monitor",
                  "customer_conversion_retire", "placement_evaluate", "export_csv")
        rev = db.aggregate_revision(self.con)
        for n, (action, detail) in enumerate(shown.items()):
            db.audit(self.con, self.B.principal_id, action, "customer_alert", str(n), detail, rev)
            db.audit(self.con, self.A.principal_id, hidden[n % len(hidden)],
                     "customer_alert", str(n), {}, rev)
        for actor in (self.A.principal_id, self.B.principal_id, ""):
            db.audit(self.con, actor, "customer_mistake_save", "evidence", "x",
                     {"ref": f"ref-{actor or 'operator'}"}, rev)
        for n in range(30):
            db.audit(self.con, self.A.principal_id, hidden[n % len(hidden)],
                     "customer_alert", str(n), {}, rev)
        db.bump_revision(self.con)
        self.con.commit()
        rows = self.con.execute(
            "SELECT at, actor, action, target_id, detail FROM audit"
            " WHERE action LIKE 'customer\\_%' ESCAPE '\\' OR action LIKE 'placement\\_%'"
            " ESCAPE '\\' ORDER BY id DESC").fetchall()
        self.assertTrue(set(shown) <= {r["action"] for r in rows})
        for action, detail in shown.items():
            self.assertIsNotNone(customer._activity_item(action, "1", detail), action)

        def private(ctx, r):
            if r["action"] != "customer_mistake_save" or ctx is None:
                return False
            if not ctx.permits("notes"):
                return True
            return not (r["actor"] == ctx.principal_id
                        or (ctx.credential_kind == "local_operator" and r["actor"] == ""))

        def old_feed(ctx, limit):
            feed = []
            for r in rows:
                item = None if private(ctx, r) else customer._activity_item(
                    r["action"], r["target_id"], json.loads(r["detail"] or "{}"))
                if item is not None:
                    feed.append({"text": item[0], "href": item[1], "at": r["at"]})
            return feed[:limit]

        operator = access.AccessContext.create(
            principal_id="owner", principal_kind="owner", workspace_id="local",
            credential_kind="local_operator")
        for label, ctx in (("owner", self.owner), ("editor A", self.A), ("editor B", self.B),
                           ("viewer", self.viewer), ("local operator", operator),
                           ("no caller", None)):
            for limit in (5, 40, 1000):
                self.assertEqual(customer._customer_activity_feed(self.con, ctx, limit),
                                 old_feed(ctx, limit), f"{label} limit {limit}")
        for label, ctx, own in (("editor A", self.A, self.A.principal_id),
                                ("local operator", operator, "operator")):
            notes = {i["href"] for i in customer._customer_activity_feed(self.con, ctx, 1000)
                     if i["text"] == "Mistake report saved"}
            self.assertIn(f"#/evidence/ref-{own}", notes, label)
            self.assertNotIn(f"#/evidence/ref-{self.B.principal_id}", notes, label)


class EventIdsInputTest(ReadPPBase):
    """POST /read's event_ids: anything but a plain integer in SQLite's
    64-bit range answers 400 INVALID_ARGUMENT and writes nothing
    (RPP-SEC-R1-02); ids are de-duplicated and one request is capped
    (RPP-SEC-R1-03)."""

    def setUp(self):
        super().setUp()
        self.ids = sorted(self.view(self.A)["marks"])

    def written(self):
        return (self.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
                self.shared_marks(), db.aggregate_revision(self.con))

    def assertRejected(self, event_ids, label):
        before = self.written()
        for name, call in (
                ("member", lambda: services.customer_alert_read(
                    self.A, self.con, MEMBER, self.aid, event_ids)),
                ("operator", lambda: customer.mark_alert_events_read(
                    self.con, self.aid, event_ids))):
            with self.assertRaises(search.FilterRejected, msg=f"{label} {name}") as raised:
                call()
            self.con.rollback()
            status, body, *_ = asgi.HostedApp._service_error(None, raised.exception)
            self.assertEqual((status, json.loads(body)["error"]["code"]),
                             (400, "INVALID_ARGUMENT"), f"{label} {name}")
            self.assertEqual(self.written(), before, f"{label} {name} wrote")

    def test_each_bad_shape_is_400_and_writes_nothing(self):
        eid = self.ids[0]
        for label, bad in (("nested list", [eid]), ("object", {"id": eid}),
                           ("true", True), ("false", False),
                           ("whole float", float(eid)), ("float", eid + 0.5),
                           ("2**63", 2 ** 63), ("-2**63-1", -2 ** 63 - 1),
                           ("2**70", 2 ** 70), ("string", str(eid)), ("null", None)):
            self.assertRejected([bad], label)
            self.assertRejected([eid, bad], f"{label} after a good id")
        self.assertNew(self.view(self.A), 3, "A")

    def test_the_64_bit_edges_are_plain_ids(self):
        out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid,
                                           [2 ** 63 - 1, -2 ** 63])
        self.assertEqual(out["marked"], 0)
        out = customer.mark_alert_events_read(self.con, self.aid, [2 ** 63 - 1, -2 ** 63])
        self.assertEqual(out["marked"], 0)

    def test_marked_counts_each_id_once(self):
        out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid,
                                           [self.ids[0]] * 5 + [self.ids[1]] * 2)
        self.assertEqual(out["marked"], 2)
        detail = json.loads(self.con.execute(
            "SELECT detail FROM audit WHERE action='customer_alert_read'").fetchone()[0])
        self.assertEqual(detail, {"marked": 2, "event_ids": self.ids[:2]})
        out = customer.mark_alert_events_read(self.con, self.aid, [self.ids[2]] * 4)
        self.assertEqual(out["marked"], 1)

    def test_one_request_is_capped(self):
        """More distinct ids than both the cap and the alert's event count
        is 400; Mark all read (at most the alert's events) always fits."""
        self.assertEqual(customer.READ_IDS_CAP, 1000)
        unknown = list(range(10 ** 6, 10 ** 6 + 997))
        out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid,
                                           self.ids + unknown + unknown)
        self.assertEqual(out["marked"], 3)
        self.con.rollback()
        self.assertRejected(self.ids + unknown + [10 ** 7], "1001 distinct")
        with mock.patch.object(customer, "READ_IDS_CAP", 2):
            self.assertRejected(self.ids + [10 ** 7], "4 distinct, cap 2, 3 events")
            out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid,
                                               self.ids + self.ids)
            self.assertEqual(out["marked"], 3)

    def test_other_alerts_events_do_not_raise_the_cap(self):
        """RPP-R2-02: the cap counts this alert's events only."""
        other = customer.save_alert(self.con, "acme again",
                                    {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
        customer.check_alert(self.con, other)
        customer.backfill_alert(self.con, other, confirm=True)
        self.con.commit()
        n_other = self.con.execute("SELECT COUNT(*) FROM customer_alert_events"
                                   " WHERE alert_id=?", (other,)).fetchone()[0]
        self.assertGreaterEqual(n_other, 2, "needs a second alert with events")
        with mock.patch.object(customer, "READ_IDS_CAP", 2):
            self.assertRejected(self.ids + [10 ** 7], "4 distinct, cap 2, 3 events here")

    def test_already_read_ids_count_towards_the_alerts_events(self):
        """RPP-R2-02: the cap counts every event of the alert, read or not,
        so naming all of them fits after some are read for everyone."""
        customer.mark_alert_events_read(self.con, self.aid, self.ids[:1])
        self.con.commit()
        with mock.patch.object(customer, "READ_IDS_CAP", 2):
            out = services.customer_alert_read(self.A, self.con, MEMBER, self.aid, self.ids)
        self.assertEqual(out["marked"], 3)


class AlertPathIdTest(unittest.TestCase):
    """RPP-SEC-R2-01: an alert id of 2**63 or more in the path is an
    unknown alert (404), not a 500, on every alert route."""

    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest("test_tracker_birth_active_local_silent_baseline")
        self._harness.setUp()
        self.addCleanup(self._harness.tearDown)
        self._call = self._harness._call

    def assertUnknown(self, method, suffix="", body=None):
        for aid in (2 ** 63 - 1, 2 ** 63, 2 ** 70, 10 ** 400):
            status, _, out = self._call(f"/api/v1/customer/alerts/{aid}{suffix}", method, body)
            self.assertEqual(status, 404, (method, suffix, aid, out))

    def test_get(self):
        self.assertUnknown("GET")

    def test_read(self):
        self.assertUnknown("POST", "/read", {"event_ids": [1]})

    def test_check(self):
        self.assertUnknown("POST", "/check", {})

    def test_lifecycle(self):
        self.assertUnknown("POST", "/lifecycle", {"lifecycle": "paused", "expected_revision": 1})

    def test_backfill(self):
        self.assertUnknown("POST", "/backfill", {"confirm": True})


class AcceptanceC14Test(unittest.TestCase):
    """RPP-R2-01: the API acceptance run's C14 posts an unknown integer id
    (200) and a string id (400); release receipts need C14 to pass."""

    def test_c12_and_c14_pass_in_process(self):
        from scripts import backend_acceptance
        root = Path(tempfile.mkdtemp(prefix="read-pp-c14-"))
        self.addCleanup(shutil.rmtree, root, True)
        backend_acceptance.prepare_workspace(root / "ws")
        out = backend_acceptance.run_hosted_http(root / "ws", only=["C12", "C14"])
        self.assertEqual({r["id"]: r["ok"] for r in out["scenarios"]},
                         {"C12": True, "C14": True}, out["scenarios"])


if __name__ == "__main__":
    import unittest
    unittest.main()
