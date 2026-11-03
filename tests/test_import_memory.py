"""IMPORTMEM: a corpus load keeps only small per-transcript facts in memory.

The load plan holds no parsed segment map and no serialized transcript
payloads; a transcript's payload is re-read (and re-verified) when its
version row is written, and the customer-ad rebuild decodes one stored
transcript at a time. The rows a load writes stay exactly what they were
before the change (GOLDEN, recorded from the importer at e2b1060).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import ad_reads, db
from earwitness.app import import_sources as seed_src
from tests.test_uat_post_seed import FIRST, SECOND, write_root

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "import_memory_golden.json"
NAMED_OUT = seed_src.NAMED_OUT_REL
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")
_SHADOW = ("_data", "_idx", "_docsize", "_config", "_content")
# Random identities a load mints (publication ids, view-state tokens).
_RANDOM = re.compile(r"sr1:[0-9a-f]{32}")
_TOKEN = re.compile(r"^(ws-[0-9a-f]{16}|[0-9a-f]{32})$")
_TOKEN_TABLES = ("meta", "research_state")


def _mask(table, v):
    if isinstance(v, bytes):
        return v.hex()
    if not isinstance(v, str):
        return v
    v = _RANDOM.sub("<sr>", _TS.sub("<ts>", v))
    return "<tok>" if table in _TOKEN_TABLES and _TOKEN.match(v) else v


def named(ep, cid, start, end):
    return {"candidate_id": cid, "ep_id": ep, "start_s": start, "end_s": end,
            "brand": "quionce", "brand_spoken": "quionce", "brand_url": None,
            "brand_code": None, "anchor": "namer:jev-choice", "confidence": "high",
            "confidence_score": 0.9, "needs_review": False, "offer_codes": [], "urls": [],
            "excerpt": "this episode is sponsored by quionce", "source": "jev+namer",
            "fetch_date": "2026-09-21", "provenance": {"jev_window_ids": ["window_001"]}}


def make_root(root: Path, copies: dict) -> Path:
    write_root(root, copies)
    (root / NAMED_OUT).parent.mkdir(parents=True, exist_ok=True)
    (root / NAMED_OUT).write_text(json.dumps([named("s1", "s1.named.60-80", 60.0, 80.0),
                                              named("s2", "s2.named.20-40", 20.0, 40.0)]))
    return root


def dump(con) -> dict:
    """Every table's rows, sorted, with timestamps masked (they are the
    only values a load does not decide)."""
    out = {}
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"
                                        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for table in tables:
        if any(table.endswith(s) for s in _SHADOW) and "_fts" in table:
            continue
        rows = []
        for row in con.execute(f"SELECT * FROM {table}"):
            rows.append([_mask(table, v) for v in tuple(row)])
        out[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))
    return out


def walk(value, seen=None):
    """Every value reachable from a plan (dicts, lists, tuples, sets)."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    items = value.values() if isinstance(value, dict) else \
        value if isinstance(value, (list, tuple, set)) else ()
    for item in items:
        yield from walk(item, seen)


