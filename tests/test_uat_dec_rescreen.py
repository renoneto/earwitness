"""R1-06 option A: re-screen each episode's CURRENT transcript copy with Jev.

The export writes every current copy (segments verbatim, plus which copy it
is) into a corpus folder that `corpus_run.py jev-backfill` screens; each Jev
result records the copy it read (transcript_version_id, segment count,
segments digest) so a later import can tie it to that copy and refuse any
other. Offline: Jev is a fake transport. Nothing here calls TypeSafe.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

from earwitness.app import (ad_reads, campaigns, citations, coverage, coverage_import,
                            customer, db, jev_rescreen, transcript_index)
from earwitness.pipeline import corpus as cr
from earwitness.pipeline import jev_screen as js
from tests.backend_fixtures import make_file_workspace, record_screening
from tests.test_customer_campaigns import add_placement, seed_campaign
from tests.test_jev_screen import fake_transport, sponsor_score
from tests.test_uat_dec_cov import ALL_STAGES, seed_legacy_episode

REPO = Path(__file__).resolve().parent.parent


def segs(minutes, sponsor_minute=1):
    return [{"start": m * 60.0 + k * 20.0, "end": m * 60.0 + k * 20.0 + 20.0,
             "text": ("this episode is sponsored by quionce go to quionce dot com"
                      if m == sponsor_minute else f"talk {m} {k}")}
            for m in range(minutes) for k in range(3)]


def add_copy(con, ep_id, segments, version_no=1, active=1, extra=None):
    payload = {"segments": segments, **(extra or {})}
    return con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"transcript:{ep_id}", version_no, f"sha-{ep_id}-{version_no}", json.dumps(payload),
         active, db.utcnow())).lastrowid


def add_episode(con, ep_id, tv, duration_min, fetch_date="2026-09-21", kind="supplied"):
    con.execute(
        "INSERT INTO episodes (id, kind, show_title, ep_title, duration_min, fetch_date,"
        " transcript_version_id, revision, updated_at) VALUES (?, ?, 'Show', 'Ep', ?, ?, ?,"
        " 1, ?)", (ep_id, kind, duration_min, fetch_date, tv, db.utcnow()))


def digest(segments):
    return hashlib.sha256(json.dumps(segments, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".tmp-rescreen-test-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.corpus = self.tmp / "corpus"


class ScreenRecordsTheCopyTest(Base):
    def test_screen_episode_records_segment_count_and_digest(self):
        s = segs(3)
        res = js.screen_episode(s, transport=fake_transport(sponsor_score), episode_id="e1")
        self.assertEqual(res["segment_count"], len(s))
        self.assertEqual(res["segments_sha256"], digest(s))
        self.assertEqual(js.segments_digest(s), digest(s))

    def test_run_doc_carries_the_copy_keys(self):
        doc = {"episode_id": "e1", "model": "jev-1.13.0", "windows": [
            {"window_id": "window_000", "start_s": 0.0, "end_s": 60.0, "noul": 0.1}],
            "segment_count": 3, "segments_sha256": "abc", "transcript_version_id": 37}
        entry = cr.jev_results_doc([cr._jev_episode("e1", doc, 60.0)])["episodes"][0]
        self.assertEqual((entry["segment_count"], entry["segments_sha256"],
                          entry["transcript_version_id"]), (3, "abc", 37))


class ExportTest(Base):
    def setUp(self):
        super().setUp()
        self.con = make_file_workspace()
        self.addCleanup(self.con.close)
        old = add_copy(self.con, "s1", segs(2), version_no=1, active=0)
        self.cur = segs(4)
        self.tv1 = add_copy(self.con, "s1", self.cur, version_no=2,
                            extra={"fetch_date": "2026-09-25",
                                   "fetch_date_source": "repro-r1:download-date"})
        self.assertNotEqual(old, self.tv1)
        add_episode(self.con, "s1", self.tv1, 4.0)
        self.tv2 = add_copy(self.con, "upload:p", segs(2, sponsor_minute=9))
        add_episode(self.con, "upload:p", self.tv2, 2.0, fetch_date=None, kind="upload")
        add_episode(self.con, "none", None, 3.0)
        self.con.commit()
        self.db_path = Path(self.con.execute("PRAGMA database_list").fetchone()[2])

    def export(self):
        return jev_rescreen.export_current_copies(self.con, self.corpus)

    def test_exports_current_copy_with_provenance_and_writes_nothing(self):
        before = self.db_path.read_bytes()
        rep = self.export()
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertEqual((rep["exported"], rep["segments"]), (2, len(self.cur) + 6))
        self.assertEqual(rep["skipped"], [{"episode_id": "none", "reason": "no transcript copy"}])
        doc = json.loads((self.corpus / "transcripts" / "s1.json").read_text("utf-8"))
        self.assertEqual(doc["segments"], self.cur)
        self.assertEqual(doc["workspace_copy"]["transcript_version_id"], self.tv1)
        self.assertEqual(doc["workspace_copy"]["segment_count"], len(self.cur))
        self.assertEqual(doc["workspace_copy"]["segments_sha256"], digest(self.cur))
        man = json.loads((self.corpus / "manifest.json").read_text("utf-8"))
        row = next(e for e in man["episodes"] if e["episode_id"] == "s1")
        self.assertEqual((row["transcript_version_id"], row["segment_count"]),
                         (self.tv1, len(self.cur)))
        self.assertEqual(row["episode_fetch_date"], "2026-09-21")
        self.assertEqual(row["episode_fetch_date_source"], "episodes.fetch_date")
        self.assertEqual((row["transcript_fetch_date"], row["transcript_fetch_date_source"]),
                         ("2026-09-25", "repro-r1:download-date"))
        up = next(e for e in man["episodes"] if e["episode_id"] == "upload:p")
        self.assertIsNone(up["episode_fetch_date"])
        self.assertEqual(up["episode_fetch_date_source"], "unrecorded")
        self.assertIn("fetch_date", man)
        self.assertIn("fetch_date_source", man)

    def test_export_is_idempotent(self):
        self.export()
        files = {p: p.read_bytes() for p in (self.corpus / "transcripts").glob("*.json")}
        rep = self.export()
        self.assertEqual(rep["exported"], 2)
        self.assertEqual({p: p.read_bytes() for p in files}, files)
        con = sqlite3.connect(self.corpus / "queue.sqlite3")
        self.assertEqual(con.execute("SELECT count(*) FROM episodes").fetchone()[0], 2)
        con.close()

    def test_export_refuses_to_replace_a_screened_copy(self):
        self.export()
        (self.corpus / "jev").mkdir()
        (self.corpus / "jev" / "s1.json").write_text("{}")
        tv3 = add_copy(self.con, "s1", segs(5), version_no=3)
        self.con.execute("UPDATE episodes SET transcript_version_id=? WHERE id='s1'", (tv3,))
        self.con.commit()
        with self.assertRaises(jev_rescreen.ExportError):
            self.export()

    def test_dry_run_estimates_every_exported_copy(self):
        self.export()
        rep = cr.jev_backfill(self.corpus, dry_run=True, log=lambda m: None)
        self.assertEqual(rep["episodes"], 2)
        self.assertGreater(rep["input_tokens_est"], 0)

    def test_backfill_screens_the_copy_and_records_it_once(self):
        self.export()
        calls = []
        rep = cr.jev_backfill(self.corpus, transport=fake_transport(sponsor_score, calls),
                              log=lambda m: None)
        self.assertEqual((rep["screened"], rep["failed"]), (2, 0))
        res = json.loads((self.corpus / "jev" / "s1.json").read_text("utf-8"))
        self.assertEqual(res["transcript_version_id"], self.tv1)
        self.assertEqual(res["segment_count"], len(self.cur))
        self.assertEqual(res["segments_sha256"], digest(self.cur))
        self.assertEqual(res["windows"][-1]["end_s"], self.cur[-1]["end"])
        n = len(calls)
        rep2 = cr.jev_backfill(self.corpus, transport=fake_transport(sponsor_score, calls),
                               log=lambda m: None)
        self.assertEqual((rep2["screened"], rep2["cost_usd"], len(calls)), (0, 0.0, n))

    def test_backfill_refuses_an_edited_copy(self):
        self.export()
        p = self.corpus / "transcripts" / "s1.json"
        doc = json.loads(p.read_text("utf-8"))
        doc["segments"][0]["text"] = "edited"
        p.write_text(json.dumps(doc), "utf-8")
        calls = []
        rep = cr.jev_backfill(self.corpus, transport=fake_transport(sponsor_score, calls),
                              log=lambda m: None)
        self.assertEqual((rep["screened"], rep["failed"]), (1, 1))
        self.assertFalse((self.corpus / "jev" / "s1.json").exists())


class CorpusTranscriptsUnchangedTest(Base):
    def test_plain_corpus_transcript_still_needs_stt_fields(self):
        (self.corpus / "transcripts").mkdir(parents=True)
        con = cr.connect(self.corpus)
        cr.ensure_schema(con)
        con.execute("INSERT INTO episodes (episode_id, feed_url, stage, duration_s)"
                    " VALUES ('e1', 'f', 'done', 240)")
        con.close()
        (self.corpus / "transcripts" / "e1.json").write_text(json.dumps({"segments": segs(4)}))
        rep = cr.jev_backfill(self.corpus, transport=fake_transport(sponsor_score),
                              log=lambda m: None)
        self.assertEqual((rep["screened"], rep["failed"]), (0, 1))


# ---------------------------------------------------------------- import-jev

def build_workspace(con):
    """s1: current copy tv1 (sponsor in minute 1), screened by the old run on
    an older copy; s2: current copy, never screened, no ad; s3: screened by
    the old run on its current copy (the old run never recorded which copy).
    Commits."""
    con.execute("INSERT INTO source_versions (source_key, version_no, sha256, payload,"
                " is_active, created_at) VALUES ('episodes.json', 1, 'eps', ?, 1, ?)",
                (json.dumps([{"ep_id": e, "show_title": "Show", "ep_title": "Ep",
                              "duration_min": d, "fetch_date": "2026-09-21"}
                             for e, d in (("s1", 4.0), ("s2", 3.0), ("s3", 2.0))]),
                 db.utcnow()))
    ids = {"old_s1": add_copy(con, "s1", segs(3), version_no=1, active=0),
           "tv1": add_copy(con, "s1", segs(4), version_no=2),
           "tv2": add_copy(con, "s2", segs(3, sponsor_minute=9)),
           "tv3": add_copy(con, "s3", segs(2))}
    for ep, tv, dm in (("s1", "tv1", 4.0), ("s2", "tv2", 3.0), ("s3", "tv3", 2.0)):
        con.execute(
            "INSERT INTO episodes (id, kind, show_title, ep_title, pub_date, duration_min,"
            " fetch_date, transcript_version_id, revision, updated_at) VALUES"
            " (?, 'supplied', 'Show', 'Ep', '2026-03-10', ?, '2026-09-21', ?, 1, ?)",
            (ep, dm, ids[tv], db.utcnow()))
    old_run = {"run_id": "jev-ad-triage-20260921", "model_returned": "jev-1.13.0", "episodes": [
        {"episode_id": "s1", "segment_count": 9, "answers": {
            "window_000": {"noul": 0.1, "start_s": 0.0, "end_s": 60.0},
            "window_001": {"noul": 0.95, "start_s": 60.0, "end_s": 120.0},
            "window_002": {"noul": 0.1, "start_s": 120.0, "end_s": 180.0}}},
        {"episode_id": "s3", "segment_count": 6, "answers": {
            "window_000": {"noul": 0.9, "start_s": 0.0, "end_s": 60.0},
            "window_001": {"noul": 0.1, "start_s": 60.0, "end_s": 120.0}}}]}
    ids["old_run"] = con.execute(
        "INSERT INTO source_versions (source_key, version_no, sha256, payload, is_active,"
        " created_at) VALUES ('jev-run', 1, 'oldrun', ?, 1, ?)",
        (json.dumps(old_run), db.utcnow())).lastrowid
    for ev_id, ep, tv, start in (("jev:old-s1", "s1", ids["old_s1"], 60.0),
                                 ("jev:old-s3", "s3", ids["tv3"], 0.0)):
        key = f"jev:{ep}:window_00{int(start // 60)}"
        con.execute(
            "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
            " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
            " source_record_key) VALUES (?, 'jev', ?, ?, ?, ?, 'old window', '{}', 1, ?, ?, ?)",
            (ev_id, ids["old_run"], ep, start, start + 60.0, db.utcnow(), tv, key))
    with db.transaction(con, write=True):
        ad_reads.rebuild_from_workspace(con)
        for ep in ("s1", "s2", "s3"):
            citations.publish_source_state(con, kind="episode", record_id=ep, actor="fixture")
    con.commit()
    return ids


def screen_corpus(con, corpus, drop=("s3",)):
    """Export + fake-Jev backfill; `drop`: episodes left without a result."""
    jev_rescreen.export_current_copies(con, corpus)
    cr.jev_backfill(corpus, transport=fake_transport(sponsor_score), log=lambda m: None)
    for ep in drop:
        (corpus / "jev" / f"{ep}.json").unlink()


def jev_attestations(con):
    return [dict(r) for r in con.execute(
        "SELECT * FROM coverage_attestations WHERE producer LIKE 'jev-rescreen:%' ORDER BY id")]


class _ImportBase(Base):
    def setUp(self):
        super().setUp()
        self.con = make_file_workspace()
        self.addCleanup(self.con.close)
        self.ids = build_workspace(self.con)
        screen_corpus(self.con, self.corpus)
        self.db_path = Path(self.con.execute("PRAGMA database_list").fetchone()[2])

    def run_import(self, dry_run=False, run_id="rs-1"):
        out = jev_rescreen.import_results(self.con, self.corpus, dry_run=dry_run,
                                          run_id=run_id, actor="operator")
        self.con.commit()
        return out

    def active_run(self):
        row = self.con.execute("SELECT id, payload FROM source_versions WHERE"
                               " source_key='jev-run' AND is_active=1").fetchall()
        self.assertEqual(len(row), 1)
        return row[0]["id"], json.loads(row[0]["payload"])


class ImportJevTest(_ImportBase):
    def test_dry_run_writes_nothing_and_reports_each_episode(self):
        before = self.db_path.read_bytes()
        out = self.run_import(dry_run=True)
        self.assertEqual(self.db_path.read_bytes(), before)
        by = {e["episode_id"]: e for e in out["episodes"]}
        self.assertEqual((by["s1"]["verdict"], by["s2"]["verdict"]), ("import", "import"))
        self.assertEqual(by["s1"]["flagged_windows"], 1)
        self.assertEqual(out["recorded"], 0)
        self.assertNotIn("s3", by)  # no re-screen result: its old entry is kept
        self.assertEqual(out["kept_entries"], ["s3"])

    def test_import_adds_a_run_version_tied_to_the_current_copies(self):
        out = self.run_import()
        vid, run = self.active_run()
        self.assertNotEqual(vid, self.ids["old_run"])
        self.assertEqual(out["jev_run_version_id"], vid)
        entries = {e["episode_id"]: e for e in run["episodes"]}
        self.assertEqual(set(entries), {"s1", "s2", "s3"})
        self.assertEqual((entries["s1"]["transcript_version_id"], entries["s1"]["segment_count"],
                          entries["s1"]["segments_sha256"]),
                         (self.ids["tv1"], 12, digest(segs(4))))
        self.assertEqual(entries["s1"]["run_id"], "rs-1")
        # The old run's entry for an episode not re-screened stays verbatim.
        self.assertEqual(entries["s3"], {"episode_id": "s3", "segment_count": 6, "answers": {
            "window_000": {"noul": 0.9, "start_s": 0.0, "end_s": 60.0},
            "window_001": {"noul": 0.1, "start_s": 60.0, "end_s": 120.0}}})

    def test_flagged_windows_enter_as_unnamed_unreviewed_evidence_on_the_current_copy(self):
        self.run_import()
        vid, _run = self.active_run()
        rows = self.con.execute("SELECT * FROM evidence WHERE source='jev' AND episode_id='s1'"
                                " AND transcript_version_id=?", (self.ids["tv1"],)).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # The plain key belongs to s1's earlier-copy row: the new row is
        # keyed by its copy, so the earlier read is never taken for replaced.
        self.assertEqual((row["source_record_key"], row["orig_start_s"], row["orig_end_s"],
                          row["source_version_id"]),
                         (f"jev:s1:window_001@tv{self.ids['tv1']}", 60.0, 120.0, vid))
        self.assertIn("sponsored by quionce", row["excerpt"])
        self.assertEqual(json.loads(row["attrs"])["run_id"], "rs-1")
        for table in ("reviews", "brand_assocs"):
            self.assertEqual(self.con.execute(f"SELECT count(*) FROM {table} WHERE evidence_id=?",
                                              (row["id"],)).fetchone()[0], 0)
        reads = {r["evidence_id"]: r for r in self.con.execute("SELECT * FROM ad_reads")}
        self.assertEqual((reads[row["id"]]["kind"], reads[row["id"]]["brand_suggested"]),
                         ("jev", None))
        self.assertNotIn("jev:old-s1", reads)   # earlier copy: not a current read
        self.assertIn("jev:old-s3", reads)      # kept entry: its row stays visible
        self.assertTrue(self.con.execute("SELECT count(*) FROM source_states WHERE"
                                         " kind='evidence' AND record_id=?",
                                         (row["id"],)).fetchone()[0])
        scr = ad_reads.screening(self.con, "s2")
        self.assertEqual((scr["jev_screened"], scr["jev_windows"]), (True, 0))

    def test_publication_is_attested_over_the_screened_current_copy(self):
        self.run_import()
        rows = jev_attestations(self.con)
        self.assertEqual({r["episode_id"] for r in rows}, {"s1", "s2"})
        for r in rows:
            self.assertEqual(r["stage"], "observation_publication")
            self.assertEqual(r["producer"], "jev-rescreen:rs-1")
        s1 = [r for r in rows if r["episode_id"] == "s1"]
        self.assertEqual([(r["succ_start_s"], r["succ_end_s"]) for r in s1], [(0.0, 240.0)])
        self.assertEqual(s1[0]["source_identity"], self.ids["tv1"])
        audits = self.con.execute("SELECT detail FROM audit WHERE action='jev_import'").fetchall()
        self.assertEqual(len(audits), 1)
        detail = json.loads(audits[0][0])
        self.assertEqual({e["episode_id"] for e in detail["episodes"]}, {"s1", "s2"})

    def test_rerun_records_nothing_and_writes_nothing(self):
        self.run_import()
        counts = lambda: tuple(self.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                               for t in ("source_versions", "evidence", "coverage_attestations",
                                         "audit"))
        before = counts()
        out = self.run_import(run_id="rs-2")
        self.assertEqual((out["recorded"], out["created_evidence"]), (0, 0))
        self.assertEqual(counts(), before)

    def test_result_for_another_copy_is_refused(self):
        p = self.corpus / "jev" / "s2.json"
        doc = json.loads(p.read_text("utf-8"))
        doc["transcript_version_id"] = self.ids["old_s1"]
        p.write_text(json.dumps(doc), "utf-8")
        q = self.corpus / "jev" / "s1.json"
        doc = json.loads(q.read_text("utf-8"))
        doc["segments_sha256"] = "0" * 64  # same segment count, other words
        q.write_text(json.dumps(doc), "utf-8")
        out = self.run_import()
        by = {e["episode_id"]: e for e in out["episodes"]}
        self.assertEqual(by["s2"]["verdict"], "skip")
        self.assertIn("another transcript copy", by["s2"]["reason"])
        self.assertEqual(by["s1"]["verdict"], "skip")
        self.assertIn("segments differ", by["s1"]["reason"])
        self.assertEqual(out["recorded"], 0)
        self.assertEqual(jev_attestations(self.con), [])

    def test_unchanged_window_on_an_unchanged_copy_keeps_its_row_and_review(self):
        # s3 re-screened too: its window_000 (0-60, same copy) is the old row.
        self.con.execute("INSERT INTO reviews (evidence_id, judgment, eff_start_s, eff_end_s,"
                         " notes, revision, updated_at) VALUES"
                         " ('jev:old-s3', 'ad', 0.0, 60.0, '', 1, ?)", (db.utcnow(),))
        self.con.commit()
        shutil.rmtree(self.corpus)
        jev_rescreen.export_current_copies(self.con, self.corpus)
        rep = cr.jev_backfill(self.corpus, transport=fake_transport(
            lambda w: 0.9 if w["start_s"] == 0.0 else 0.02), log=lambda m: None)
        self.assertEqual(rep["failed"], 0)
        out = self.run_import()
        s3_rows = self.con.execute("SELECT id FROM evidence WHERE episode_id='s3'").fetchall()
        self.assertEqual([r[0] for r in s3_rows], ["jev:old-s3"])
        self.assertIn("jev:old-s3", {r[0] for r in self.con.execute("SELECT evidence_id FROM ad_reads")})
        self.assertEqual(out["reused_evidence"], 1)


class NoReadDisappearsTest(_ImportBase):
    """Every evidence row active before import-jev stays active and counted
    after it, with the same id and the same copy (D-1: no read is hidden;
    earlier-copy reads stay earlier-copy reads; saved items, campaign links
    and alerts may cite them)."""

    def customer_rows(self):
        return {r["id"]: r["transcript_version_id"]
                for r in customer._customer_ad_rows(self.con, None, self_promo="include")}

    def test_every_active_row_stays_active_with_its_copy(self):
        before = self.customer_rows()
        self.assertIn("jev:old-s1", before)   # earlier-copy read of a re-screened episode
        self.assertIn("jev:old-s3", before)
        self.run_import()
        after = self.customer_rows()
        for eid, tv in before.items():
            self.assertEqual(after.get(eid, "missing"), tv, eid)
        vid, _run = self.active_run()
        new = set(after) - set(before)
        self.assertEqual(len(new), 1)  # s1's flagged window on its current copy
        self.assertEqual(after[new.pop()], self.ids["tv1"])
        self.assertTrue(all(jev_rescreen._src_visible(self.con, eid, vid) for eid in before))

    def test_a_current_copy_window_no_longer_flagged_stays_active(self):
        before = self.customer_rows()
        shutil.rmtree(self.corpus)
        jev_rescreen.export_current_copies(self.con, self.corpus)
        rep = cr.jev_backfill(self.corpus, transport=fake_transport(lambda w: 0.02),
                              log=lambda m: None)
        self.assertEqual(rep["failed"], 0)
        out = self.run_import()
        by = {e["episode_id"]: e for e in out["episodes"]}
        self.assertEqual(by["s3"]["verdict"], "import")
        self.assertEqual(self.customer_rows(), before)


class CoverageFromTheRescreenTest(_ImportBase):
    def cover(self):
        for tv in ("tv1", "tv2", "tv3"):
            transcript_index.index_versions(self.con, [self.ids[tv]])
        self.con.commit()
        self.run_import()
        out = coverage_import.run(self.con, dry_run=False, run_id="cov-1")
        self.con.commit()
        return {e["episode_id"]: e for e in out["episodes"]}

    def test_screen_attested_from_the_matching_run_entry(self):
        rows = self.cover()
        # s1 still has its earlier-copy window record: it never blocks.
        self.assertEqual(rows["s1"]["stages"]["screen"]["verdict"], "record")
        # s2 has no flagged window at all: the run entry is the evidence.
        self.assertEqual(rows["s2"]["stages"]["screen"]["verdict"], "record")
        # s3: the old run never recorded which copy it read.
        self.assertEqual(rows["s3"]["stages"]["screen"]["verdict"], "skip")
        self.assertIn("transcript copy", rows["s3"]["stages"]["screen"]["reason"])
        pub = rows["s1"]["stages"]["observation_publication"]
        self.assertEqual(pub["verdict"], "skip")
        self.assertIn("jev-rescreen:rs-1", pub["reason"])

    def cover_without_screening_row(self, edit_run=None, screening=None):
        """s2 as a bench/upload-like episode: no episode_screening row (or the
        given one), after the import."""
        for tv in ("tv1", "tv2", "tv3"):
            transcript_index.index_versions(self.con, [self.ids[tv]])
        self.run_import()
        self.con.execute("DELETE FROM episode_screening WHERE episode_id='s2'")
        if screening:
            self.con.execute("INSERT INTO episode_screening (episode_id, jev_screened,"
                             " stopped_partway, coverage_tier) VALUES ('s2', 1, ?, ?)", screening)
        if edit_run:
            vid, run = self.active_run()
            for e in run["episodes"]:
                if e["episode_id"] == "s2":
                    edit_run(e)
            self.con.execute("UPDATE source_versions SET payload=? WHERE id=?",
                             (json.dumps(run), vid))
        self.con.commit()
        out = coverage_import.run(self.con, dry_run=False, run_id="cov-1")
        self.con.commit()
        return {e["episode_id"]: e for e in out["episodes"]}["s2"]["stages"]["screen"]

    def test_run_entry_alone_attests_screen_when_no_screening_row(self):
        stage = self.cover_without_screening_row()
        self.assertEqual(stage["verdict"], "record")
        self.assertEqual(stage["intervals"], [(0.0, 180.0)])
        self.assertTrue(stage["basis"].startswith("run entry (no episode_screening row)"))
        audit = json.loads(self.con.execute("SELECT detail FROM audit WHERE"
                                            " action='coverage_import'").fetchone()[0])
        s2 = next(e for e in audit["episodes"] if e["episode_id"] == "s2")
        self.assertTrue(s2["stages"]["screen"]["basis"].startswith(
            "run entry (no episode_screening row)"))
        s1 = next(e for e in audit["episodes"] if e["episode_id"] == "s1")
        self.assertTrue(s1["stages"]["screen"]["basis"].startswith(coverage_import.SCREEN_BASIS))

    def test_run_entry_with_a_gap_is_not_enough(self):
        stage = self.cover_without_screening_row(
            edit_run=lambda e: e["answers"].pop("window_001"))
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("gap", stage["reason"])

    def test_run_entry_of_another_model_is_not_enough(self):
        stage = self.cover_without_screening_row(edit_run=lambda e: e.update(model="jev-1.12.0"))
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("jev-1.12.0", stage["reason"])

    def test_screening_row_saying_stopped_partway_still_blocks(self):
        stage = self.cover_without_screening_row(screening=(1, "full"))
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("stopped partway", stage["reason"])

    def test_screening_row_of_another_tier_still_blocks(self):
        stage = self.cover_without_screening_row(screening=(0, "transcript"))
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("tier", stage["reason"])

    def test_same_segment_count_with_other_words_is_never_enough(self):
        self.run_import()
        vid, run = self.active_run()
        for e in run["episodes"]:
            if e["episode_id"] == "s2":
                e["segments_sha256"] = "f" * 64
        self.con.execute("UPDATE source_versions SET payload=? WHERE id=?", (json.dumps(run), vid))
        self.con.commit()
        transcript_index.index_versions(self.con, [self.ids["tv2"]])
        self.con.commit()
        rows = {e["episode_id"]: e for e in coverage_import.run(
            self.con, dry_run=True, run_id="cov-1")["episodes"]}
        stage = rows["s2"]["stages"]["screen"]
        self.assertEqual(stage["verdict"], "skip")
        self.assertIn("another transcript copy", stage["reason"])

    def test_placements_reach_a_result_from_their_own_evidence(self):
        self.cover()
        cid = seed_campaign(self.con)
        p1 = add_placement(self.con, cid, "p1", episode_id="s1")
        p2 = add_placement(self.con, cid, "p2", episode_id="s2")
        p3 = add_placement(self.con, cid, "p3", episode_id="s3")
        draft = campaigns.build_customer_draft(self.con, cid)
        cov = draft["coverage_by_placement"]
        self.assertEqual((cov[p1]["state"], cov[p2]["state"]), ("complete", "complete"))
        self.assertNotEqual(cov[p3]["state"], "complete")
        # s1's flagged window is unnamed and unreviewed: waiting, never "no match".
        self.assertEqual(cov[p1]["waiting_for_review"], 1)
        self.assertEqual(cov[p2]["waiting_for_review"], 0)
        evals = draft["allocation"]["evaluations"]
        self.assertEqual(evals[p1]["outcome"], "unreviewed_detections_in_scope")
        self.assertEqual(evals[p2]["outcome"], "no_observation_in_covered_sources")


class SharedToleranceTest(unittest.TestCase):
    def placement(self, extent):
        con = make_file_workspace()
        self.addCleanup(con.close)
        tv = seed_legacy_episode(con, "ep-1", 60.0, extent)
        for stage in ALL_STAGES:
            coverage.record_attestation(con, "ep-1", stage, (0.0, 3600.0), (0.0, extent),
                                        source_pin=f"source-version:{tv}",
                                        producer="test-fixture", actor="owner")
        con.commit()
        record_screening(con, "ep-1")
        cid = seed_campaign(con)
        pid = add_placement(con, cid, "p1", episode_id="ep-1")
        return campaigns.build_customer_draft(con, cid)["coverage_by_placement"][pid]

    def test_one_constant(self):
        self.assertEqual(coverage.DURATION_TOLERANCE_S, 3.0)
        self.assertEqual(coverage_import.TOLERANCE_S, coverage.DURATION_TOLERANCE_S)

    def test_attested_copy_within_tolerance_of_the_rounded_duration_reads_complete(self):
        self.assertEqual(self.placement(3597.5)["state"], "complete")

    def test_shortfall_past_tolerance_stays_incomplete(self):
        # DET-PLAN item 6 rule: incomplete when more than 15 s short (16 s here).
        self.assertEqual(self.placement(3584.0)["state"], "incomplete")

    def test_proof_short_of_the_transcript_itself_stays_incomplete(self):
        # The tolerance covers only the rounded duration, never a transcript
        # the proof does not reach.
        con = make_file_workspace()
        self.addCleanup(con.close)
        tv = seed_legacy_episode(con, "ep-1", 60.0, 3601.2)
        for stage in ALL_STAGES:
            coverage.record_attestation(con, "ep-1", stage, (0.0, 3601.2), (0.0, 3599.0),
                                        source_pin=f"source-version:{tv}",
                                        producer="test-fixture", actor="owner")
        con.commit()
        cid = seed_campaign(con)
        pid = add_placement(con, cid, "p1", episode_id="ep-1")
        self.assertEqual(campaigns.build_customer_draft(con, cid)["coverage_by_placement"][pid]
                         ["state"], "incomplete")


class ImportJevCliTest(Base):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(REPO / "scripts"))
        import app_workspace
        self.app_workspace = app_workspace
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        con = db.connect(self.ws)
        db.init_schema(con)
        con.commit()
        build_workspace(con)
        screen_corpus(con, self.corpus)
        con.close()

    def cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.app_workspace.main(["import-jev", "--workspace", str(self.ws),
                                            "--corpus", str(self.corpus), *extra])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return {p.name: p.read_bytes() for p in sorted(self.ws.iterdir()) if p.is_file()
                and p.name not in ("workspace.lock", "workspace.sqlite3-shm")
                and not (p.name.endswith("-wal") and p.stat().st_size == 0)}

    def audits(self):
        con = sqlite3.connect(self.ws / "workspace.sqlite3")
        try:
            return con.execute("SELECT count(*) FROM audit WHERE action='jev_import'").fetchone()[0]
        finally:
            con.close()

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        code, out, err = self.cli("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out.lower())
        self.assertIn("s1", out)
        self.assertEqual(self.snapshot(), before)

    def test_run_refuses_while_server_runs(self):
        (self.ws / "server.pid").write_text(str(os.getpid()))
        code, _out, err = self.cli("--run-id", "cli-1")
        self.assertEqual(code, 1)
        self.assertIn("server", err)
        self.assertEqual(self.audits(), 0)

    def test_run_backs_up_first_then_rerun_is_a_noop(self):
        code, out, err = self.cli("--run-id", "cli-1")
        self.assertEqual(code, 0, err)
        backup = self.ws / "before-import-jev-cli-1.sqlite3"
        self.assertTrue(backup.is_file())
        self.assertEqual(self.audits(), 1)
        code, out, err = self.cli("--run-id", "cli-2")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded 0", out)
        self.assertFalse((self.ws / "before-import-jev-cli-2.sqlite3").exists())
        self.assertEqual(self.audits(), 1)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- review #4

from earwitness.app import import_sources as seed_src  # noqa: E402

JEV_RUN = "experiments/jev-ad-triage-20260921/results.json"
DET_OUT = "experiments/next-validation-detector/output/candidates_normalized.json"


def write_seed_root(root: Path, s2_minutes=3):
    """s1 (4 min, sponsor in minute 1) screened by an old-style run; s2
    (sponsor in minute 2) never screened."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(exist_ok=True)
    (root / "data/episodes.json").write_text(json.dumps([
        {"ep_id": "s1", "show_title": "Show", "ep_title": "One", "pub_date": "2026-03-10",
         "duration_min": 4.0, "fetch_date": "2026-09-21"},
        {"ep_id": "s2", "show_title": "Show", "ep_title": "Two", "pub_date": "2026-03-10",
         "duration_min": float(s2_minutes), "fetch_date": "2026-09-21"}]))
    (root / "transcripts/s1.json").write_text(json.dumps({"segments": segs(4)}))
    (root / "transcripts/s2.json").write_text(json.dumps({"segments": segs(s2_minutes, 2)}))
    (root / DET_OUT).parent.mkdir(parents=True, exist_ok=True)
    (root / DET_OUT).write_text("[]")
    (root / JEV_RUN).parent.mkdir(parents=True, exist_ok=True)
    (root / JEV_RUN).write_text(json.dumps({"run_id": "old", "episodes": [
        {"episode_id": "s1", "segment_count": 12, "answers": {
            "window_000": {"noul": 0.1, "start_s": 0.0, "end_s": 60.0},
            "window_001": {"noul": 0.95, "start_s": 60.0, "end_s": 120.0}}}]}))


