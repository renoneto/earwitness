"""Post-decisions item 2: a seed never moves an episode back to an older
transcript copy.

The owner workspace was seeded twice: first from a source root with the
first transcript copies (2026-09-22), then from one with later copies
(2026-09-25), so its episodes are pinned to the later copies. A seed
from the first root again offers the older files (and none at all for
episodes it never had); it must keep every current copy, its reads,
breaks and coverage, and a rerun must be a no-op. A copy the workspace
has never held is newer and still becomes current. Offline; no Jev call.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from earwitness.app import (ad_breaks, campaigns, coverage, coverage_import, customer, db,
                            episode_alias, jev_rescreen)
from earwitness.app import import_sources as seed_src
from tests.test_customer_campaigns import add_placement, seed_campaign
from tests.test_uat_dec_rescreen import DET_OUT, JEV_RUN, Base, screen_corpus, segs


def det(cid, ep, start, end):
    return {"candidate_id": cid, "ep_id": ep, "start_s": start, "end_s": end,
            "excerpt": "this episode is sponsored by quionce", "brand_spoken": "Quionce",
            "confidence": "high", "fetch_date": "2026-09-21"}


def later(segments):
    """A later transcription of the same audio: same times, other words."""
    return [dict(s, text=s["text"] + " (r1)") for s in segments]


def write_root(root: Path, copies: dict):
    """A repo-layout source root. s1 and s2 are supplied (s1 screened by the
    old run); s3 is supplied too; b1 is a benchmark twin of s2. `copies`:
    episode -> segments of the transcript file the root offers (absent =
    the root has no transcript for it, as O has none for s010..s029)."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(exist_ok=True)
    # s2 and s3 have no supplied duration (as O's episodes.json): the seed
    # takes it from the transcript copy.
    (root / "data/episodes.json").write_text(json.dumps([
        {"ep_id": ep, "show_title": "Show", "ep_title": title, "pub_date": "2026-03-10",
         "duration_min": minutes, "fetch_date": "2026-09-21"}
        for ep, title, minutes in (("s1", "One", 4.0), ("s2", "Two", None), ("s3", "Three", None))]))
    for ep, segments in copies.items():
        extra = {"show": "Show", "ep_title": "Two"} if ep == "b1" else {}
        (root / "transcripts" / f"{ep}.json").write_text(json.dumps({"segments": segments, **extra}))
    (root / DET_OUT).parent.mkdir(parents=True, exist_ok=True)
    (root / DET_OUT).write_text(json.dumps([det("c1", "s1", 60.0, 80.0),
                                            det("c3", "s3", 60.0, 80.0)]))
    (root / JEV_RUN).parent.mkdir(parents=True, exist_ok=True)
    (root / JEV_RUN).write_text(json.dumps({"run_id": "old", "episodes": [
        {"episode_id": "s1", "segment_count": 12, "answers": {
            "window_000": {"noul": 0.1, "start_s": 0.0, "end_s": 60.0},
            "window_001": {"noul": 0.95, "start_s": 60.0, "end_s": 120.0}}}]}))


# The later s2 copy runs longer (3.2 min vs 3.0): its duration is the current one.
S2_LATER = later(segs(3, 2)) + [{"start": 180.0, "end": 192.0, "text": "outro (r1)"}]
FIRST = {"s1": segs(4), "s2": segs(3, 2), "b1": S2_LATER}
SECOND = {"s1": later(segs(4)), "s2": S2_LATER, "s3": segs(3, 1), "b1": S2_LATER}


