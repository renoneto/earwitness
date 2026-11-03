"""Plan A excerpt view: old artifacts do not block workspace readiness
(PA fix round 6; design 6).

A saved excerpt and a frozen report made under Plan B stay stored,
unchanged, after a change to Plan A, and are refused (403, plan) on use.
They are not pending conversion work, so the workspace stays ready and the
session keeps its frontend contract.

FROZEN-DENY (lead 2026-10-29): the same holds for a source's own deny, in
both plans. Readiness means intact and converted, never currently allowed:
a denied old item stays stored, refuses on open and export (403), and
leaks nothing; a tampered digest or a broken capture still counts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.parse

from earwitness.app import (access, asgi, browser_auth, campaigns, citations, collaboration,
                            connections, conversion, customer, db, exports, services)
from tests.test_backend_allocation import _brief, _placement, _revs, _supplied
from tests.test_plan_a_fixture import ENTRY, Workspace, member, set_plan, set_row

ACTOR = "oidc:https://i.example:owner"
DENIED = "source disclosure denied"


def _old_items(ws, saved="ad-a1", title="Saved read", moved_on=False):
    """Under Plan B: a product-created workspace with a saved excerpt of
    `saved` and a report on ad-a1 frozen while the record allowed
    transcript export. moved_on: the campaign then detaches ad-a1, so only
    the frozen report still cites it. Returns (saved item id, campaign id,
    report no, export id)."""
    con = ws.con
    if db.workspace_identity(con) is None:
        con.execute("INSERT INTO meta (key, value) VALUES ('workspace_id', 'ws-plan-a-ready')")
    conversion.record_product_creation(con, ACTOR)
    item = services.customer_save_item(member(), con, ENTRY, {
        "type": "excerpt", "title": title,
        "target": {"ref": ws.ref("evidence", saved)}})["item"]
    ref = ws.ref("evidence", "ad-a1")
    camp = _supplied(con, _brief(con))["campaign"]
    unit = _placement(con, camp["id"], camp["revision"], episode_id="ep_a")["placement"]
    revision, _ = _revs(con, camp["id"])
    services.customer_attach_evidence(member(), con, ENTRY, camp["id"], unit["id"], ref, revision)
    con.execute("UPDATE source_content_policy SET export_eligible=1"
                " WHERE content_category='transcript'")
    draft = services.customer_campaign_draft(member(), con, ENTRY, camp["id"])["draft"]
    frozen = services.customer_freeze_report(member(), con, ENTRY, camp["id"],
                                             draft["campaign"]["revision"], draft["pins"])
    export_id = con.execute("SELECT export_id FROM campaign_reports WHERE report_no=?",
                            (frozen["report_no"],)).fetchone()[0]
    if moved_on:
        revision, _ = _revs(con, camp["id"])
        services.customer_detach_evidence(member(), con, ENTRY, camp["id"], unit["id"], ref,
                                          revision)
    return item["id"], camp["id"], frozen["report_no"], export_id


class _Base(unittest.TestCase):
    UNTRANSCRIBED = False

    def setUp(self):
        self.ws = Workspace("B", untranscribed=self.UNTRANSCRIBED)
        self.addCleanup(self.ws.close)
        self.con = self.ws.con
        self.item_id, self.cid, self.report_no, self.export_id = _old_items(self.ws)
        self.keys = {f"saved_capture:{self.item_id}", f"artifact:{self.export_id}"}
        self.pin_keys = self.source_pins()

    def source_pins(self):
        return {e["key"] for e in conversion.dry_run(self.con)["definitions"]
                if e["kind"] == "source_pin"}

    def notes(self):
        return {e["key"]: (e["disposition"], e["notes"])
                for e in conversion.dry_run(self.con)["definitions"]}

    def assertConsistent(self):
        """dry_run, status and pending_definitions agree."""
        pending = conversion.pending_definitions(self.con)
        self.assertEqual(conversion.dry_run(self.con)["pending_definitions"], pending)
        self.assertEqual(conversion.status(self.con)["pending_definitions"], pending)
        return pending

    def assertReady(self):
        self.assertEqual(self.assertConsistent(), [])
        self.assertTrue(conversion.readiness(self.con)["ready"])
        self.assertEqual(customer.session_contract_marker(self.con), customer.SESSION_CONTRACT)

    def assertWithheld(self, note=DENIED):
        """Ready with the contract; both old items (and the placement's
        source pin) stay stored, qualified with the refusal's words as the
        note, and every role gets 403 on open and export. The plan words
        are the note only where the plan rule alone refuses (FDC-R1-04)."""
        self.assertReady()
        notes = self.notes()
        for key in self.keys | self.pin_keys:
            self.assertEqual(notes[key], ("qualified", [note]), key)
        for role in ("owner", "editor", "viewer"):
            ctx = member(principal_kind=role)
            for name, call in (
                    ("open report", lambda: services.customer_report(
                        ctx, self.con, ENTRY, self.cid, self.report_no)),
                    ("report bytes", lambda: campaigns.get_customer_report_html(
                        self.con, self.cid, self.report_no, ctx=ctx)),
                    ("export", lambda: services.customer_export_bytes(
                        ctx, self.con, ENTRY, self.export_id)),
                    ("export bytes", lambda: exports.load_export_bytes(
                        self.con, self.export_id, ctx, export=True)),
                    ("saved item", lambda: services.customer_saved_item(
                        ctx, self.con, ENTRY, self.item_id))):
                with self.subTest(role=role, call=name):
                    with self.assertRaises(access.AuthorizationError) as raised:
                        call()
                    self.assertEqual(raised.exception.status, 403)

    def assertPending(self, key):
        self.assertIn(key, self.assertConsistent())
        self.assertFalse(conversion.readiness(self.con)["ready"])
        self.assertIsNone(customer.session_contract_marker(self.con))


class OldArtifactsAfterPlanATest(_Base):
    def test_b_then_a_with_old_items_is_ready_and_keeps_the_contract(self):
        self.assertReady()
        self.ws.plan("A")
        self.assertReady()
        # The plan rule alone refuses the old items: the plan words. The
        # placement's pin reads at excerpt level, so it is not refused.
        notes = self.notes()
        for key in self.keys:
            self.assertEqual(notes[key], ("qualified", [citations.PLAN_REFUSAL]), key)
        for key in self.pin_keys:
            self.assertEqual(notes[key], ("qualified", []), key)

    def test_the_old_report_is_still_listed_as_plan_and_refused_on_open(self):
        self.ws.plan("A")
        rows = services.customer_reports(member(), self.con, ENTRY, self.cid)["reports"]
        self.assertEqual([(r["report_no"], r["available"], r["exportable"], r.get("export_refusal"))
                          for r in rows], [(self.report_no, True, False, "plan")])
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_report(member(), self.con, ENTRY, self.cid, self.report_no)
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(getattr(raised.exception, "details", None), {"reason": "plan"})
        with self.assertRaises(access.AuthorizationError) as raised:
            services.customer_saved_item(member(), self.con, ENTRY, self.item_id)
        self.assertEqual(raised.exception.status, 403)

    def test_a_metadata_deny_on_old_items_keeps_the_workspace_ready_and_refuses_them(self):
        # Was test_a_non_plan_refusal_on_an_old_item_still_blocks (FROZEN-DENY).
        for key in ("detector:ad-a1", "transcript:ep_a"):
            with self.subTest(denied=key):
                self.ws.plan("A")
                self.ws.row(key, "metadata", "deny")
                self.assertWithheld()

    def test_a_tampered_old_report_still_blocks(self):
        self.ws.plan("A")
        self.con.execute("UPDATE campaign_reports SET report_json=report_json||' '"
                         " WHERE report_no=?", (self.report_no,))
        pending = self.assertConsistent()
        self.assertIn(f"artifact:{self.export_id}", pending)
        self.assertFalse(conversion.readiness(self.con)["ready"])


class PlanBUnchangedTest(_Base):
    def test_plan_b_old_items_are_ready(self):
        self.assertReady()

    def test_plan_b_deny_on_old_items_keeps_the_workspace_ready_and_refuses_them(self):
        # Was test_plan_b_refusal_on_an_old_item_still_blocks (FROZEN-DENY).
        for key, category in (("detector:ad-a1", "metadata"), ("detector:ad-a1", "excerpt"),
                              ("transcript:ep_a", "metadata")):
            with self.subTest(denied=(key, category)):
                self.ws.plan("B")
                self.ws.row(key, category, "deny")
                self.assertWithheld()


class MovedOnReportTest(_Base):
    """The campaign detached the read after the freeze: only the frozen
    report and the saved item cite it, so their own rights checks (not the
    current draft's) refuse them."""

    def setUp(self):
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        self.con = self.ws.con
        self.item_id, self.cid, self.report_no, self.export_id = _old_items(
            self.ws, moved_on=True)
        self.keys = {f"saved_capture:{self.item_id}", f"artifact:{self.export_id}"}
        self.pin_keys = self.source_pins()

    def test_a_deny_on_a_report_the_campaign_moved_past_is_withheld(self):
        for plan, category in (("B", "excerpt"), ("B", "metadata"), ("A", "metadata")):
            with self.subTest(plan=plan, denied=category):
                self.ws.plan(plan)
                self.ws.row("detector:ad-a1", category, "deny")
                self.assertWithheld()
                for role in ("owner", "editor", "viewer"):
                    try:
                        rows = services.customer_reports(member(principal_kind=role), self.con,
                                                         ENTRY, self.cid)["reports"]
                    except access.AuthorizationError as exc:
                        self.assertEqual(exc.status, 403)
                    else:
                        self.assertEqual(more_than_its_number(rows, self.report_no), [])


class DeniedButBrokenStillBlocksTest(_Base):
    """FROZEN-DENY MR conditions 1a and 1b: a deny never hides a tampered
    digest or a broken capture or source pin, in either plan. Each case
    also runs with no deny, so readiness is shown not to depend on the
    rights rows (FDC-R1-01)."""

    DENIES = (("B", "detector:ad-a1", "excerpt"), ("B", "detector:ad-a1", "metadata"),
              ("A", "detector:ad-a1", "metadata"))
    CASES = (("B", None, None),) + DENIES

    def deny(self, plan, key, category):
        self.ws.plan("B")
        self.ws.plan(plan)
        if key is not None:
            self.ws.row(key, category, "deny")

    def corrupt_capture(self, table, column, where, args):
        row = self.con.execute(f"SELECT {column} FROM {table} WHERE {where}", args).fetchone()
        captured = json.loads(row[0])
        captured["capture_digest"] = "0" * 64
        self.con.execute(f"UPDATE {table} SET {column}=? WHERE {where}",
                         (json.dumps(captured), *args))
        return row[0]

    def assertPendingKind(self, kind, *words):
        """One definition of `kind` is pending (needs_review, its note
        holding `words`), and the workspace is not ready."""
        pending = [k for k in self.assertConsistent() if k.startswith(kind + ":")]
        self.assertTrue(pending, kind)
        notes = self.notes()
        for key in pending:
            self.assertEqual(notes[key][0], "needs_review", key)
            for word in words:
                self.assertIn(word, " ".join(notes[key][1]), key)
        self.assertFalse(conversion.readiness(self.con)["ready"])
        self.assertIsNone(customer.session_contract_marker(self.con))

    def test_a_tampered_report_under_a_deny_still_blocks(self):
        for case in self.DENIES:
            with self.subTest(case=case):
                self.deny(*case)
                self.con.execute("UPDATE campaign_reports SET report_json=report_json||' '"
                                 " WHERE report_no=?", (self.report_no,))
                try:
                    self.assertPending(f"artifact:{self.export_id}")
                finally:
                    self.con.execute("UPDATE campaign_reports SET"
                                     " report_json=rtrim(report_json, ' ') WHERE report_no=?",
                                     (self.report_no,))

    def test_tampered_export_bytes_under_a_deny_still_block(self):
        for case in self.DENIES:
            with self.subTest(case=case):
                self.deny(*case)
                self.con.execute("UPDATE exports SET payload=payload||' ' WHERE id=?",
                                 (self.export_id,))
                try:
                    self.assertPending(f"artifact:{self.export_id}")
                finally:
                    self.con.execute("UPDATE exports SET payload=rtrim(payload, ' ') WHERE id=?",
                                     (self.export_id,))

    def test_a_broken_report_capture_under_a_deny_still_blocks(self):
        for case in self.DENIES:
            with self.subTest(case=case):
                self.deny(*case)
                before = self.corrupt_capture("exports", "captured_json", "id=?",
                                              (self.export_id,))
                try:
                    self.assertPending(f"artifact:{self.export_id}")
                finally:
                    self.con.execute("UPDATE exports SET captured_json=? WHERE id=?",
                                     (before, self.export_id))

    def test_a_broken_saved_capture_under_a_deny_still_blocks(self):
        for case in self.DENIES:
            with self.subTest(case=case):
                self.deny(*case)
                before = self.corrupt_capture("customer_saved_items", "captured_json", "id=?",
                                              (self.item_id,))
                try:
                    self.assertPending(f"saved_capture:{self.item_id}")
                finally:
                    self.con.execute("UPDATE customer_saved_items SET captured_json=? WHERE id=?",
                                     (before, self.item_id))

    def drop_publication(self, captured):
        """FDC-R1-01 (the code reviewer's probe D): the publication a
        capture cites is gone, after the same projection was republished
        under a new id, so source qualification still passes. Returns the
        undo."""
        state_id, _window = citations._parse_source_ref(captured["source_ref"])
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(source_states)")]
        row = dict(self.con.execute("SELECT * FROM source_states WHERE id=?",
                                    (state_id,)).fetchone())
        clone = dict(row, id="sr1:" + "f" * 32, publication_order=row["publication_order"] + 100)
        insert = (f"INSERT INTO source_states ({','.join(cols)})"
                  f" VALUES ({','.join('?' * len(cols))})")
        self.con.execute("PRAGMA foreign_keys=OFF")
        self.con.execute(insert, [clone[c] for c in cols])
        self.con.execute("DELETE FROM source_states WHERE id=?", (state_id,))

        def undo():
            self.con.execute(insert, [row[c] for c in cols])
            self.con.execute("DELETE FROM source_states WHERE id=?", (clone["id"],))
            self.con.execute("PRAGMA foreign_keys=ON")
        return undo

    def test_a_saved_capture_whose_publication_is_gone_still_blocks_under_a_deny(self):
        captured = json.loads(self.con.execute(
            "SELECT captured_json FROM customer_saved_items WHERE id=?",
            (self.item_id,)).fetchone()[0])
        for case in self.CASES:
            with self.subTest(case=case):
                self.deny(*case)
                undo = self.drop_publication(captured)
                try:
                    self.assertTrue(conversion.source_qualification(self.con)["qualified"])
                    self.assertPending(f"saved_capture:{self.item_id}")
                    notes = self.notes()[f"saved_capture:{self.item_id}"]
                    self.assertEqual(notes, ("needs_review", ["capture publication is unretained"]))
                finally:
                    undo()

    def test_an_evidence_artifact_whose_publication_is_gone_still_blocks_under_a_deny(self):
        # No export path writes an evidence capture today; the artifact
        # branch runs the same live check as validate_capture all the same.
        saved = self.con.execute("SELECT captured_json FROM customer_saved_items WHERE id=?",
                                 (self.item_id,)).fetchone()[0]
        export_id = self.con.execute(
            "INSERT INTO exports (workspace_id, created_at, format, filter, aggregate_rev,"
            " item_count, payload, title, digest, count_units, captured_json)"
            " VALUES ('local', ?, 'json', '{}', 0, 1, 'x', 'Evidence', ?, 'items', ?)",
            (db.utcnow(), hashlib.sha256(b"x").hexdigest(), saved)).lastrowid
        for case in self.CASES:
            with self.subTest(case=case):
                self.deny(*case)
                if case[1] is not None:
                    self.assertEqual(self.notes()[f"artifact:{export_id}"],
                                     ("qualified", [DENIED]))
                undo = self.drop_publication(json.loads(saved))
                try:
                    self.assertPending(f"artifact:{export_id}")
                    self.assertEqual(self.notes()[f"artifact:{export_id}"],
                                     ("needs_review", ["capture publication is unretained"]))
                finally:
                    undo()

    def test_a_capture_whose_policy_identity_is_gone_still_blocks_under_a_deny(self):
        # The cited publication's pinned source version is gone, so the
        # capture's policy identity is unretained: for a capture saved
        # under Plan B (today's locators) and one saved under Plan A (pa1).
        self.ws.plan("A")
        pa1_id = services.customer_save_item(member(), self.con, ENTRY, {
            "type": "excerpt", "title": "Plan A read",
            "target": {"ref": self.ws.ref("evidence", "ad-a1")}})["item"]["id"]
        captured = json.loads(self.con.execute(
            "SELECT captured_json FROM customer_saved_items WHERE id=?", (pa1_id,)).fetchone()[0])
        self.assertTrue(any(loc.get("rule") == citations.PA1 for loc in captured["disclosure"]))
        state_id, _window = citations._parse_source_ref(captured["source_ref"])
        pins = json.loads(self.con.execute("SELECT pins_json FROM source_states WHERE id=?",
                                           (state_id,)).fetchone()[0])
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(source_versions)")]
        # The excerpt policy's version, then the pinned transcript's
        # version (FDK-C-02: mutant K11).
        for case, pin in ((case, pin) for pin in ("source_version_id", "transcript_version_id")
                          for case in self.CASES):
            version = pins[pin]
            row = self.con.execute("SELECT * FROM source_versions WHERE id=?",
                                   (version,)).fetchone()
            with self.subTest(case=case, pin=pin):
                self.deny(*case)
                self.con.execute("PRAGMA foreign_keys=OFF")
                self.con.execute("DELETE FROM source_versions WHERE id=?", (version,))
                try:
                    pending = self.assertConsistent()
                    notes = self.notes()
                    for item_id in (self.item_id, pa1_id):
                        key = f"saved_capture:{item_id}"
                        self.assertIn(key, pending)
                        self.assertEqual(notes[key][0], "needs_review", key)
                        self.assertIn("policy identity is unretained", notes[key][1][0], key)
                    self.assertFalse(conversion.readiness(self.con)["ready"])
                finally:
                    self.con.execute(f"INSERT INTO source_versions ({','.join(cols)})"
                                     f" VALUES ({','.join('?' * len(cols))})", tuple(row))
                    self.con.execute("PRAGMA foreign_keys=ON")

    def test_a_tampered_source_pin_payload_still_blocks_under_a_deny(self):
        # FDC-R1-03: the pinned payload digests are rechecked (MC1).
        version = json.loads(self.con.execute(
            "SELECT s.pins_json FROM source_states s JOIN placement_evidence p ON p.ref=s.id"
        ).fetchone()[0])["source_version_id"]
        for case in self.CASES:
            with self.subTest(case=case):
                self.deny(*case)
                self.con.execute("UPDATE source_versions SET payload=payload||' ' WHERE id=?",
                                 (version,))
                try:
                    self.assertPendingKind("source_pin", "pinned source payload digest")
                finally:
                    self.con.execute("UPDATE source_versions SET payload=rtrim(payload, ' ')"
                                     " WHERE id=?", (version,))

    def test_a_source_pin_window_outside_its_bounds_still_blocks_under_a_deny(self):
        # FDC-R1-07: the issued window is checked against the pinned
        # bounds where the read is at full level (Plan B), with or without
        # a deny. Plan A reads the pin at excerpt level, which ignores the
        # window, so it qualifies with or without a deny (FDK-C-01): a deny
        # never decides readiness.
        placement, ref = self.con.execute(
            "SELECT placement_id, ref FROM placement_evidence").fetchone()
        for case in self.CASES + (("A", None, None),):
            with self.subTest(case=case):
                self.deny(*case)
                self.con.execute("UPDATE placement_evidence SET ref=? WHERE placement_id=?",
                                 (ref.split(":w:")[0] + ":w:9000:9001", placement))
                try:
                    if case[0] == "B":
                        self.assertPendingKind("source_pin", "outside pinned source bounds")
                    else:
                        self.assertReady()
                        pins = [n for k, n in self.notes().items()
                                if k.startswith("source_pin:placement_evidence/")]
                        self.assertEqual(pins, [("qualified", [DENIED] if case[1] else [])])
                finally:
                    self.con.execute("UPDATE placement_evidence SET ref=? WHERE placement_id=?",
                                     (ref, placement))

    def test_a_source_pin_missing_a_component_reads_needs_review_under_a_deny(self):
        # FDC-R1-08: the pinned components are resolved, as source
        # qualification does, so the pin itself is named (not qualified).
        state = self.con.execute("SELECT s.record_id, s.pins_json FROM source_states s"
                                 " JOIN placement_evidence p ON p.ref=s.id").fetchone()
        where = ("evidence_id=? AND review_revision=?",
                 (state[0], json.loads(state[1])["review_revision"]))
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(review_history)")]
        row = self.con.execute(f"SELECT * FROM review_history WHERE {where[0]}",
                               where[1]).fetchone()
        for case in self.CASES:
            with self.subTest(case=case):
                self.deny(*case)
                self.con.execute(f"DELETE FROM review_history WHERE {where[0]}", where[1])
                try:
                    self.assertPendingKind("source_pin", "review_history component is unretained")
                finally:
                    self.con.execute(f"INSERT INTO review_history ({','.join(cols)})"
                                     f" VALUES ({','.join('?' * len(cols))})", tuple(row))


class WindowLevelPerShowTest(_Base):
    """DPC-03: _pin_intact checks a stored window only where the owner's
    read with no deny is at full level, and that level follows the read's
    own rows, show by show: the pinned transcript's source row for a
    transcribed pin (not the episode's row, mutant K13), and the evidence
    source's or the episode's row for an untranscribed one (mutant K12).
    The window is stored directly, as a row written before DPC-01 would
    be (the routes now refuse it). Plan B, so plan-wide rows decide
    nothing."""

    UNTRANSCRIBED = True

    def store(self, record_id):
        """The placement's stored ref, moved to `record_id`'s read with a
        window outside its pinned bounds."""
        placement = self.con.execute("SELECT placement_id FROM placement_evidence").fetchone()[0]
        bad = self.ws.ref("evidence", record_id).split(":w:")[0] + ":w:9000:9001"
        self.con.execute("UPDATE placement_evidence SET ref=? WHERE placement_id=?",
                         (bad, placement))

    def assertWindow(self, refused, note):
        pins = [n for k, n in self.notes().items()
                if k.startswith("source_pin:placement_evidence/")]
        if refused:
            self.assertEqual(len(pins), 1)
            self.assertEqual(pins[0][0], "needs_review")
            self.assertIn("outside pinned source bounds", " ".join(pins[0][1]))
            self.assertFalse(conversion.readiness(self.con)["ready"])
            self.assertIsNone(customer.session_contract_marker(self.con))
        else:
            self.assertReady()
            self.assertEqual(pins, [("qualified", note)])

    def check(self, record_id, cases):
        self.store(record_id)
        for rows, refused in cases:
            with self.subTest(record=record_id, rows=rows):
                self.ws.plan("B")
                for key, category in rows:
                    self.ws.row(key, category, "deny")
                metadata = any(category == "metadata" for _key, category in rows)
                self.assertWindow(refused, [DENIED] if metadata else [])

    def test_a_transcribed_pin_follows_its_transcript_sources_row(self):
        self.check("ad-a1", (
            ((), True),
            # The pinned transcript's own source denies it: excerpt level.
            ((("transcript:ep_a", "transcript"),), False),
            # FDK-C-01's shape with a per-show row (DPC K13 probe).
            ((("transcript:ep_a", "transcript"), ("detector:ad-a1", "metadata")), False),
            # The episode's row is not the transcribed read's row: full level.
            ((("episode:ep_a", "transcript"),), True),
            ((("episode:ep_a", "transcript"), ("detector:ad-a1", "metadata")), True)))

    def test_an_untranscribed_pin_follows_its_evidence_and_episode_rows(self):
        self.check("ad-n1", (
            ((), True),
            ((("episode:ep_n", "transcript"),), False),
            ((("detector:ad-n1", "transcript"),), False),
            ((("episode:ep_n", "transcript"), ("detector:ad-n1", "metadata")), False),
            ((("transcript:ep_a", "transcript"),), True)))


class SessionContractTest(unittest.TestCase):
    """The hosted session of a product-created workspace changed from Plan B
    to Plan A with old items carries the frontend contract."""

    def setUp(self):
        from tests.test_backend_authority import HostedReaderHttpTest
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.headers = self.http._csrf()

    def call(self, path, method="POST", body=None):
        return self.http._call(path, method=method, body=body, headers=self.headers)

    def install(self, workspace_id, ws):
        """Replace the created workspace's database with the fixture's,
        keeping its identity and membership rows."""
        path = os.path.join(self.http.data_dir, workspace_id + ".sqlite3")
        ws.con.execute("ATTACH DATABASE ? AS fresh", (path,))
        identity = ws.con.execute("SELECT value FROM fresh.meta WHERE key='workspace_id'").fetchone()[0]
        ws.con.execute("UPDATE meta SET value=? WHERE key='workspace_id'", (identity,))
        collaboration.setup_aux_schema(ws.con)
        for table in collaboration._AUX_TABLES:
            ws.con.execute(f"INSERT INTO main.{table} SELECT * FROM fresh.{table}")
        ws.con.execute("DETACH DATABASE fresh")
        _old_items(ws)
        ws.plan("A")
        target = sqlite3.connect(path)
        try:
            ws.con.backup(target)
        finally:
            target.close()

    def test_session_after_b_to_a_with_old_items_carries_the_contract(self):
        status, created = self.call("/api/v1/workspaces",
                                    body={"workspace_id": "pa", "name": "Plan A"})
        self.assertEqual(status, 200, created)
        ws = Workspace("B")
        self.addCleanup(ws.close)
        self.install("pa", ws)
        status, before = self.call("/api/v1/session", method="GET")
        self.assertEqual(status, 200, before)
        status, switched = self.call("/api/v1/session/workspace", body={
            "workspace_id": "pa",
            "expected_workspace_switch_revision": before["workspace_switch_revision"]})
        self.assertEqual(status, 200, switched)
        self.assertEqual(switched["frontend_contract"], customer.SESSION_CONTRACT)
        status, after = self.call("/api/v1/session", method="GET")
        self.assertEqual(status, 200, after)
        self.assertEqual(after["active_workspace"], "pa")
        self.assertEqual(after["frontend_contract"], customer.SESSION_CONTRACT)



ISSUER = "https://i.example"
PEOPLE = (("owner", "owner"), ("ed", "editor"), ("vi", "viewer"))
SAVED_TITLE = "Zxwithheld saved read"
TOKEN_SCOPES = ("research:read", "transcripts:read", "watchlists:read", "campaigns:read",
                "notes:read")


FREEZE_AT = "2025-03-04T05:06:07+00:00"  # the fixture writes no other row on this day
FROZEN_ROW = {"text": "Campaign report frozen", "at": FREEZE_AT}


def _principal(subject):
    return f"oidc:{ISSUER}:{subject}"


def more_than_its_number(rows, report_no):
    """History rows that show a withheld report as more than its number.
    It is left out, or (with lr-app-nogap-alert-deny, ADS-R1-02) kept as
    exactly {"report_no", "rights_withheld"}."""
    return [r for r in rows if r.get("report_no") == report_no
            and r != {"report_no": report_no, "rights_withheld": True}]


class FrozenDenyHostedTest(unittest.TestCase):
    """FROZEN-DENY on the real HostedApp (direct ASGI calls, no sockets).

    Three triggers (lead 2026-10-29): a frozen report under a Plan B deny,
    a saved item under a Plan B deny, and a Plan A metadata deny. For each,
    and for owner, editor and viewer: the session carries the frontend
    contract, the withheld content answers 403, and no response (the 403
    bodies included, and an agent token's) holds the withheld item's
    title, ids, digests, passage, clip or citation, or counts it."""

    # name: (plan, denied rows, report withheld, saved item withheld,
    # campaign moved past the report)
    TRIGGERS = {
        "report under a Plan B deny": ("B", (("detector:ad-a1", "excerpt"),), True, False, False),
        "saved item under a Plan B deny": ("B", (("detector:ad-a2", "excerpt"),), False, True,
                                           False),
        "Plan A metadata deny": ("A", (("detector:ad-a1", "metadata"),
                                       ("detector:ad-a2", "metadata")), True, True, False),
        # Only the frozen report still cites the read: its own check refuses.
        "moved-past report under a Plan B deny": ("B", (("detector:ad-a1", "excerpt"),), True,
                                                  False, True),
    }

    def host(self, plan, denies, moved_on=False):
        tmp = tempfile.TemporaryDirectory(prefix="ew-frozen-deny-")
        self.addCleanup(tmp.cleanup)
        data = os.path.join(tmp.name, "data")
        os.makedirs(data)
        ws = Workspace("B")
        self.addCleanup(ws.close)
        self.item_id, self.cid, self.report_no, self.export_id = _old_items(
            ws, saved="ad-a2", title=SAVED_TITLE, moved_on=moved_on)
        con = ws.con
        ws.plan(plan)
        if plan == "B":
            # As at the freeze: under Plan B only the deny refuses the report.
            con.execute("UPDATE source_content_policy SET export_eligible=1"
                        " WHERE content_category='transcript'")
        for key, category in denies:
            ws.row(key, category, "deny")
        # A freeze time no other row shares: only Overview's freeze row may
        # show it (lead condition (a); FDS-R1-02).
        con.execute("UPDATE campaign_reports SET created_at=? WHERE report_no=?",
                    (FREEZE_AT, self.report_no))
        con.execute("UPDATE exports SET created_at=? WHERE id=?", (FREEZE_AT, self.export_id))
        con.execute("UPDATE audit SET at=? WHERE action='customer_report_freeze'", (FREEZE_AT,))
        report = con.execute("SELECT e.digest, e.captured_json, e.title FROM exports e"
                             " WHERE e.id=?", (self.export_id,)).fetchone()
        saved = json.loads(con.execute("SELECT captured_json FROM customer_saved_items"
                                       " WHERE id=?", (self.item_id,)).fetchone()[0])
        report_capture = json.loads(report["captured_json"])
        # The captured words are also the transcript's own words: Plan B
        # lets every member read the transcript (its own row), so they
        # count as a leak only where the plan withholds the transcript.
        self.report_needles = {
            "export digest": report["digest"],
            "report_json digest": report_capture["report_json_digest"],
            "report capture digest": report_capture["capture_digest"],
            "archive title": report["title"], "report heading": "Report 1 captured",
            "report passage": "Captured passage", "clip": "clip-good",
            "freeze day": FREEZE_AT[:10]}
        if plan == "A":
            self.report_needles["passage words"] = "brought to you by Lumen lamps go to lumen"
        self.saved_needles = {"saved id": self.item_id, "saved title": SAVED_TITLE,
                              "saved ref": saved["source_ref"],
                              "saved capture digest": saved["capture_digest"]}
        if plan == "A":
            self.saved_needles["saved text"] = saved["text"]
        path = self.db_path = os.path.join(data, "fd.sqlite3")
        target = sqlite3.connect(path)
        con.backup(target)
        target.row_factory = sqlite3.Row
        collaboration.create_workspace(target, "local", "Frozen", ACTOR)
        for subject, role in PEOPLE[1:]:
            invite = collaboration.create_invite(target, "local", _principal(subject), role, ACTOR)
            collaboration.accept_invite(target, invite["invite_id"], _principal(subject))
        target.commit()
        target.close()
        self.app = asgi.HostedApp(asgi.HostedConfig(
            data_dir=data, grants_db=os.path.join(tmp.name, "grants.sqlite3")))
        self.addCleanup(self.app.close)
        connections.register_workspace(self.app.store, "fd", path, data)
        raw = self.app.store.auth_connection()
        browser_auth.ensure_auth_schema(raw)
        now = time.time()
        for subject, _role in PEOPLE:
            raw.execute("INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
                        " email_verified, csrf_token, active_workspace, created_at, expires_at,"
                        " revoked_at) VALUES (?, ?, ?, '', 0, ?, 'fd', ?, ?, NULL)",
                        (hashlib.sha256(f"ew-browser-v1:sess-{subject}".encode()).hexdigest(),
                         ISSUER, subject, f"csrf-{subject}", now, now + 3600))
        raw.commit()
        raw.close()

    def get(self, path, *, subject=None, method="GET", body=None):
        """(status, body bytes) of one request as `subject`'s browser
        session, or as an owner's agent token when subject is None (a
        fresh grant per request: one grant's burst is 20 reads)."""
        extra = []
        if subject:
            auth = (b"cookie", f"ew_session=sess-{subject}".encode())
            if method != "GET":  # a browser write: its CSRF token and switch revision
                _status, session = self.get("/api/v1/session", subject=subject)
                extra = [(browser_auth.CSRF_HEADER.encode(), f"csrf-{subject}".encode()),
                         (b"x-earwitness-workspace-switch-revision",
                          str(json.loads(session)["workspace_switch_revision"]).encode())]
        else:
            token = self.app.store.issue_grant(
                member_id=ACTOR, member_role="owner", client_id="c1", workspace_id="fd",
                scopes=list(TOKEN_SCOPES), content_policy={})["access_token"]
            auth = (b"authorization", f"Bearer {token}".encode())
        path, _, qs = path.partition("?")
        payload = json.dumps(body).encode() if body is not None else b""
        sent = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            sent.append(message)
        asyncio.run(self.app({"type": "http", "method": method, "path": path, "scheme": "http",
                              "query_string": qs.encode(), "headers": [
                                  (b"host", b"127.0.0.1:8771"), auth, *extra,
                                  (b"content-type", b"application/json"),
                                  (b"content-length", str(len(payload)).encode())]},
                             receive, send))
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        return status, b"".join(m.get("body", b"") for m in sent
                                if m["type"] == "http.response.body")

    CALLERS = PEOPLE + ((None, "agent token"),)

    def found(self, path, text, needles):
        """The needles in one body. Overview's freeze row holds the freeze
        time by design (lead condition (a)); its shape is pinned apart."""
        return [name for name, value in needles.items()
                if value.lower() in text.lower()
                and not (name == "freeze day" and path == "/api/v1/customer/overview")]

    def paths(self):
        """(listing surfaces, report content, saved item content). The
        session answer is the workspace list; the agent routes answer the
        token (a browser session gets 401 there)."""
        c = f"/api/v1/customer/campaigns/{self.cid}"
        report = f"{c}/reports/{self.report_no}"
        search = [f"/api/v1/customer/search?query=" + urllib.parse.quote(json.dumps(
            {"version": 2, "mode": mode, "q": q})) for mode in ("ads", "mentions")
            for q in ("lumen", "acme")]
        agent = [("POST", "/api/v1/evidence/search", {"filter": {"query": q}})
                 for q in ("Lumen", "Acme")]
        return (["/api/v1/session", "/api/v1/customer/overview", "/api/v1/customer/campaigns",
                 c, f"{c}/reports", "/api/v1/customer/archive", "/api/v1/customer/saved-items",
                 *search, "/api/v1/campaigns", f"/api/v1/campaigns/{self.cid}",
                 "/api/v1/evidence/ad-a1", "/api/v1/evidence/ad-a2", *agent],
                [report, f"{report}/html", f"{report}/html?download=1",
                 f"/api/v1/customer/exports/{self.export_id}"],
                [f"/api/v1/customer/saved-items/{self.item_id}"])

    def test_an_open_workspace_serves_the_items_to_every_role(self):
        # The control: with no deny, each role and the agent token open
        # both items, so each 403 below is the deny's (FDC-R1-10).
        for moved_on in (False, True):
            self.host("B", (), moved_on)
            _lists, report, saved = self.paths()
            for subject, role in self.CALLERS:
                for path in report + saved:
                    with self.subTest(moved_on=moved_on, role=role, path=path):
                        status, body = self.get(path, subject=subject)
                        self.assertEqual(status, 200, body[:200])

    # FDC-R1-06: where each marker shows with no deny, for every role and
    # the agent token (measured). A marker absent under a deny proves
    # something only where it shows here: on a listing surface, or in the
    # item's own bodies ("report", "saved item"), which answer 403 under a
    # deny. The two stored digests and the clip id are never served, even
    # when allowed: they only guard that no body ever quotes them.
    # Under Plan A the plan rule already withholds the old report and the
    # saved item with no deny, so only the history's export digest and
    # freeze day, and the read's own passage words and ref (Search, and the
    # agent's evidence routes: the read itself is allowed), are meaningful
    # there; the other Plan A markers prove nothing.
    NEVER = {"report_json digest": set(), "report capture digest": set(), "clip": set()}
    SHOWN = {
        "B": {**NEVER, "export digest": {"history", "archive", "report"},
              "archive title": {"archive"}, "report heading": {"report"},
              "report passage": {"report"},
              "freeze day": {"overview", "history", "archive", "report"},
              "saved id": {"saved list", "saved item"},
              "saved title": {"saved list", "saved item"},
              "saved ref": {"saved list", "saved item", "search"},
              "saved capture digest": {"saved list", "saved item"}},
        "A": {**NEVER, "export digest": {"history"}, "archive title": set(),
              "report heading": set(), "report passage": set(),
              "freeze day": {"overview", "history"},
              "passage words": {"search", "agent evidence"},
              "saved id": set(), "saved title": set(), "saved ref": {"search"},
              "saved capture digest": set(), "saved text": set()},
    }

    def surface(self, path):
        c = f"/api/v1/customer/campaigns/{self.cid}"
        return {"/api/v1/customer/overview": "overview", f"{c}/reports": "history",
                "/api/v1/customer/archive": "archive",
                "/api/v1/customer/saved-items": "saved list",
                "/api/v1/customer/search": "search"}.get(path) \
            or ("report" if path.startswith(f"{c}/reports/")
                or path.startswith("/api/v1/customer/exports/") else None) \
            or ("saved item" if path.startswith("/api/v1/customer/saved-items/") else None) \
            or ("agent evidence" if path.startswith("/api/v1/evidence/") else None) \
            or path

    def test_each_marker_shows_where_it_is_allowed(self):
        for plan in ("B", "A"):
            self.host(plan, ())
            lists, report, saved = self.paths()
            needles = {**self.report_needles, **self.saved_needles}
            for subject, role in self.CALLERS:
                seen = {name: set() for name in needles}
                for path in lists + report + saved:
                    method, path, sent = path if isinstance(path, tuple) else ("GET", path, None)
                    status, body = self.get(path, subject=subject, method=method, body=sent)
                    text = body.decode("utf-8", "replace").lower()
                    for name, value in needles.items():
                        if value.lower() in text:
                            seen[name].add(self.surface(path.partition("?")[0]))
                shown = dict(self.SHOWN[plan])
                if subject:  # the agent routes answer the token only
                    shown = {name: where - {"agent evidence"} for name, where in shown.items()}
                with self.subTest(plan=plan, role=role):
                    self.assertEqual(seen, shown)

    def test_each_trigger_keeps_the_workspace_and_withholds_the_item(self):
        expected = checked = 0
        for trigger, (plan, denies, report_withheld, saved_withheld, moved_on) \
                in self.TRIGGERS.items():
            self.host(plan, denies, moved_on)
            lists, report, saved = self.paths()
            needles = {**(self.report_needles if report_withheld else {}),
                       **(self.saved_needles if saved_withheld else {})}
            withheld = (report if report_withheld else []) + (saved if saved_withheld else [])
            for subject, role in self.CALLERS:
                expected += len(lists + withheld)
                with self.subTest(trigger=trigger, role=role):
                    if subject:
                        status, body = self.get("/api/v1/session", subject=subject)
                        session = json.loads(body)
                        self.assertEqual((status, session.get("active_workspace"),
                                          session.get("frontend_contract")),
                                         (200, "fd", customer.SESSION_CONTRACT), body[:300])
                    for path in withheld:
                        status, body = self.get(path, subject=subject)
                        self.assertEqual(status, 403, (path, body[:300]))
                    for path in lists + withheld:
                        method, path, sent = path if isinstance(path, tuple) else ("GET", path, None)
                        status, body = self.get(path, subject=subject, method=method, body=sent)
                        text = body.decode("utf-8", "replace")
                        self.assertLess(status, 500, (path, text[:300]))
                        self.assertEqual(self.found(path, text, needles), [], (path, status))
                        checked += 1
                        if status != 200:
                            continue
                        out = json.loads(body)
                        if path == "/api/v1/customer/overview":
                            # Lead condition (a): that a report was frozen,
                            # when, and the campaign link; no report number.
                            self.assertEqual([a for a in out["activity"] if a.get("text")
                                              == FROZEN_ROW["text"]],
                                             [dict(FROZEN_ROW, href=f"#/campaign/{self.cid}")])
                        if path.endswith("/reports") and report_withheld:
                            self.assertEqual(more_than_its_number(out["reports"], self.report_no),
                                             [])
                        if path == "/api/v1/customer/archive" and report_withheld:
                            self.assertNotIn(self.export_id, [i.get("id") for i in out["items"]])
                            self.assertEqual([i for i in out["items"] if "id" not in i
                                              and i != {"kind": "report", "rights_withheld": True,
                                                        "report_no": self.report_no}], [])
                            self.assertEqual(out["total"], len(out["items"]))
                        if path == "/api/v1/customer/saved-items" and saved_withheld:
                            # Left out, and counted only as the running
                            # version's lines say: before DENY-WORDS not at
                            # all; with it (lr-app-nogap-alert-deny), once:
                            # in the plan count where the plan also refuses
                            # the item (Plan A), else in rights_withheld.
                            counted = (0, 0) if "rights_withheld" not in out \
                                else (1, 0) if plan == "A" else (0, 1)
                            self.assertEqual((out["items"], out["total"], out["counts"]["excerpt"],
                                              (out["plan_withheld"], out.get("rights_withheld", 0))),
                                             ([], 0, 0, counted))
        # Every body of every surface was checked for every caller
        # (FDS-R1-01): no assertion above cut a subtest short.
        self.needle_checks = checked
        self.assertEqual(checked, expected)

    def test_a_window_outside_its_bounds_is_refused_on_every_plan_and_keeps_the_contract(self):
        # DPC-01 (FDK-C-01's write side): a read at excerpt level ignores an
        # issued window, so before this fix the real routes stored a ref
        # whose window lies outside its pinned bounds wherever the read was
        # at excerpt level (Plan A, or a source's own transcript deny under
        # Plan B): the editor's mistake report, the owner's placement attach
        # and research target attach. A later change to full level (an
        # upgrade to Plan B, a lifted transcript deny) then cost every member
        # the frontend contract. Every write now refuses that window (400)
        # on every plan and stores nothing; the viewer's mistake POST stays
        # a role refusal (403). Then a deny, and the shape's own change to
        # full level, keep every member's contract.
        api = "/api/v1/customer"
        shapes = (("A", (), "upgrade to Plan B"),
                  ("B", (("transcript:ep_a", "transcript"),), "lift the transcript deny"),
                  ("B", (), None))
        for plan, denies, change in shapes:
            self.host(plan, denies)
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            self.addCleanup(con.close)
            ref = con.execute("SELECT ref FROM placement_evidence").fetchone()[0]
            bad = ref.split(":w:")[0] + ":w:9000:9001"
            placement = con.execute("SELECT placement_id FROM placement_evidence").fetchone()[0]
            status, body = self.get(f"{api}/campaigns", subject="owner", method="POST", body={
                "campaign": {"name": "Window research", "mode": "research", "brand": "lumen"}})
            self.assertEqual(status, 200, body[:300])
            research = json.loads(body)["campaign"]
            status, body = self.get(f"{api}/campaigns/{research['id']}/targets", subject="owner",
                                    method="POST", body={
                                        "target": {"scope": "episode", "episode_id": "ep_a"},
                                        "expected_campaign_revision": research["revision"]})
            self.assertEqual(status, 200, body[:300])
            target = json.loads(body)
            target_id = (target.get("target") or target)["id"]

            def revision(cid):
                return con.execute("SELECT revision FROM campaigns WHERE id=?",
                                   (cid,)).fetchone()[0]
            quoted = urllib.parse.quote(bad, safe="")
            writes = (
                ("viewer mistake", "vi", f"{api}/records/{quoted}/mistake",
                 lambda: {"reason": "wrong brand"}),
                ("editor mistake", "ed", f"{api}/records/{quoted}/mistake",
                 lambda: {"reason": "wrong brand"}),
                ("owner placement attach", "owner",
                 f"{api}/campaigns/{self.cid}/placements/{placement}/evidence",
                 lambda: {"ref": bad, "expected_revision": revision(self.cid)}),
                ("owner target attach", "owner",
                 f"{api}/campaigns/{research['id']}/targets/{target_id}/evidence",
                 lambda: {"ref": bad, "expected_revision": revision(research["id"])}))
            for name, subject, path, body in writes:
                with self.subTest(plan=plan, denies=denies, write=name):
                    status, out = self.get(path, subject=subject, method="POST", body=body())
                    self.assertEqual(status, 403 if subject == "vi" else 400, out[:300])
            stored = {table: con.execute(f"SELECT count(*) FROM {table} WHERE {column} LIKE ?",
                                         ("%:w:9000:9001",)).fetchone()[0]
                      for table, column in (("customer_mistake_reports", "ref"),
                                            ("placement_evidence", "ref"),
                                            ("target_evidence", "ref"))}
            with self.subTest(plan=plan, denies=denies, check="nothing stored"):
                self.assertEqual(stored, dict.fromkeys(stored, 0))

            def keeps(**where):
                conversion._CLASSIFIED.clear()
                for subject, role in PEOPLE:
                    with self.subTest(plan=plan, denies=denies, role=role, **where):
                        status, body = self.get("/api/v1/session", subject=subject)
                        self.assertEqual((status, json.loads(body).get("frontend_contract")),
                                         (200, customer.SESSION_CONTRACT), body[:300])
            for category in ("metadata", "excerpt"):
                set_row(con, "detector:ad-a1", category, "deny")
                con.commit()
                try:
                    keeps(deny=category)
                finally:
                    set_row(con, "detector:ad-a1", category, "allow")
                    con.commit()
            if change is not None:
                if plan == "A":
                    set_plan(con, "B")
                for key, category in denies:
                    set_row(con, key, category, "allow")
                con.commit()
                keeps(change=change)

if __name__ == "__main__":
    unittest.main()