class ReseedKeepsTheRescreenTest(Base):
    """R4-01: a seed after import-jev keeps the re-screen (same rows, ids,
    keys, run entries) for episodes whose current copy it leaves unchanged;
    screen and publication proofs count only while the active run holds the
    matching entry."""

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        write_seed_root(self.root)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.con = db.connect(self.ws)
        self.addCleanup(self.con.close)
        db.init_schema(self.con)
        self.con.commit()
        self.seed()
        screen_corpus(self.con, self.corpus, drop=())
        jev_rescreen.import_results(self.con, self.corpus, dry_run=False, run_id="rs-1")
        self.con.commit()
        coverage_import.run(self.con, dry_run=False, run_id="cov-1")
        self.con.commit()
        cid = seed_campaign(self.con)
        self.cid, self.p2 = cid, add_placement(self.con, cid, "p2", episode_id="s2")

    def seed(self):
        seed_src.seed_all(self.root, self.con, "test")
        self.con.commit()

    def rows(self):
        return {r["id"]: (r["transcript_version_id"], r["source_record_key"])
                for r in customer._customer_ad_rows(self.con, None, self_promo="include")}

    def placement(self):
        d = campaigns.build_customer_draft(self.con, self.cid)
        return d["coverage_by_placement"][self.p2], d["allocation"]["evaluations"][self.p2]

    def active_entries(self):
        row = self.con.execute("SELECT payload FROM source_versions WHERE source_key='jev-run'"
                               " AND is_active=1").fetchone()
        return {e["episode_id"]: e for e in json.loads(row[0])["episodes"]}

    def test_reseed_keeps_rows_entries_and_outcome(self):
        rows, (cov, ev) = self.rows(), self.placement()
        self.assertEqual((cov["state"], ev["outcome"]), ("complete", "unreviewed_detections_in_scope"))
        entries = self.active_entries()
        self.seed()
        self.assertEqual(self.rows(), rows)
        self.assertEqual(self.active_entries(), entries)
        cov2, ev2 = self.placement()
        self.assertEqual((cov2["state"], ev2["outcome"], cov2["waiting_for_review"]),
                         (cov["state"], ev["outcome"], cov["waiting_for_review"]))
        self.assertNotIn(ev2["outcome"], ("no_observation_in_covered_sources",))
        # A second seed and a rerun of import-jev change nothing.
        versions = self.con.execute("SELECT count(*) FROM source_versions").fetchone()[0]
        self.seed()
        out = jev_rescreen.import_results(self.con, self.corpus, dry_run=False, run_id="rs-2")
        self.con.commit()
        self.assertEqual(out["recorded"], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM source_versions").fetchone()[0],
                         versions)

    def test_changed_copy_is_not_carried_and_its_reads_stay(self):
        rows = self.rows()
        (self.root / "transcripts/s2.json").write_text(
            json.dumps({"segments": segs(3, 2) + [{"start": 180.0, "end": 190.0, "text": "x"}]}))
        self.seed()
        self.assertNotIn("transcript_version_id", self.active_entries().get("s2", {}))
        after = self.rows()
        for eid, val in rows.items():
            self.assertEqual(after.get(eid), val, eid)

    def test_proofs_count_only_while_the_active_run_holds_the_entry(self):
        old = self.con.execute("SELECT id FROM source_versions WHERE source_key='jev-run'"
                               " AND json_extract(payload, '$.run_id')='old'").fetchone()[0]
        self.con.execute("UPDATE source_versions SET is_active=(id=?) WHERE source_key='jev-run'",
                         (old,))
        self.con.commit()
        cov, ev = self.placement()
        self.assertNotEqual(cov["state"], "complete")
        self.assertNotIn(ev["outcome"], campaigns.NO_MATCH_OUTCOMES)