class SeedKeepsCurrentCopiesTest(Base):
    def setUp(self):
        super().setUp()
        self.first, self.second = self.tmp / "first", self.tmp / "second"
        write_root(self.first, FIRST)
        write_root(self.second, SECOND)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.con = db.connect(self.ws)
        self.addCleanup(self.con.close)
        db.init_schema(self.con)
        self.con.commit()
        self.seed(self.first)
        self.seed(self.second)  # the later copies become current (owner: repro-r1)
        episode_alias.record(self.con, "b1", "s2", actor="operator:test")
        self.con.commit()
        screen_corpus(self.con, self.corpus, drop=())
        jev_rescreen.import_results(self.con, self.corpus, dry_run=False, run_id="rs-1")
        self.con.commit()
        coverage_import.run(self.con, dry_run=False, run_id="cov-1")
        self.con.commit()
        self.cid = seed_campaign(self.con)
        self.placements = {ep: add_placement(self.con, self.cid, f"p-{ep}", episode_id=ep)
                           for ep in ("s1", "s2", "s3")}
        self.con.commit()

    def seed(self, root):
        out = seed_src.seed_all(root, self.con, "test")
        self.con.commit()
        return out

    def pins(self):
        return dict(self.con.execute("SELECT id, transcript_version_id FROM episodes"))

    def state(self):
        rows = customer._customer_ad_rows(self.con, None, self_promo="include")
        index = ad_breaks.scope_index(self.con)
        draft = campaigns.build_customer_draft(self.con, self.cid)
        outcomes = {}
        for ep, pid in self.placements.items():
            cov = draft["coverage_by_placement"][pid]
            ev = draft["allocation"]["evaluations"][pid]
            outcomes[ep] = (cov["state"], ev["outcome"], cov.get("waiting_for_review"))
        current = {}
        for ep, tv in self.pins().items():
            for stage in coverage.REQUIRED_STAGES:
                intervals, _failed = coverage.attestation_union(
                    self.con, ep, coverage.STAGE_GROUPS[stage], source_identity=tv)
                current[(ep, stage)] = sorted(set(map(tuple, intervals)))
        return {"pins": self.pins(),
                "episodes": {r[0]: tuple(r[1:]) for r in self.con.execute(
                    "SELECT id, duration_min, revision, alignment, kind, alias_of,"
                    " show_title, ep_title FROM episodes")},
                "reads": {r["id"]: (r["transcript_version_id"], r["source_record_key"])
                          for r in rows},
                "breaks": len({b["id"] for b in index.values()}),
                "outcomes": outcomes, "coverage": current}

    def test_fixture_matches_the_owner_workspace(self):
        s = self.state()
        version_no = dict(self.con.execute(
            "SELECT e.id, sv.version_no FROM episodes e JOIN source_versions sv"
            " ON sv.id=e.transcript_version_id"))
        self.assertEqual((version_no["s1"], version_no["s2"], version_no["s3"]), (2, 2, 1))
        durations = dict(self.con.execute("SELECT id, duration_min FROM episodes"))
        self.assertEqual((durations["s2"], durations["s3"]), (3.2, 3.0))
        self.assertEqual(self.con.execute("SELECT alias_of FROM episodes WHERE id='b1'"
                                          ).fetchone()[0], "s2")
        self.assertTrue(s["reads"])
        self.assertEqual(s["outcomes"]["s2"][0], "complete")
        self.assertEqual(s["outcomes"]["s3"][0], "complete")

    def test_reseed_from_the_first_root_keeps_every_current_copy(self):
        before = self.state()
        self.seed(self.first)
        after = self.state()
        self.assertEqual(after["pins"], before["pins"])
        self.assertEqual(after["episodes"], before["episodes"])  # duration from the kept copy
        self.assertEqual(after["reads"], before["reads"])
        self.assertEqual(after["breaks"], before["breaks"])
        self.assertEqual(after["coverage"], before["coverage"])
        self.assertEqual(after["outcomes"], before["outcomes"])
        self.assertEqual(quotes_off_their_copy(self.con), [])
        for ep, (state, _outcome, _waiting) in after["outcomes"].items():
            self.assertNotEqual(state, "unknown", ep)  # "Coverage not checked"
        # The older copy stays an earlier copy; the current one stays active.
        active = dict(self.con.execute(
            "SELECT source_key, id FROM source_versions WHERE is_active=1"
            " AND source_key LIKE 'transcript:%'"))
        for ep, tv in after["pins"].items():
            if tv is not None:
                self.assertEqual(active[f"transcript:{ep}"], tv, ep)

    def test_reseed_never_attests_the_current_copy_from_an_older_file(self):
        self.seed(self.first)
        older = {r["id"] for r in self.con.execute(
            "SELECT sv.id FROM source_versions sv JOIN episodes e"
            " ON sv.source_key='transcript:'||e.id WHERE sv.id IS NOT e.transcript_version_id")}
        pins = self.pins()
        for row in self.con.execute(
                "SELECT episode_id, source_pin, source_identity FROM coverage_attestations"
                " WHERE producer='seed-import' AND stage='transcribe'"):
            if row["source_identity"] == pins.get(row["episode_id"]):
                self.assertNotIn(int(row["source_pin"].split(":")[1]), older, dict(row))

    def test_second_reseed_is_a_no_op(self):
        self.seed(self.first)
        state = self.state()
        counts = lambda: {t: self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                          for t in ("source_versions", "evidence", "episode_history",
                                    "coverage_attestations")}
        before = counts()
        revisions = dict(self.con.execute("SELECT id, revision FROM episodes"))
        last_audit = self.con.execute("SELECT max(id) FROM audit").fetchone()[0]
        self.seed(self.first)
        self.assertEqual(counts(), before)
        self.assertEqual(dict(self.con.execute("SELECT id, revision FROM episodes")), revisions)
        actions = {r[0] for r in self.con.execute("SELECT action FROM audit WHERE id>?",
                                                  (last_audit,))}
        self.assertLessEqual(actions, {"seed", "jev_carry_forward"})
        self.assertEqual(self.state(), state)

    def test_a_copy_the_workspace_never_held_is_newer_and_wins(self):
        pins = self.pins()
        newest = later(later(later(segs(4))))
        (self.first / "transcripts/s1.json").write_text(json.dumps({"segments": newest}))
        self.seed(self.first)
        after = self.pins()
        row = self.con.execute("SELECT version_no, is_active, payload FROM source_versions"
                               " WHERE id=?", (after["s1"],)).fetchone()
        self.assertNotEqual(after["s1"], pins["s1"])
        self.assertEqual(row["version_no"], 3)  # MAX(version_no) + 1: newer than every copy
        self.assertEqual(row["is_active"], 1)
        self.assertEqual(json.loads(row["payload"])["segments"], newest)
        self.assertEqual({k: v for k, v in after.items() if k != "s1"},
                         {k: v for k, v in pins.items() if k != "s1"})

    def test_a_held_copy_newer_than_the_pin_still_wins(self):
        # The pin sits on an earlier copy; the root offers the later one the
        # workspace already holds (higher version_no): it becomes current.
        v = dict(self.con.execute("SELECT version_no, id FROM source_versions"
                                  " WHERE source_key='transcript:s2'"))
        self.con.execute("UPDATE episodes SET transcript_version_id=? WHERE id='s2'", (v[1],))
        self.con.execute("UPDATE source_versions SET is_active=(id=?)"
                         " WHERE source_key='transcript:s2'", (v[1],))
        self.con.commit()
        self.seed(self.second)
        self.assertEqual(self.pins()["s2"], v[2])