class _Root(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".tmp-importmem-test-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class PlanHoldsSmallFactsTest(_Root):
    def test_the_plan_holds_no_segments_and_no_transcript_payloads(self):
        root = make_root(self.tmp / "first", FIRST)
        plan = seed_src._build_seed_plan(root)
        for value in walk(plan):
            if isinstance(value, list) and value and all(
                    isinstance(s, dict) and {"start", "end", "text"} <= set(s) for s in value):
                self.fail(f"the plan holds a segment list: {value[:1]}")
            if isinstance(value, str) and '"segments"' in value:
                self.fail(f"the plan holds a serialized transcript: {value[:80]}")
        for ver in plan["versions"]:
            if ver["source_key"].startswith("transcript:"):
                self.assertNotIn("payload", ver)
        # The facts a load needs from a transcript are still there.
        self.assertEqual(plan["_extent"]["s1"], 240.0)
        jev = [e for e in plan["evidence"] if e["id"].startswith("jev:")]
        self.assertEqual([e["excerpt"][:30] for e in jev], ["this episode is sponsored by q"])


class LoadRowsUnchangedTest(_Root):
    """Three loads (first copies, later copies, first copies again: the
    kept-copy path) and an upgrade rebuild write the rows they wrote
    before IMPORTMEM. The transcript_segments ids of the later steps were
    re-recorded for the AGA-R1-06 id floor (transcript_index.index_versions:
    a re-indexed version takes ids above every id present at the start of
    the call); every other value is the e2b1060 recording."""

    def run_loads(self) -> dict:
        # The golden is the rule-free load (EARWITNESS_AD_RULES=off); the
        # import-side ad rules have their own tests (test_ad_rules_import).
        env = mock.patch.dict(os.environ, {"EARWITNESS_AD_RULES": "off"})
        env.start()
        self.addCleanup(env.stop)
        first = make_root(self.tmp / "first", FIRST)
        second = make_root(self.tmp / "second", SECOND)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        out = {}
        for name, root in (("first", first), ("second", second), ("again", first)):
            report = seed_src.seed_all(root, con, "test")
            con.commit()
            out[name] = {"report": json.loads(_TS.sub("<ts>", json.dumps(report, default=str))),
                         "tables": dump(con)}
        with db.transaction(con, write=True):
            rebuilt = ad_reads.rebuild_from_workspace(con)
        con.commit()
        out["rebuild"] = {"report": rebuilt, "tables": dump(con)}
        return out

    def test_rows_match_the_golden_dump(self):
        got = json.loads(json.dumps(self.run_loads(), default=str))
        want = json.loads(GOLDEN.read_text())
        retired = {}
        for step in ("first", "second", "again", "rebuild"):
            # CLIPGUARD R8: every build also writes the clean-clip list
            # (ad_reads_meta), empty with the rules off (nothing judged).
            meta = got[step]["tables"]["ad_reads_meta"]
            self.assertIn([ad_reads.CLIP_SHOWN, '{"evidence_ids":[]}'], meta, step)
            # CLIP-STOP-FIX4: a build keeps the last display span of each
            # row it drops from ad_reads (a load retired it), one per row.
            retired[step] = [r for r in meta if r[0].startswith(ad_reads.RETIRED_DISPLAY)]
            got[step]["tables"]["ad_reads_meta"] = [
                r for r in meta if r[0] != ad_reads.CLIP_SHOWN
                and not r[0].startswith(ad_reads.RETIRED_DISPLAY)]
            self.assertEqual(got[step]["report"], want[step]["report"], step)
            self.assertEqual(sorted(got[step]["tables"]), sorted(want[step]["tables"]), step)
            for table in want[step]["tables"]:
                self.assertEqual(got[step]["tables"][table], want[step]["tables"][table],
                                 (step, table))
        self.assertEqual({k: len(v) for k, v in retired.items()},
                         {"first": 0, "second": 5, "again": 9, "rebuild": 9})
        self.assertEqual(retired["rebuild"], retired["again"])
        self.assertTrue(set(map(tuple, retired["second"])) <= set(map(tuple, retired["again"])))


class RetryReadsNothingTest(_Root):
    def test_an_unchanged_reload_reads_each_transcript_once(self):
        root = make_root(self.tmp / "first", FIRST)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        seed_src.seed_all(root, con, "test")
        con.commit()
        reads = []

        def reader(path):
            reads.append(Path(path).name)
            return Path(path).read_bytes()
        src = dict(seed_src.seed_sources(root), read=reader)
        seed_src.seed_all(root, con, "test", sources=src)
        con.commit()
        transcripts = sorted(r for r in reads if r.split(".")[0] in FIRST)
        self.assertEqual(transcripts, sorted(f"{ep}.json" for ep in FIRST))


class ChangedAtInsertTest(_Root):
    def test_a_transcript_changed_after_planning_is_refused_and_rolls_back(self):
        root = make_root(self.tmp / "first", FIRST)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        before = dump(con)
        seen = []

        def reader(path):
            p = Path(path)
            if p.name == "s1.json":
                seen.append(1)
                if len(seen) == 2:  # the plan has read it; now it changes
                    p.write_text(json.dumps({"segments": [
                        {"start": 0.0, "end": 5.0, "text": "swapped"}]}))
            return p.read_bytes()
        src = dict(seed_src.seed_sources(root), read=reader)
        with self.assertRaises(seed_src.ImportError):
            seed_src.seed_all(root, con, "test", sources=src)
        con.rollback()
        self.assertEqual(len(seen), 2)
        self.assertEqual(dump(con), before)

    def test_a_reactivated_transcript_changed_after_planning_is_refused_and_rolls_back(self):
        """IMLR-R1-01: the copy a load reactivates (held, inactive, not
        older than the episode's pin) is re-read and re-checked too."""
        first = make_root(self.tmp / "first", FIRST)
        second = make_root(self.tmp / "second", SECOND)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        for root in (first, second):
            seed_src.seed_all(root, con, "test")
            con.commit()
        # s1's first copy is now inactive. Unpinning s1 (as a show removal
        # would) makes a reload of the first copy reactivate it.
        con.execute("UPDATE episodes SET transcript_version_id=NULL WHERE id='s1'")
        con.commit()
        digest = seed_src.sha256_bytes((first / seed_src.TRANSCRIPTS_DIR / "s1.json").read_bytes())
        held = con.execute("SELECT is_active FROM source_versions"
                           " WHERE source_key='transcript:s1' AND sha256=?", (digest,)).fetchone()
        self.assertEqual(held["is_active"], 0)
        before = dump(con)
        seen = []

        def reader(path):
            p = Path(path)
            if p.name == "s1.json":
                seen.append(1)
                if len(seen) == 2:  # the plan has read it; now it changes
                    p.write_text(json.dumps({"segments": [
                        {"start": 0.0, "end": 5.0, "text": "swapped"}]}))
            return p.read_bytes()
        src = dict(seed_src.seed_sources(first), read=reader)
        with self.assertRaisesRegex(seed_src.ImportError, "changed since the load was planned"):
            seed_src.seed_all(first, con, "test", sources=src)
        con.rollback()
        self.assertEqual(len(seen), 2)
        self.assertEqual(dump(con), before)



class _Rescreened(_Root):
    """FIRST then SECOND seeded, then a Jev re-screen (rs-1) of every
    episode but s3."""

    def setUp(self):
        super().setUp()
        from earwitness.app import jev_rescreen
        from tests.test_uat_dec_rescreen import screen_corpus
        self.first = make_root(self.tmp / "first", FIRST)
        self.second = second = make_root(self.tmp / "second", SECOND)
        ws = self.tmp / "ws"
        ws.mkdir()
        self.con = db.connect(ws)
        self.addCleanup(self.con.close)
        db.init_schema(self.con)
        self.con.commit()
        for root in (self.first, second):
            seed_src.seed_all(root, self.con, "test")
            self.con.commit()
        # s3 gets no re-screen result: pinned, but no re-screen entry names it.
        screen_corpus(self.con, self.tmp / "corpus", drop=("s3",))
        jev_rescreen.import_results(self.con, self.tmp / "corpus", dry_run=False, run_id="rs-1")
        self.con.commit()


class CarryForwardDecodesPerEntryTest(_Rescreened):
    """IMPORTMEM-R1-01: a reload over a Jev re-screen decodes a transcript
    only for a re-screen entry on the episode's current copy, one at a
    time, and carries what it carried before."""

    def test_carry_forward_decodes_one_listed_copy_at_a_time_and_carries_the_same(self):
        import json as _json
        from unittest import mock
        from earwitness.app import jev_rescreen
        _jev = jev_rescreen._jev
        real_loads, real_carry = _json.loads, jev_rescreen.carry_forward
        state = {"on": False, "decoded": 0, "expected": None, "report": None}

        def loads(*args, **kwargs):
            out = real_loads(*args, **kwargs)
            if state["on"] and isinstance(out, dict) and "segments" in out:
                state["decoded"] += 1
            return out

        def carry(con, *args, **kwargs):
            # The carry the importer made before IMPORTMEM, from every copy at once.
            current = jev_rescreen._current_copies(con)
            rescreen = next(p for p in (real_loads(r[0]) for r in con.execute(
                "SELECT payload FROM source_versions WHERE source_key='jev-run'"
                " ORDER BY id DESC")) if p.get("status") == jev_rescreen.RESCREEN_STATUS)
            state["expected"] = sorted(
                str(e["episode_id"]) for e in rescreen["episodes"]
                if e.get("run_id") and str(e["episode_id"]) in current
                and e.get("transcript_version_id") == current[str(e["episode_id"])]["tv"]
                and e.get("segment_count") == len(current[str(e["episode_id"])]["segments"])
                and e.get("segments_sha256") == _jev.segments_digest(
                    current[str(e["episode_id"])]["segments"]))
            state["on"] = True
            try:
                state["report"] = real_carry(con, *args, **kwargs)
            finally:
                state["on"] = False
            return state["report"]
        # A copy the workspace never held: s1's re-screen entry is now on an
        # earlier copy (not carried, and not decoded).
        (self.first / "transcripts/s1.json").write_text(json.dumps({"segments": [
            dict(seg, text=seg["text"] + " (r2)") for seg in FIRST["s1"]]}))
        with mock.patch.object(_json, "loads", loads), \
                mock.patch.object(jev_rescreen, "carry_forward", carry):
            seed_src.seed_all(self.first, self.con, "test")
        self.con.commit()
        report = state["report"]
        self.assertTrue(state["expected"])
        self.assertEqual(report["episodes"], state["expected"])
        self.assertEqual(report["carried"], len(state["expected"]))
        self.assertNotIn("s1", report["episodes"])
        # One decode per carried entry; none for s1's earlier copy or the unlisted s3.
        self.assertEqual(state["decoded"], len(state["expected"]))


class RebuildHoldsOneTranscriptTest(_Root):
    """IMPORTMEM-R1-02: ad_reads.rebuild's quote memo holds one transcript."""

    def test_the_quote_memo_never_holds_two_transcripts(self):
        import weakref
        from unittest import mock
        from earwitness.app import citations
        alive = weakref.WeakSet()
        most = []

        class Tracked(citations._PinnedTranscript):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                alive.add(self)
                most.append(len(alive))
        root = make_root(self.tmp / "first", FIRST)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        with mock.patch.object(citations, "_PinnedTranscript", Tracked):
            seed_src.seed_all(root, con, "test")
        con.commit()
        self.assertGreaterEqual(len(most), 2)  # reads quote at least two transcripts
        self.assertEqual(max(most), 1, most)


class CarryForwardGuardsTest(_Rescreened):
    """IMPMEM-SEC-R2-01: a re-screen entry is carried only with its run id
    and only when its digest matches the current copy's words; the same
    version id and segment count are not enough. A plain reseed carries
    b1, s1 and s2."""

    def reseed_with_s2_entry(self, change):
        from earwitness.app import jev_rescreen
        row = next(r for r in self.con.execute(
            "SELECT id, payload FROM source_versions WHERE source_key='jev-run' ORDER BY id DESC")
            if json.loads(r["payload"]).get("status") == jev_rescreen.RESCREEN_STATUS)
        payload = json.loads(row["payload"])
        entry = next(e for e in payload["episodes"] if e["episode_id"] == "s2")
        change(entry)
        self.con.execute("UPDATE source_versions SET payload=? WHERE id=?",
                         (json.dumps(payload), row["id"]))
        self.con.commit()
        real, out = jev_rescreen.carry_forward, {}

        def carry(*args, **kwargs):
            out["report"] = real(*args, **kwargs)
            return out["report"]
        with mock.patch.object(jev_rescreen, "carry_forward", carry):
            seed_src.seed_all(self.second, self.con, "test")
        self.con.commit()
        active = json.loads(self.con.execute(
            "SELECT payload FROM source_versions WHERE source_key='jev-run' AND is_active=1"
        ).fetchone()["payload"])
        return out["report"], [e for e in active["episodes"] if e.get("run_id") == "rs-1"]

    def assert_s2_not_carried(self, report, carried):
        self.assertEqual(report["episodes"], ["b1", "s1"])
        self.assertEqual(sorted(e["episode_id"] for e in carried), ["b1", "s1"])

    def test_same_version_and_count_with_other_words_is_not_carried(self):
        report, carried = self.reseed_with_s2_entry(
            lambda e: e.update(segments_sha256="0" * 64))
        self.assert_s2_not_carried(report, carried)

    def test_an_entry_without_its_run_id_is_not_carried(self):
        report, carried = self.reseed_with_s2_entry(lambda e: e.pop("run_id"))
        self.assert_s2_not_carried(report, carried)


class RebuildQuotesThePinTest(_Root):
    """IMLR-R1-03: on a reload of an older copy (FIRST after SECOND), the
    rebuild's segments for that episode are the copy the episode pins
    (SECOND's), never the copy this load read; a read on the older copy
    is quoted from its own copy through its evidence row."""

    def test_the_segments_of_a_resupplied_older_copy_episode_are_the_pins(self):
        first, second = make_root(self.tmp / "first", FIRST), make_root(self.tmp / "second", SECOND)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        for root in (first, second):
            seed_src.seed_all(root, con, "test")
            con.commit()
        pin = con.execute("SELECT transcript_version_id FROM episodes WHERE id='s1'").fetchone()[0]
        real, seen = ad_reads.rebuild, {}

        def rebuild(con_, **kwargs):
            seen["s1"] = kwargs["segments"].get("s1")
            return real(con_, **kwargs)
        with mock.patch.object(ad_reads, "rebuild", rebuild):
            seed_src.seed_all(first, con, "test")
        con.commit()
        self.assertEqual(con.execute("SELECT transcript_version_id FROM episodes WHERE id='s1'"
                                     ).fetchone()[0], pin)
        self.assertEqual(seen["s1"], SECOND["s1"])
        self.assertNotEqual(seen["s1"], FIRST["s1"])


if __name__ == "__main__":
    unittest.main()