class ToleranceOnlyAtTheEndTest(unittest.TestCase):
    """R4-02 / R4-03."""

    def placement(self, duration_min, extent, intervals):
        con = make_file_workspace()
        self.addCleanup(con.close)
        tv = seed_legacy_episode(con, "ep-1", duration_min, extent)
        req = max(extent, duration_min * 60.0)
        for stage in ALL_STAGES:
            for s, e in intervals:
                coverage.record_attestation(con, "ep-1", stage, (0.0, req), (s, e),
                                            source_pin=f"source-version:{tv}",
                                            producer="test-fixture", actor="owner")
        con.commit()
        record_screening(con, "ep-1")
        cid = seed_campaign(con)
        pid = add_placement(con, cid, "p1", episode_id="ep-1")
        return campaigns.build_customer_draft(con, cid)["coverage_by_placement"][pid]["state"]

    def test_mid_gap_inside_tolerance_is_not_complete(self):
        self.assertEqual(self.placement(61.7, 3702.0, [(0.0, 1000.0), (1002.9, 3702.0)]),
                         "incomplete")

    def test_shortfall_at_the_end_inside_tolerance_is_complete(self):
        self.assertEqual(self.placement(61.7, 3699.5, [(0.0, 3699.5)]), "complete")

    def test_summarize_scope_mid_gap(self):
        snap = {"episode_id": "e", "fetch_id": "e", "fetch_start": "", "req_start": 0.0,
                "req_end": 3702.0, "duration_s": 3702.0, "tolerance_s": 3.0,
                "stages": {k: True for k in coverage.REQUIRED_STAGES}, "has_transcript": True}
        gap = coverage.summarize_scope([dict(snap, successful_intervals=[(0.0, 1000.0),
                                                                        (1002.9, 3702.0)])])
        self.assertNotIn("e", gap["distinct_covered_episodes"])
        end = coverage.summarize_scope([dict(snap, successful_intervals=[(0.0, 3699.5)])])
        self.assertIn("e", end["distinct_covered_episodes"])

    def test_measured_duration_keeps_eps(self):
        self.assertEqual(coverage.duration_tolerance(61.7), coverage.DURATION_TOLERANCE_S)
        self.assertEqual(coverage.duration_tolerance(214.809333333333), coverage.EPS)
        # duration_tolerance (the import's gate) keeps EPS for a measured
        # duration, off the 0.1-min grid; placement coverage uses the one
        # DET-PLAN item 6 rule for every stored duration (15 s).
        self.assertEqual(self.placement(3616.0 / 60.0, 3600.0, [(0.0, 3600.0)]), "incomplete")
        self.assertEqual(self.placement(3614.0 / 60.0, 3600.0, [(0.0, 3600.0)]), "complete")

    def test_import_keeps_eps_for_measured_durations(self):
        con = make_file_workspace()
        self.addCleanup(con.close)
        seed_legacy_episode(con, "ep-m", 3601.0 / 60.0, 3600.0)
        seed_legacy_episode(con, "ep-g", 60.1, 3604.0)
        rows = {e["episode_id"]: e for e in coverage_import.run(con, dry_run=True)["episodes"]}
        self.assertIn("stops short", rows["ep-m"]["reason"])
        self.assertEqual(rows["ep-m"]["tolerance_s"], coverage.EPS)
        self.assertNotIn("stops short", rows["ep-g"]["reason"])


