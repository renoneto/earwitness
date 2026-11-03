"""GET /customer/alerts: one provenance lookup per source version (LR alerts speed).

On the full corpus the Alerts list took about 29 s: 14,911 single-row
lookups on source_versions (citations._version_provenance), reached through
tracker_dto and captured_disclosure, each walking the version's payload
(about 128 KB of overflow pages) to reach created_at, which is stored after
it. One list asks for the same versions again for every alert and event that
cites them.

1. One list looks each source version up at most once, and never reads the
   payload or a column stored after it (is_active, created_at): reaching
   those walks the payload's overflow pages.
2. The list is unchanged: same JSON bytes, same disclosures registered on
   the caller's context, as a stored golden of this fixture. The fixture
   mints random publication refs, wall-clock times and an event insertion
   order that varies between runs; only those are canonicalized (a ref to
   the kind and record it publishes, an event id to its alert and logical
   identity, events in logical-identity order). Every other byte, and the
   field order, is compared as returned.
3. The same holds for a caller denied `transcript` on transcript:e1 (a
   source content policy row): since Plan A the list is served with each
   alert's history withheld, and registers the same disclosures.
4. The same holds for a Plan A workspace with a shown excerpt-level (pa1)
   event (as test_plan_a_alerts), listed for the member and the owner:
   the list memo also serves Plan A's excerpt-level disclosure path.
5. _version_source_key agrees with _version_provenance for policy
   lookups: the same source_key for an existing id, None for a missing one.
The goldens were recorded on launch-readiness 621ac862's code (Plan A),
before this change.

Run: PYTHONPATH=. python -m unittest tests.test_alerts_list_provenance
"""
from __future__ import annotations

import collections
import json
import re
import sys
import unittest
from pathlib import Path

from earwitness.app import citations, customer, services
from earwitness.app.access import AccessContext

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests import test_uat_dec_d1api as d1  # noqa: E402
from tests.test_uat_d1 import _republish  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "alerts-list-provenance-golden.json"
GOLDEN_DENIED = Path(__file__).parent / "fixtures" / "alerts-list-provenance-denied-golden.json"
GOLDEN_PA1 = Path(__file__).parent / "fixtures" / "alerts-list-provenance-pa1-golden.json"
LOOKUP = re.compile(r"\bFROM\s+source_versions\s+WHERE\s+id\s*=\s*(\d+)", re.I)
SELECTED = re.compile(r"^\s*SELECT\s+(.*?)\s+FROM\s+source_versions\b", re.I | re.S)
REF = re.compile(r"sr1:[0-9a-f]{32}")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")
# payload and every column stored after it in source_versions (db.py).
PAYLOAD_AND_AFTER = ("payload", "is_active", "created_at")


def owner():
    return AccessContext.create(principal_id="local-owner", principal_kind="owner",
                                workspace_id="local", credential_kind="local_operator")


def listing(rows, ctx, **extra) -> dict:
    """A list as compared: events keyed by alert and logical identity, in
    that order (their ids and insertion order vary between runs), and the
    disclosures the list registered on ctx."""
    for row in rows or ():
        for event in row["events"]:
            event["id"] = f"{row['id']}:{event['logical_identity']}"
        row["events"].sort(key=lambda e: e["id"])
    return {**extra, "alerts": rows, "disclosures": sorted(map(list, ctx.disclosures))}


def canonical(con, value) -> str:
    """JSON text with each random publication ref named by the kind and
    record it publishes, and wall-clock times masked."""
    def ref(m):
        state = con.execute("SELECT kind, record_id FROM source_states WHERE id=?",
                            (m.group(0),)).fetchone()
        return f"sr1:{state[0]}:{state[1]}"
    return TIMESTAMP.sub("<ts>", REF.sub(ref, json.dumps(value, ensure_ascii=False))) + "\n"