def quotes_off_their_copy(con):
    """ad_reads rows whose quote is not the text of the copy their evidence
    row names, at the read's span."""
    from earwitness.app.citations import _PinnedTranscript
    bad = []
    for r in con.execute("SELECT ar.evidence_id, ar.quote, ar.start_s, ar.end_s, sv.payload"
                         " FROM ad_reads ar JOIN evidence e ON e.id=ar.evidence_id"
                         " JOIN source_versions sv ON sv.id=e.transcript_version_id"
                         " WHERE ar.start_s IS NOT NULL AND ar.end_s IS NOT NULL"):
        want = _PinnedTranscript(json.loads(r["payload"])["segments"]).text(
            r["start_s"], r["end_s"])[:2000]
        if r["quote"] != want:
            bad.append((r["evidence_id"], r["quote"], want))
    return bad


class QuotesComeFromTheRowsCopyTest(Base):
    """A read's quote is cut from the transcript copy its evidence row names,
    also when the seed keeps a newer current copy (s1's later copy differs in
    text at the detector span 60-80 s)."""

    def setUp(self):
        super().setUp()
        self.first, self.second = self.tmp / "first", self.tmp / "second"
        write_root(self.first, FIRST)
        write_root(self.second, SECOND)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.con = db.connect(self.ws)
        self.addCleanup(self.con.close)
        db.init_schema(self.con)
        self.con.commit()
        for root in (self.first, self.second, self.first):
            seed_src.seed_all(root, self.con, "test")
            self.con.commit()

    def test_every_quote_matches_its_rows_copy(self):
        rows = self.con.execute("SELECT count(*) FROM ad_reads ar JOIN evidence e"
                                " ON e.id=ar.evidence_id WHERE ar.record_key='det:c1'"
                                ).fetchone()[0]
        self.assertGreater(rows, 0)
        self.assertEqual(quotes_off_their_copy(self.con), [])

    def test_rebuild_from_workspace_cuts_quotes_from_each_rows_copy(self):
        from earwitness.app import ad_reads
        with db.transaction(self.con, write=True):
            ad_reads.rebuild_from_workspace(self.con)
        self.assertEqual(quotes_off_their_copy(self.con), [])