class ReportAndReadsTest(_ImportBase):
    """R4-04 / R4-05 / R4-06."""

    def test_unflagged_active_row_keeps_its_read_and_text_entry(self):
        shutil.rmtree(self.corpus)
        jev_rescreen.export_current_copies(self.con, self.corpus)
        cr.jev_backfill(self.corpus, transport=fake_transport(lambda w: 0.02), log=lambda m: None)
        self.run_import()
        row = self.con.execute("SELECT id, kind FROM ad_reads WHERE evidence_id='jev:old-s3'"
                               ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "jev")
        if ad_reads.fts_present(self.con):
            hits = {r[0] for r in self.con.execute(
                f"SELECT rowid FROM {ad_reads.FTS} WHERE {ad_reads.FTS} MATCH 'window'")}
            self.assertIn(row["id"], hits)

    def test_rerun_says_already_imported_and_lists_only_real_carry_overs(self):
        self.run_import()
        out = self.run_import(run_id="rs-2")
        self.assertEqual(out["kept_entries"], ["s3"])
        self.assertEqual(out["already_imported"], ["s1", "s2"])
        text = jev_rescreen.format_report(out)
        self.assertIn("already imported: s1, s2", text)

    def test_dry_run_reports_the_planned_counts(self):
        out = self.run_import(dry_run=True)
        self.assertEqual((out["created_evidence"], out["reused_evidence"], out["kept_rows"],
                          out["attestations"]), (1, 0, 2, 2))
        text = jev_rescreen.format_report(out)
        self.assertIn("would create 1 new evidence rows", text)
        self.assertNotIn("jev-run version None", text)