class AlertsListProvenanceTest(d1.SeededBase):
    """Three ads alerts whose events cite the same e1 versions (as
    test_demo2_pass5.AlertListBuildsNoScopeIndexTest)."""

    maxDiff = None

    def setUp(self):
        super().setUp()
        cur = self.tv["cur"]
        ids = [customer.save_alert(self.con, f"acme {n}",
                                   {"version": 2, "mode": "ads", "brands": ["acme"]})["id"]
               for n in range(3)]
        self.con.commit()
        d1._evidence(self.con, "r8", "e1", 45.0, 58.0, cur, "acme")
        d1._evidence(self.con, "r9", "e1", 200.0, 230.0, cur, "acme")
        _republish(self.con)
        for aid in ids:
            customer.check_alert(self.con, aid)
        self.con.commit()

    def _listed(self):
        ctx = owner()
        statements = []
        self.con.set_trace_callback(statements.append)
        try:
            rows = customer.list_alerts(self.con, ctx=ctx)
        finally:
            self.con.set_trace_callback(None)
        return rows, ctx, statements

    def test_each_version_is_looked_up_once_and_its_payload_never_read(self):
        rows, _ctx, statements = self._listed()
        self.assertEqual(len(rows), 3)
        per_version = collections.Counter(
            m.group(1) for sql in statements for m in LOOKUP.finditer(sql))
        self.assertTrue(per_version, "the list looked up no source version")
        repeated = {v: n for v, n in per_version.items() if n > 1}
        deep = sorted({sql for sql in statements if (m := SELECTED.match(sql))
                       and {c.strip().lower() for c in m.group(1).split(",")}
                       & {"*", *PAYLOAD_AND_AFTER}})
        self.assertEqual({"looked up more than once": repeated,
                          "read the payload or a column after it": deep},
                         {"looked up more than once": {},
                          "read the payload or a column after it": []})

    def canonical(self, rows, ctx, **extra) -> str:
        return canonical(self.con, listing(rows, ctx, **extra))

    def test_list_is_unchanged(self):
        rows, ctx, _ = self._listed()
        self.assertEqual(self.canonical(rows, ctx), GOLDEN.read_text(encoding="utf-8"))

    def denied_listing(self) -> str:
        """The list for a caller denied `transcript` on transcript:e1 (a
        refusal, if any, is part of the golden)."""
        self.con.execute("INSERT INTO source_content_policy (source_key, content_category,"
                         " agent_disclosure, updated_at)"
                         " VALUES ('transcript:e1', 'transcript', 'deny', 'fixture')")
        self.con.commit()
        ctx, rows, error = owner(), None, None
        try:
            rows = customer.list_alerts(self.con, ctx=ctx)
        except Exception as exc:  # the refusal is part of the golden
            error = [type(exc).__name__, str(exc), getattr(exc, "status", None)]
        return self.canonical(rows, ctx, error=error)

    def test_denied_caller_list_is_unchanged(self):
        self.assertEqual(self.denied_listing(), GOLDEN_DENIED.read_text(encoding="utf-8"))


