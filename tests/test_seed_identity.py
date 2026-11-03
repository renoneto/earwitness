"""Detector evidence identity survives a re-seed when its own candidate is
unchanged, and a separate corpus directory seeds into a separate workspace.

No mocks: a synthetic two-episode corpus is seeded with the real importer
into workspaces created inside the repository and removed afterwards."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import campaigns, citations, db, ops
from earwitness.app import import_sources as src
from earwitness.pipeline import jobs

DET_REL = "experiments/next-validation-detector/output/candidates_normalized.json"
JEV_REL = "experiments/jev-ad-triage-20260921/results.json"


def _transcript(text: str, shift: float = 0.0) -> dict:
    return {"segments": [{"start": shift, "end": shift + 12.0, "text": text},
                         {"start": shift + 12.0, "end": shift + 24.0, "text": "more words"}]}


def _candidate(ep_id: str, start: float, **extra) -> dict:
    return {"candidate_id": f"{ep_id}.c0", "ep_id": ep_id, "start_s": start,
            "end_s": start + 10.0, "excerpt": "use code TEST", "brand_spoken": "Acme",
            "anchor": "cta_code", "confidence": "high", "fetch_date": "2026-10-01",
            "provenance": {"anchor_seg_idx": [1, 2]}, **extra}


def write_corpus(root: Path, shift_e2: float = 0.0, jev: bool = True,
                 e1_extra: dict | None = None) -> Path:
    """Two supplied episodes, one detector candidate each."""
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(exist_ok=True)
    (root / "data/episodes.json").write_text(json.dumps([
        {"ep_id": ep, "show_title": "Synthetic Show", "ep_title": ep,
         "pub_date": "2026-09-21", "audio_url": f"https://audio.example/{ep}.mp3"}
        for ep in ("e1", "e2")]))
    (root / "transcripts/e1.json").write_text(json.dumps(_transcript("alpha one")))
    (root / "transcripts/e2.json").write_text(json.dumps(_transcript("beta two", shift_e2)))
    (root / DET_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / DET_REL).write_text(json.dumps(
        [_candidate("e1", 0.0, **(e1_extra or {})), _candidate("e2", shift_e2)]))
    if jev:
        (root / JEV_REL).parent.mkdir(parents=True, exist_ok=True)
        (root / JEV_REL).write_text(json.dumps({"episodes": []}))
    return root


def _workspace_cli():
    spec = importlib.util.spec_from_file_location("app_workspace_cli", REPO / "scripts/app_workspace.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drop_membership(con) -> None:
    """A workspace or backup made before evidence_source_membership existed."""
    con.execute("DROP TABLE evidence_source_membership")


def _detector_items(con) -> dict:
    out = ops.search_evidence(con, {"source": ["detector"], "per_page": 100})
    return {i["source_record_key"]: i for i in out["items"]}


def _make_legacy(con) -> None:
    """Re-key detector evidence the way the pre-content-identity importer did:
    sha256 of [record id, whole detector file sha, transcript sha]."""
    rows = con.execute("SELECT e.id, e.source_record_key, sv.sha256 AS dsha, tv.sha256 AS tsha"
                       " FROM evidence e JOIN source_versions sv ON sv.id=e.source_version_id"
                       " JOIN source_versions tv ON tv.id=e.transcript_version_id"
                       " WHERE e.source='detector'").fetchall()
    tables = [m[0] for m in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if any(c[1] == "evidence_id" for c in con.execute(f"PRAGMA table_info({m[0]})"))]
    con.execute("PRAGMA foreign_keys=OFF")
    for row in rows:
        new = "det:" + src.sha256_bytes(json.dumps(
            [row["source_record_key"], row["dsha"], row["tsha"]]).encode())
        for table in tables:
            con.execute(f"UPDATE {table} SET evidence_id=? WHERE evidence_id=?", (new, row["id"]))
        con.execute("UPDATE source_states SET record_id=? WHERE kind='evidence' AND record_id=?",
                    (new, row["id"]))
        con.execute("UPDATE evidence SET id=? WHERE id=?", (new, row["id"]))
    con.execute("PRAGMA foreign_keys=ON")


class SeedIdentityTest(unittest.TestCase):
    def setUp(self):
        self.corpus = Path(tempfile.mkdtemp(prefix="seed-identity-corpus-"))
        self.ws = Path(tempfile.mkdtemp(prefix=".seed-identity-ws-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.corpus, True)
        self.addCleanup(shutil.rmtree, self.ws, True)

    def test_unchanged_candidate_keeps_identity_and_review_across_reseed(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            before = _detector_items(con)
            self.assertEqual(set(before), {"det:e1.c0", "det:e2.c0"})
            ops.save_review(con, before["det:e1.c0"]["id"], {"judgment": "ad"}, 0, "test")
            # e2 is re-fetched with a pre-roll shift; the detector re-runs and
            # the whole detector-output file changes.
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")
            after = _detector_items(con)
            total = con.execute("SELECT COUNT(*) FROM evidence WHERE source='detector'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(after["det:e1.c0"]["id"], before["det:e1.c0"]["id"])
        self.assertEqual(after["det:e1.c0"]["judgment"], "ad")
        self.assertNotEqual(after["det:e2.c0"]["id"], before["det:e2.c0"]["id"])
        self.assertIsNone(after["det:e2.c0"]["judgment"])
        # Only the changed episode's candidate is new: 2 + 1, not 2 + 2.
        self.assertEqual(total, 3)

    def test_unnamed_named_read_seeds_no_brand(self):
        write_corpus(self.corpus)
        named = self.corpus / src.NAMED_OUT_REL
        named.parent.mkdir(parents=True, exist_ok=True)
        unnamed = {"brand_spoken": None, "brand_raw": None, "needs_review": True,
                   "source": "jev+namer", "anchor": "namer:gliner"}
        named.write_text(json.dumps([
            _candidate("e1", 0.0, **{**unnamed, "candidate_id": "e1.named.a", "brand": None}),
            _candidate("e1", 40.0, **{**unnamed, "candidate_id": "e1.named.b",   # older export
                                      "brand": "(unnamed)"})]))
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            rows = con.execute("SELECT id FROM evidence WHERE source_record_key LIKE 'named:%'"
                               ).fetchall()
            brands = con.execute("SELECT a.brand FROM brand_assocs a JOIN evidence e"
                                 " ON e.id=a.evidence_id WHERE e.source_record_key LIKE 'named:%'"
                                 ).fetchall()
        finally:
            con.close()
        self.assertEqual((len(rows), brands), (2, []))

    def test_volatile_candidate_fields_do_not_change_identity(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            before = _detector_items(con)
            ops.save_review(con, before["det:e1.c0"]["id"], {"judgment": "ad"}, 0, "test")
            write_corpus(self.corpus, e1_extra={
                "fetch_date": "2026-10-09", "confidence": "medium", "ep_title": "renamed",
                "provenance": {"anchor_seg_idx": [7, 8], "fetch_date_source": "processing-date"}})
            src.seed_all(self.corpus, con, "test")
            after = _detector_items(con)
            total = con.execute("SELECT COUNT(*) FROM evidence WHERE source='detector'").fetchone()[0]
            # Evidence-defining content still re-keys.
            write_corpus(self.corpus, e1_extra={"excerpt": "use code OTHER"})
            src.seed_all(self.corpus, con, "test")
            changed = _detector_items(con)
        finally:
            con.close()
        self.assertEqual(after["det:e1.c0"]["id"], before["det:e1.c0"]["id"])
        self.assertEqual(after["det:e1.c0"]["judgment"], "ad")
        self.assertEqual(total, 2)
        self.assertNotEqual(changed["det:e1.c0"]["id"], before["det:e1.c0"]["id"])

    def test_workspace_seeded_before_content_identity_keeps_reviews(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            _make_legacy(con)
            legacy = _detector_items(con)
            ops.save_review(con, legacy["det:e1.c0"]["id"], {"judgment": "ad"}, 0, "test")
            src.seed_all(self.corpus, con, "test")  # unchanged files
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")  # e2 changes
            after = _detector_items(con)
            total = con.execute("SELECT COUNT(*) FROM evidence WHERE source='detector'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(after["det:e1.c0"]["id"], legacy["det:e1.c0"]["id"])
        self.assertEqual(after["det:e1.c0"]["judgment"], "ad")
        self.assertEqual(total, 3)

    def test_evidence_ref_after_association_edit_survives_unrelated_reseed(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            eid = _detector_items(con)["det:e1.c0"]["id"]
            ops.save_review(con, eid, {"judgment": "ad"}, 0, "test")
            ops.set_brand_assocs(con, eid, [{"brand": "acme", "basis": "confirmed",
                                             "channel": "spoken"}], 0, "test")
            ref = citations.make_evidence_ref(con, eid)
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")
            resolved = citations.resolve_evidence_ref(con, ref)
        finally:
            con.close()
        self.assertEqual(resolved["evidence_id"], eid)
        self.assertEqual([a["brand"] for a in resolved["assocs"]], ["acme"])
        self.assertEqual(resolved["review"]["judgment"], "ad")

    def test_published_attachment_ref_stays_current_after_unrelated_reseed(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            eid = _detector_items(con)["det:e1.c0"]["id"]
            ops.save_review(con, eid, {"judgment": "ad"}, 0, "test")
            ops.set_brand_assocs(con, eid, [{"brand": "acme", "basis": "confirmed",
                                             "channel": "spoken"}], 0, "test")
            ref = citations.current_source_ref(con, "evidence", eid)
            before = campaigns._occurrence_from_evidence(con, eid, "acme", ref)
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")
            after = campaigns._occurrence_from_evidence(con, eid, "acme", ref)
            row = con.execute("SELECT source_version_id, revision FROM evidence WHERE id=?",
                              (eid,)).fetchone()
            visible = _detector_items(con)["det:e1.c0"]["id"]
        finally:
            con.close()
        self.assertEqual((before["review_status"], before["advertiser_confirmed"]),
                         ("reviewed_ad", True))
        self.assertEqual((after["review_status"], after["advertiser_confirmed"],
                          after["source_matched"]),
                         ("reviewed_ad", True, before["source_matched"]))
        self.assertTrue(after["operator_qualification"]["source_current"])
        self.assertEqual(visible, eid)  # still visible, with its source pin untouched

    def test_duplicates_from_the_old_reseed_bug_collapse_to_the_reviewed_row(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            reviewed = _detector_items(con)["det:e1.c0"]["id"]
            ops.save_review(con, reviewed, {"judgment": "ad"}, 0, "test")
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")
            # The old importer re-inserted every unchanged row under the new version.
            con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                        " transcript_version_id, source_record_key)"
                        " SELECT 'det:dup', source, (SELECT id FROM source_versions WHERE"
                        " source_key='detector-output' AND is_active=1), episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, 1, created_at,"
                        " transcript_version_id, source_record_key FROM evidence WHERE id=?",
                        (reviewed,))
            src.seed_all(self.corpus, con, "test")
            out = ops.search_evidence(con, {"source": ["detector"], "per_page": 100})
            keys = [i["source_record_key"] for i in out["items"]]
            kept = [i for i in out["items"] if i["source_record_key"] == "det:e1.c0"]
        finally:
            con.close()
        self.assertEqual(sorted(keys), ["det:e1.c0", "det:e2.c0"])
        self.assertEqual((kept[0]["id"], kept[0]["judgment"]), (reviewed, "ad"))

    def test_workspace_without_membership_table_searches_and_upgrade_adds_it(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            _drop_membership(con)
            self.assertEqual(sorted(_detector_items(con)), ["det:e1.c0", "det:e2.c0"])
            ops.overview(con)
            src.seed_all(self.corpus, con, "test")  # seeding recreates it on demand
            self.assertTrue(db.has_evidence_membership(con))
            _drop_membership(con)
        finally:
            con.close()
        result = _workspace_cli().upgrade_workspace(self.ws)
        self.assertFalse(result["upgraded"])
        con = db.connect(self.ws)
        try:
            self.assertTrue(db.has_evidence_membership(con))
            self.assertEqual(sorted(_detector_items(con)), ["det:e1.c0", "det:e2.c0"])
        finally:
            con.close()

    def test_backup_made_before_membership_restores(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            _drop_membership(con)
            backup = self.ws / "old-backup.sqlite3"
            ops.backup_to(con, backup)
        finally:
            con.close()
        target = Path(tempfile.mkdtemp(prefix=".seed-identity-restore-", dir=REPO))
        self.addCleanup(shutil.rmtree, target, True)
        _workspace_cli().restore(target, str(backup.relative_to(REPO)), False)
        con = db.connect(target)
        try:
            self.assertTrue(db.has_evidence_membership(con))
            self.assertEqual(sorted(_detector_items(con)), ["det:e1.c0", "det:e2.c0"])
        finally:
            con.close()

    def test_backup_made_before_job_profiles_restores(self):
        # A v8 backup taken before p01_job_profiles existed (any backup from
        # code older than the pipeline merge) must still restore.
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            jobs.submit_run(con, stages=[
                {"job_id": "j1", "stage": "transcribe", "input_digest": "j1",
                 "backend_profile": "gpu-a"},
                {"job_id": "j2", "stage": "transcribe", "input_digest": "j2"}],
                idempotency_key="pre-merge")
            # What pre-merge code never created.
            con.execute("DROP TABLE p01_job_profiles")
            con.execute("DROP TABLE evidence_source_membership")
            con.execute("DROP INDEX IF EXISTS idx_jobs_state")
            backup = self.ws / "old-backup.sqlite3"
            ops.backup_to(con, backup)
        finally:
            con.close()
        target = Path(tempfile.mkdtemp(prefix=".seed-identity-restore-", dir=REPO))
        self.addCleanup(shutil.rmtree, target, True)
        _workspace_cli().restore(target, str(backup.relative_to(REPO)), False)
        con = db.connect(target)
        try:
            self.assertEqual([tuple(r) for r in con.execute(
                "SELECT job_id, profile FROM p01_job_profiles ORDER BY job_id")],
                [("j1", "gpu-a"), ("j2", "")])
            self.assertEqual(db.verify_schema(con)["problems"], [])
        finally:
            con.close()

    def test_duplicates_with_human_work_on_both_rows_merge_onto_one(self):
        # Review of 534fc73 item 2: one active row per stable key; the other
        # copy's human work moves onto it (it used to stay visible too).
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            first = _detector_items(con)["det:e1.c0"]["id"]
            ops.save_review(con, first, {"judgment": "ad"}, 0, "test")
            write_corpus(self.corpus, shift_e2=30.0)
            src.seed_all(self.corpus, con, "test")
            # Old importer: unchanged row re-inserted under the new version; the
            # owner then confirmed an association on the visible copy.
            con.execute("INSERT INTO evidence (id, source, source_version_id, episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, revision, created_at,"
                        " transcript_version_id, source_record_key)"
                        " SELECT 'det:dup', source, (SELECT id FROM source_versions WHERE"
                        " source_key='detector-output' AND is_active=1), episode_id,"
                        " orig_start_s, orig_end_s, excerpt, attrs, 1, created_at,"
                        " transcript_version_id, source_record_key FROM evidence WHERE id=?",
                        (first,))
            con.execute("DELETE FROM evidence_source_membership")
            ops.set_brand_assocs(con, "det:dup", [{"brand": "acme", "basis": "confirmed",
                                                   "channel": "spoken"}], 0, "test")
            report = src.seed_all(self.corpus, con, "test")
            out = ops.search_evidence(con, {"source": ["detector"], "per_page": 100})
            e1 = {i["id"]: i for i in out["items"] if i["source_record_key"] == "det:e1.c0"}
            assocs = [a["brand"] for a in e1["det:dup"]["assocs"]]
        finally:
            con.close()
        self.assertEqual(set(e1), {"det:dup"})
        self.assertEqual(e1["det:dup"]["judgment"], "ad")
        self.assertIn("acme", assocs)
        self.assertEqual((report["human_work"]["merged"], report["human_work"]["merged_keys"]),
                         (1, ["det:e1.c0"]))

    def test_membership_changes_the_projection_revision(self):
        from earwitness.app import search
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            spec = {"scope": "research"}
            before = search.projection_revision(con, spec)
            eid = _detector_items(con)["det:e1.c0"]["id"]
            version = con.execute("SELECT id FROM source_versions WHERE source_key='detector-output'"
                                  ).fetchone()[0]
            con.execute("INSERT INTO evidence_source_membership VALUES (?, ?, 0)", (eid, version))
            after = search.projection_revision(con, spec)
        finally:
            con.close()
        self.assertNotEqual(before, after)

    def test_identical_reseed_is_a_no_op(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            first = _detector_items(con)
            src.seed_all(self.corpus, con, "test")
            second = _detector_items(con)
            total = con.execute("SELECT COUNT(*) FROM evidence WHERE source='detector'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual({k: v["id"] for k, v in first.items()},
                         {k: v["id"] for k, v in second.items()})
        self.assertEqual(total, 2)


class SeedSourceRootTest(unittest.TestCase):
    def setUp(self):
        self.corpus = Path(tempfile.mkdtemp(prefix="seed-root-corpus-"))
        self.ws = Path(tempfile.mkdtemp(prefix=".seed-root-ws-", dir=REPO))
        self.addCleanup(shutil.rmtree, self.corpus, True)
        self.addCleanup(shutil.rmtree, self.ws, True)

    def _counts(self) -> dict:
        con = db.connect(self.ws)
        try:
            return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("episodes", "evidence")} | {
                "transcripts": con.execute(
                    "SELECT COUNT(*) FROM source_versions WHERE source_key LIKE 'transcript:%'"
                ).fetchone()[0]}
        finally:
            con.close()

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO / "scripts/app_workspace.py"),
             "--workspace", str(self.ws.relative_to(REPO)), "seed", *args],
            cwd=REPO, capture_output=True, text=True, env={"PYTHONPATH": str(REPO)})

    def test_cli_seeds_separate_corpus_into_separate_workspace(self):
        write_corpus(self.corpus)
        done = self._cli("--source-root", str(self.corpus))
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertEqual((report["episodes"], report["detector_candidates"]), (2, 2))
        self.assertEqual(self._counts(), {"episodes": 2, "evidence": 2, "transcripts": 2})

    def test_source_files_record_actual_paths_only_for_explicit_sources(self):
        write_corpus(self.corpus)
        done = self._cli("--source-root", str(self.corpus))
        self.assertEqual(done.returncode, 0, done.stderr)
        con = db.connect(self.ws)
        try:
            paths = {r[0] for r in con.execute("SELECT path FROM source_files")}
        finally:
            con.close()
        root = str(self.corpus.resolve())
        self.assertIn(f"{root}/data/episodes.json", paths)
        self.assertIn(f"{root}/transcripts/e1.json", paths)
        self.assertTrue(all(p.startswith(root + "/") for p in paths), paths)
        # Library default (no sources): constant repo-relative labels as before.
        default_ws = Path(tempfile.mkdtemp(prefix=".seed-root-default-", dir=REPO))
        self.addCleanup(shutil.rmtree, default_ws, True)
        con = db.connect(default_ws)
        try:
            db.init_schema(con)
            src.seed_all(self.corpus, con, "test")
            default_paths = {r[0] for r in con.execute("SELECT path FROM source_files")}
        finally:
            con.close()
        self.assertIn("data/episodes.json", default_paths)
        self.assertIn("transcripts/e1.json", default_paths)

    def test_no_jev_is_reported_and_keeps_existing_jev_windows_explicit(self):
        write_corpus(self.corpus)
        con = db.connect(self.ws)
        try:
            db.init_schema(con)
            default = src.seed_all(self.corpus, con, "test")
            skipped = src.seed_all(self.corpus, con, "test",
                                   src.seed_sources(self.corpus, no_jev=True))
        finally:
            con.close()
        self.assertNotIn("jev_skipped", default)
        self.assertEqual(skipped["jev_skipped"], {"existing_jev_evidence": 0})

    def test_no_jev_allows_missing_jev_results_only_when_requested(self):
        write_corpus(self.corpus, jev=False)
        refused = self._cli("--source-root", str(self.corpus))
        self.assertNotEqual(refused.returncode, 0)
        done = self._cli("--source-root", str(self.corpus), "--no-jev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._counts(), {"episodes": 2, "evidence": 2, "transcripts": 2})

    def test_explicit_paths_override_the_source_root_layout(self):
        write_corpus(self.corpus)
        flat = Path(tempfile.mkdtemp(prefix="seed-root-flat-"))
        self.addCleanup(shutil.rmtree, flat, True)
        shutil.copy(self.corpus / "data/episodes.json", flat / "eps.json")
        shutil.copytree(self.corpus / "transcripts", flat / "tx")
        shutil.copy(self.corpus / DET_REL, flat / "cands.json")
        done = self._cli("--episodes", str(flat / "eps.json"), "--transcripts", str(flat / "tx"),
                         "--candidates", str(flat / "cands.json"), "--no-jev")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self._counts(), {"episodes": 2, "evidence": 2, "transcripts": 2})


if __name__ == "__main__":
    unittest.main()