# ---------------------------------------------------------------- review #5

class EachGateHalfTest(_ImportBase):
    """R5-01: each half of the proof gate on its own. One stage is valid from
    a producer the gate never touches; the other rests on re-screen proof
    only. With the old run reactivated the placement stays incomplete."""

    def setUp(self):
        super().setUp()
        self.run_import()
        tv = self.ids["tv2"]
        for stage in ("transcribe", "index"):
            coverage.record_attestation(self.con, "s2", stage, (0.0, 180.0), (0.0, 180.0),
                                        source_pin=f"source-version:{tv}",
                                        producer="test-fixture", actor="owner")
        self.con.commit()
        transcript_index.index_versions(self.con, [tv])
        self.con.commit()
        self.cid = seed_campaign(self.con)
        self.pid = add_placement(self.con, self.cid, "p2", episode_id="s2")

    def attest(self, stage, producer):
        coverage.record_attestation(self.con, "s2", stage, (0.0, 180.0), (0.0, 180.0),
                                    source_pin=f"source-version:{self.ids['tv2']}",
                                    producer=producer, actor="owner")
        self.con.commit()

    def state(self):
        return campaigns.build_customer_draft(self.con, self.cid)["coverage_by_placement"][
            self.pid]["state"]

    def reactivate_old_run(self):
        self.con.execute("UPDATE source_versions SET is_active=(id=?) WHERE source_key='jev-run'",
                         (self.ids["old_run"],))
        self.con.commit()

    def test_screen_half(self):
        # Publication from the re-screen and from a non-gated producer;
        # screen only from the run-entry import.
        self.attest("observation_publication", "observation:obs-1")
        self.attest("screen", "legacy-coverage-import:cov-1")
        self.assertEqual(self.state(), "complete")
        self.reactivate_old_run()
        self.assertEqual(self.state(), "incomplete")

    def test_publication_half(self):
        # Screen from a non-gated producer; publication only from the re-screen.
        # DET-PLAN item 6: publication proof is not part of "fully checked",
        # so its half of the gate no longer moves coverage.
        self.attest("screen", "test-fixture")
        self.assertEqual(self.state(), "complete")
        self.reactivate_old_run()
        self.assertEqual(self.state(), "complete")

    def test_entry_whose_rows_are_not_visible_does_not_count(self):
        # R5-04: an active run whose entry's published rows are hidden.
        self.attest("screen", "test-fixture")
        vid, _run = self.active_run()
        row = self.con.execute("SELECT id FROM evidence WHERE episode_id='s1'"
                               " AND source_version_id=?", (vid,)).fetchone()
        self.assertTrue(jev_rescreen.run_entry_current(self.con, "s1", self.ids["tv1"]))
        self.con.execute("INSERT OR REPLACE INTO evidence_source_membership VALUES (?, ?, 0)",
                         (row[0], vid))
        self.con.commit()
        self.assertFalse(jev_rescreen.run_entry_current(self.con, "s1", self.ids["tv1"]))