class PlanAExcerptLevelListTest(unittest.TestCase):
    """A Plan B alert, then Plan A, a new Plan A alert and a new read that
    emits a pa1 event (test_plan_a_alerts), listed for the member (the
    service) and the owner (customer.list_alerts)."""

    maxDiff = None

    def setUp(self):
        from tests.test_plan_a_fixture import ENTRY, Workspace, member
        self.entry, self.member = ENTRY, member
        self.ws = Workspace("B")
        self.addCleanup(self.ws.close)
        con = self.ws.con
        hidden = services.customer_save_alert(
            member(), con, ENTRY, "Hidden words", {"version": 2, "mode": "ads", "q": "qzlate"},
            "/searches", ["new_finding"])["id"]
        self.ws.add_ad("ad-a6", "ep_d", 98.0, 122.0, "Nimbus", "nimbus again")
        services.customer_check_alert(member(), con, ENTRY, hidden)
        self.ws.plan("A")
        lumen = services.customer_save_alert(
            member(), con, ENTRY, "Lumen", {"version": 2, "mode": "ads", "q": "lumen"},
            "/searches", ["new_finding"])["id"]
        self.ws.add_ad("ad-a7", "ep_a", 30.0, 45.0, "Lumen", "lumen again qzstored")
        self.assertEqual(services.customer_check_alert(
            member(), con, ENTRY, lumen)["emitted"], 1)

    def pa1_listing(self) -> str:
        con = self.ws.con
        as_member, as_owner = self.member(), owner()
        rows = services.customer_alerts(as_member, con, self.entry)["alerts"]
        shown = listing(rows, as_member)
        self.assertEqual(sum(len(r["events"]) for r in rows), 1, "no pa1 event shown")
        rows = customer.list_alerts(con, ctx=as_owner)
        return canonical(con, {"member": shown, "owner": listing(rows, as_owner)})

    def test_pa1_list_is_unchanged(self):
        self.assertEqual(self.pa1_listing(), GOLDEN_PA1.read_text(encoding="utf-8"))

    def test_a_deny_row_on_the_shown_pa1_event_gives_the_same_outcome_with_and_without_the_memo(self):
        """ALS-R4-02: a Plan A caller under an excerpt deny on either source
        of the shown pa1 event: the list (or its refusal) and the
        disclosures it registers are the same with the provenance memo as
        without it, for the member and the owner."""
        import contextlib
        from unittest import mock
        from tests.test_plan_a_fixture import set_row
        con = self.ws.con

        def outcome(read, ctx):
            try:
                return canonical(con, listing(read(ctx), ctx))
            except Exception as exc:  # a refusal is an outcome too
                return [type(exc).__name__, str(exc), getattr(exc, "status", None),
                        sorted(map(list, ctx.disclosures))]
        reads = (("member", self.member, lambda ctx: services.customer_alerts(ctx, con, self.entry)["alerts"]),
                 ("owner", owner, lambda ctx: customer.list_alerts(con, ctx=ctx)))
        for key in ("detector:ad-a7", "transcript:ep_a"):
            set_row(con, key, "excerpt", "deny")
            con.commit()
            for who, make, read in reads:
                with self.subTest(source=key, caller=who):
                    got = []
                    for memo in (citations.provenance_memo, contextlib.nullcontext):
                        with mock.patch.object(citations, "provenance_memo", memo):
                            got.append(outcome(read, make()))
                    self.assertEqual(got[0], got[1])
            set_row(con, key, "excerpt", "allow")
            con.commit()


class SourceKeyAgreesWithProvenanceTest(unittest.TestCase):
    """_version_source_key: existence and source_key only, equivalent to
    _version_provenance for policy lookups, with and without the memo."""

    def test_existing_and_missing_ids_agree(self):
        from tests.backend_fixtures import make_memory_workspace
        con = make_memory_workspace()
        self.addCleanup(con.close)
        ids = [r[0] for r in con.execute("SELECT id FROM source_versions")]
        self.assertTrue(ids)
        missing = max(ids) + 1000
        # Without the memo, and under a fresh memo filled by either first.
        for memo, key_first in ((False, True), (True, True), (True, False)):
            with citations.provenance_memo() if memo else _no_memo():
                for vid in ids + [missing, None]:
                    if key_first:
                        key = citations._version_source_key(con, vid)
                        row = citations._version_provenance(con, vid)
                    else:
                        row = citations._version_provenance(con, vid)
                        key = citations._version_source_key(con, vid)
                    where = (memo, key_first, vid)
                    if vid in ids:
                        self.assertIsNotNone(row, where)
                        self.assertEqual(key, row["source_key"], where)
                    else:
                        self.assertIsNone(row, where)
                        self.assertIsNone(key, where)


class _no_memo:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