class SeedAttestsOnceTest(Base):
    """A rerun that proves nothing new records no attestation again; the
    first run's rows stay current. A later failure is superseded by a new
    seed proof (latest failure wins until then)."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        write_root(self.root, SECOND)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.con = db.connect(self.ws)
        self.addCleanup(self.con.close)
        db.init_schema(self.con)
        self.con.commit()

    def seed(self):
        seed_src.seed_all(self.root, self.con, "test")
        self.con.commit()

    def rows(self):
        return [dict(r) for r in self.con.execute(
            "SELECT id, episode_id, stage, source_pin, source_identity, succ_end_s, status"
            " FROM coverage_attestations WHERE producer='seed-import' ORDER BY id")]

    def test_second_seed_records_no_duplicate_and_first_rows_stay_current(self):
        self.seed()
        first = self.rows()
        self.assertTrue(any(r["stage"] == "screen" for r in first))
        self.assertTrue(any(r["stage"] == "transcribe" for r in first))
        self.seed()
        self.assertEqual(self.rows(), first)
        pins = dict(self.con.execute("SELECT id, transcript_version_id FROM episodes"))
        for r in first:
            self.assertEqual(r["source_identity"], pins[r["episode_id"]])
            stage = {"transcribe": "transcription", "screen": "screening"}[r["stage"]]
            intervals, failed = coverage.attestation_union(
                self.con, r["episode_id"], coverage.STAGE_GROUPS[stage],
                source_identity=pins[r["episode_id"]])
            self.assertIsNone(failed)
            self.assertIn((0.0, r["succ_end_s"]), [tuple(i) for i in intervals])

    def test_a_later_failure_is_superseded_by_the_next_seed(self):
        self.seed()
        tv = self.con.execute("SELECT transcript_version_id FROM episodes WHERE id='s1'"
                              ).fetchone()[0]
        coverage.record_attestation(self.con, "s1", "transcribe", (0.0, 240.0), (0.0, 1.0),
                                    source_pin=f"source-version:{tv}", producer="test",
                                    actor="test", status="failed")
        self.con.commit()
        before = len(self.rows())
        self.seed()
        self.assertEqual(len(self.rows()), before + 1)
        _intervals, failed = coverage.attestation_union(self.con, "s1", ("transcribe",),
                                                        source_identity=tv)
        self.assertIsNone(failed)


class FreshSeedUnchangedTest(Base):
    """A first seed and a plain reseed from the same root behave as before."""

    def test_first_seed_pins_the_offered_copies_and_reseed_keeps_them(self):
        root = self.tmp / "root"
        write_root(root, FIRST)
        ws = self.tmp / "ws"
        ws.mkdir()
        con = db.connect(ws)
        self.addCleanup(con.close)
        db.init_schema(con)
        con.commit()
        seed_src.seed_all(root, con, "test")
        con.commit()
        pins = dict(con.execute("SELECT id, transcript_version_id FROM episodes"))
        self.assertIsNone(pins["s3"])  # no transcript offered, none held
        self.assertTrue(all(pins[e] for e in ("s1", "s2", "b1")))
        seed_src.seed_all(root, con, "test")
        con.commit()
        self.assertEqual(dict(con.execute("SELECT id, transcript_version_id FROM episodes")), pins)


if __name__ == "__main__":
    import unittest
    unittest.main()