class ClippedTwinIsReusedTest(_ImportBase):
    """R5-03: an earlier run's row for the same window and start, whose end
    went past the copy's end, is the re-screen's window (ends equal once
    both are clipped to the copy end): reused, never a second read."""

    def test_clipped_end_twin_is_reused(self):
        self.con.execute(
            "INSERT INTO evidence (id, source, source_version_id, episode_id, orig_start_s,"
            " orig_end_s, excerpt, attrs, revision, created_at, transcript_version_id,"
            " source_record_key) VALUES ('jev:old-s3-w1', 'jev', ?, 's3', 60.0, 125.0,"
            " 'old window', '{}', 1, ?, ?, 'jev:s3:window_001')",
            (self.ids["old_run"], db.utcnow(), self.ids["tv3"]))
        self.con.commit()
        shutil.rmtree(self.corpus)
        jev_rescreen.export_current_copies(self.con, self.corpus)
        cr.jev_backfill(self.corpus, transport=fake_transport(
            lambda w: 0.95 if w["start_s"] == 60.0 else 0.02), log=lambda m: None)
        out = self.run_import()
        s3 = [r[0] for r in self.con.execute(
            "SELECT id FROM evidence WHERE episode_id='s3' AND orig_start_s=60.0")]
        self.assertEqual(s3, ["jev:old-s3-w1"])
        entry = {e["episode_id"]: e for e in self.active_run()[1]["episodes"]}["s3"]
        self.assertIn("window_001", entry["answers"])  # the reused row's own key
        rows = customer._customer_ad_rows(self.con, None, self_promo="include")
        self.assertEqual(sum(1 for r in rows if r["episode_id"] == "s3"
                             and r["orig_start_s"] == 60.0), 1)
        self.assertGreaterEqual(out["reused_evidence"], 1)


class SeedReportAfterCarryTest(ReseedKeepsTheRescreenTest):
    """R5-02: the seed report reflects the carry: a reviewed re-screen row
    on an unchanged copy is not 'orphaned', and ad_reads is counted after
    the carry."""

    def test_report(self):
        row = self.con.execute("SELECT e.id FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
                               " WHERE e.source='jev' AND e.episode_id='s2'"
                               " AND e.transcript_version_id=ep.transcript_version_id").fetchone()
        self.con.execute("INSERT INTO reviews (evidence_id, judgment, eff_start_s, eff_end_s,"
                         " notes, revision, updated_at) VALUES (?, 'ad', NULL, NULL, '', 1, ?)",
                         (row[0], db.utcnow()))
        self.con.commit()
        report = seed_src.seed_all(self.root, self.con, "test")
        self.con.commit()
        self.assertEqual(report["human_work"]["orphaned"], 0)
        self.assertEqual(report["ad_reads"]["reads"],
                         self.con.execute("SELECT count(*) FROM ad_reads").fetchone()[0])
        self.assertIn(row[0], {r["id"] for r in customer._customer_ad_rows(
            self.con, None, self_promo="include")})
