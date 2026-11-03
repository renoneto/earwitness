"""Mini-side `export-delta` / `export-receipt` (fresh-data design rev 4a:
5.2-5.4, 6.2, 7.1-7.2, 8.1, 8.4, 8.5, 12.1, 13). Offline; the signing keys
are generated here with ssh-keygen."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from earwitness.pipeline import corpus as cr

try:
    from earwitness.pipeline import export_delta as ed
except ImportError:  # red: the module does not exist yet; every test then fails
    ed = None

REPO = Path(__file__).resolve().parent.parent
SSH_KEYGEN = shutil.which("ssh-keygen")
FEED_A = "https://a.example/feed"
FEED_B = "https://b.example/feed"
FEED_X = "https://x.example/feed"   # a full-tier show outside the inventory
SPONSOR = ("This episode is sponsored by Squarespace. Go to squarespace.com/earwitness "
           "and use code EARWITNESS for ten percent off.")
T0 = datetime(2026, 10, 22, 7, 30, tzinfo=timezone.utc)
PKG_NAME = re.compile(r"p-[0-9a-f]{32}\.(tar|manifest\.json|manifest\.json\.sig|done)")


def ep(n: int) -> str:
    return "ep_%016x" % n


def keygen(path: Path) -> str:
    subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return Path(str(path) + ".pub").read_text().strip()


def sign(key: Path, path: Path, ns: str) -> None:
    subprocess.run([SSH_KEYGEN, "-Y", "sign", "-n", ns, "-f", str(key), str(path)],
                   check=True, stdin=subprocess.DEVNULL, capture_output=True)


@unittest.skipIf(SSH_KEYGEN is None, "ssh-keygen not installed: package and receipt "
                 "signatures cannot be made or checked")
class ExportDeltaBase(unittest.TestCase):
    targets = ["db-judges", "db-pool-1"]

    def setUp(self):
        base = os.environ.get("AGENT_SCRATCH") or None
        if base:
            Path(base).mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="export-delta-", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.corpus = self.tmp / "corpus"
        cr.init_corpus(self.corpus, [
            {"title": "Show A", "feed_url": FEED_A, "category": "News", "source": "t"},
            {"title": "Show B", "feed_url": FEED_B, "category": "News", "source": "t"},
            {"title": "Show X", "feed_url": FEED_X, "category": "News", "source": "t"}], 2)
        self.dirs = cr.corpus_dirs(self.corpus)
        keys = self.tmp / "keys"
        keys.mkdir()
        self.mini_key = keys / "mini"
        self.host_key = keys / "host"
        self.other_key = keys / "other"
        mini_pub, host_pub = keygen(self.mini_key), keygen(self.host_key)
        keygen(self.other_key)
        self.package_signers = keys / "package_signers"
        self.package_signers.write_text(f'mini-1 namespaces="earwitness-package-v1" {mini_pub}\n')
        self.receipt_signers = keys / "receipt_signers"
        self.receipt_signers.write_text(
            f'earwitness-host namespaces="earwitness-receipt-v1" {host_pub}\n')
        self.inventory = self.tmp / "shows-inventory.json"
        self.inventory.write_text(json.dumps({"shows": [
            {"show_key": "show-a", "feed_url": FEED_A, "feed_identity": FEED_A,
             "apple_id": 1, "title": "Show A"},
            {"show_key": "show-b", "feed_url": FEED_B, "feed_identity": FEED_B,
             "apple_id": None, "title": "Show B"}]}))
        self.removal = self.tmp / "removal-list.json"
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": []}))
        self.outbox = self.tmp / "outbox"
        # the Mini's own store of host removals, apart from the export ledger
        self.removal_store = self.tmp / "mini-config" / "earwitness-removals.json"
        self.host_removals = {"episode_ids": [], "show_keys": []}
        self.write_store()     # an activated Mini: a store from its first accepted receipt
        self.cfg_path = self.tmp / "export-config.json"
        self.limits = {}
        self.now = T0
        self.write_config()
        self.obs_id = 0

    def write_store(self, eps=(), shows=(), paused=None):
        self.removal_store.parent.mkdir(parents=True, exist_ok=True)
        self.removal_store.write_text(json.dumps({
            "format": "earwitness-mini-removals/1", "episode_ids": sorted(eps),
            "show_keys": sorted(shows), "paused": paused}))

    def write_config(self, **over):
        cfg = {"corpus": str(self.corpus), "ledger": str(self.tmp / "export-ledger.sqlite3"),
               "outbox": str(self.outbox), "work_dir": str(self.tmp / "work"),
               "inventory": str(self.inventory), "removal_list": str(self.removal),
               "scope": {"pub_date_from": "2026-10-08", "pub_date_to": "2026-12-31"},
               "targets": self.targets, "producer": "mini-1",
               "signing_key": str(self.mini_key),
               "receipt_allowed_signers": str(self.receipt_signers),
               "receipt_principal": "earwitness-host", "initial_epoch": 1,
               "jev_budget_ledger": str(self.tmp / "no-jev-budget.sqlite3"),
               "removal_store": str(self.removal_store),
               "limits": self.limits, **over}
        self.cfg_path.write_text(json.dumps(cfg))

    # ------------------------------------------------------------ fixture corpus
    def add_done(self, ep_id, feed=FEED_A, pub="2026-10-10", tier="full", jev="ok",
                 names="ok", detections=True, text=SPONSOR, stage="done"):
        con = cr.connect(self.corpus)
        try:
            with con:
                cur = con.execute(
                    "INSERT INTO fetch_observations (episode_id, started_at, finished_at,"
                    " enclosure_url, final_url, http_status, bytes, sha256, host, verification)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ep_id, "2026-10-10T04:59:00+00:00", "2026-10-10T05:00:00+00:00",
                     f"https://cdn.example/{ep_id}.mp3", f"https://cdn2.example/{ep_id}.mp3",
                     200, 1000, "a" * 64, "mini", json.dumps({"length_verified": True})))
                obs = cur.lastrowid
                con.execute(
                    "INSERT INTO episodes (episode_id, feed_url, guid, show_title, ep_title,"
                    " pub_date, enclosure_url, stage, audio_sha256, audio_bytes, duration_s,"
                    " fetch_observation_id, jev_status, name_status, tier, dead_stage, last_error)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ep_id, feed, f"guid-{ep_id}", "Show", f"Title {ep_id}", pub,
                     f"https://cdn.example/{ep_id}.mp3", stage, "a" * 64, 1000, 600.0, obs,
                     jev, names, tier, "transcribe" if stage == "dead" else None,
                     "transcript_unusable: secret body text" if stage == "dead" else None))
        finally:
            con.close()
        self.write_transcript(ep_id, obs, text)
        if detections:
            self.write_json(self.dirs["detections"] / f"{ep_id}.json", [{
                "candidate_id": f"{ep_id}.c0", "ep_id": ep_id, "start_s": 0.0, "end_s": 30.0,
                "brand_spoken": "squarespace", "brand_url": "squarespace.com",
                "brand_code": "EARWITNESS", "brand_raw": "Squarespace",
                "fetch_date": "2026-10-10", "fetch_date_source": "fetch_observation",
                "needs_review": True}])
        if jev and jev.startswith("ok"):
            self.write_jev(ep_id)
        if names and names.startswith("ok"):
            self.write_json(self.dirs["names"] / f"{ep_id}.json", {
                "pipeline": "test", "spans": [{
                    "span_id": "s0", "brand": "Squarespace", "method": "gliner",
                    "confidence": 0.9, "needs_review": True, "start_s": 0.0, "end_s": 30.0}]})
        return obs

    def write_transcript(self, ep_id, obs, text):
        self.write_json(self.dirs["transcripts"] / f"{ep_id}.json", {
            "ep_id": ep_id, "model": "fake", "language": "en",
            "segments": [{"start": 0.0, "end": 30.0, "text": text}],
            "provenance": {"fetch_observation_id": obs, "audio_sha256": "a" * 64}})

    def write_jev(self, ep_id, noul=0.95, model="jev-model", screened_at="2026-10-10T06:00:00Z"):
        self.write_json(self.dirs["jev"] / f"{ep_id}.json", {
            "episode_id": ep_id, "model": model, "screened_at": screened_at,
            "usage": {"input_tokens": 10, "output_tokens": 0}, "cost_usd": 0.00001,
            "windows": [{"window_id": "w0", "noul": noul, "start_s": 0.0, "end_s": 60.0}]})

    def set_episode(self, ep_id, **fields):
        con = cr.connect(self.corpus)
        try:
            with con:
                con.execute("UPDATE episodes SET %s WHERE episode_id=?"
                            % ",".join(f"{k}=?" for k in fields), [*fields.values(), ep_id])
        finally:
            con.close()

    @staticmethod
    def write_json(path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    # ------------------------------------------------------------ running
    def night(self, days=0):
        self.now = T0 + timedelta(days=days)
        with mock.patch.object(ed, "_now", lambda: self.now):
            code, res = ed.export_delta(ed.load_config(self.cfg_path))
        json.dumps(res)  # one JSON-serializable line
        return code, res

    def receipt(self, path, sig_key=None):
        sign(sig_key or self.host_key, path, "earwitness-receipt-v1")
        with mock.patch.object(ed, "_now", lambda: self.now):
            return ed.export_receipt(ed.load_config(self.cfg_path), path)

    def write_receipt(self, name, doc, removals=None, raw=False):
        """Contract C2: every host receipt and reconcile file carries the
        removal list and its digest (`raw` writes the doc as given)."""
        if not raw:
            rl = removals if removals is not None else self.host_removals
            doc = {**doc, "removal_list": rl, "removal_list_sha256": hashlib.sha256(
                json.dumps(rl, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        path = self.tmp / "receipts" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(doc))
        return path

    def host_receipt(self, res, accepted_by=None, epoch=1, extra_targets=(), removals=None,
                     **per_target):
        """A receipt naming this package; every target accepts every revision
        unless `accepted_by` names fewer targets."""
        man = self.manifest(res["package_id"])
        revs = [{"episode_id": r["episode_id"], "revision_seq": r["revision_seq"]}
                for r in man["revisions"]]
        tlist = []
        for t in (accepted_by if accepted_by is not None else self.targets):
            entry = {"db_identity": t, "outcome": "committed", "request_id": "REQ-20261022-1",
                     "accepted": revs, "stale": [], "refused": [], "deferred": []}
            entry.update(per_target.get(t.replace("-", "_"), {}))
            tlist.append(entry)
        for t in extra_targets:
            tlist.append({"db_identity": t, "outcome": "committed", "request_id": "REQ-x",
                          "accepted": revs, "stale": [], "refused": [], "deferred": []})
        digest = self.manifest_sha(res["package_id"])
        return self.write_receipt(f"{digest}.json", {
            "kind": "receipt", "restore_epoch": epoch, "package_digest": digest,
            "targets": tlist}, removals=removals)

    # ------------------------------------------------------------ package reading
    def files(self, pid):
        return {s: self.outbox / f"{pid}.{s}" for s in ("tar", "manifest.json",
                                                         "manifest.json.sig", "done")}

    def manifest(self, pid):
        return json.loads(self.files(pid)["manifest.json"].read_text())

    def manifest_sha(self, pid):
        return hashlib.sha256(self.files(pid)["manifest.json"].read_bytes()).hexdigest()

    def members(self, pid):
        with tarfile.open(self.files(pid)["tar"], "r:") as tf:
            return {m.name: (m, tf.extractfile(m).read() if m.isfile() else None)
                    for m in tf.getmembers()}

    def offered(self, pid):
        return {(r["episode_id"], r["revision_seq"]) for r in self.manifest(pid)["revisions"]}

    def revision(self, pid, ep_id):
        return json.loads(self.members(pid)[f"{ep_id}/revision.json"][1])


class ExportDeltaPackageTest(ExportDeltaBase):
    def test_first_export_builds_a_signed_package_in_the_export_format(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B, pub="2026-10-09")
        code, res = self.night()
        self.assertEqual((code, res["code"]), (0, "ok"))
        pid = res["package_id"]
        self.assertRegex(pid, r"^p-[0-9a-f]{32}$")
        self.assertEqual(pid, "p-" + self.manifest_sha(pid)[:32])
        self.assertEqual(sorted(p.name for p in self.outbox.iterdir() if p.is_file()),
                         sorted(f"{pid}.{s}" for s in ("tar", "manifest.json",
                                                       "manifest.json.sig", "done")))
        self.assertEqual(self.files(pid)["done"].read_bytes(), b"")
        man = self.manifest(pid)
        self.assertEqual(man["producer"], "mini-1")
        self.assertTrue(man["sealed_at"].startswith("2026-10-22T07:30"))
        self.assertEqual([r["episode_id"] for r in man["revisions"]], [ep(2), ep(1)])  # oldest
        mem = self.members(pid)
        listed = {m["name"]: m for m in man["members"]}
        self.assertEqual(set(listed), set(mem))
        for name, (info, data) in mem.items():
            self.assertTrue(info.isreg(), name)
            self.assertEqual((listed[name]["size"], listed[name]["sha256"]),
                             (len(data), hashlib.sha256(data).hexdigest()))
        self.assertEqual(man["tar"]["sha256"],
                         hashlib.sha256(self.files(pid)["tar"].read_bytes()).hexdigest())
        # the member files are the records the existing export writes
        out = self.tmp / "plain-export"
        cr.export(self.corpus, out)
        plain_eps = {e["ep_id"]: e for e in json.loads((out / "data/episodes.json").read_text())}
        plain_cands = json.loads((out / cr.EXPORT_CANDIDATES_REL).read_text())
        plain_named = json.loads((out / cr.EXPORT_NAMED_CANDIDATES_REL).read_text())
        plain_jev = json.loads((out / cr.EXPORT_JEV_RESULTS_REL).read_text())
        for e in (ep(1), ep(2)):
            self.assertEqual(json.loads(mem[f"{e}/fetch.json"][1]), plain_eps[e])
            self.assertEqual(mem[f"{e}/transcript.json"][1],
                             (out / "transcripts" / f"{e}.json").read_bytes())
            self.assertEqual(json.loads(mem[f"{e}/detections.json"][1]),
                             [c for c in plain_cands if c["ep_id"] == e])
            self.assertEqual(json.loads(mem[f"{e}/names.json"][1]),
                             [c for c in plain_named if c["ep_id"] == e])
            jev = json.loads(mem[f"{e}/jev.json"][1])
            self.assertEqual(jev["episodes"], [x for x in plain_jev["episodes"]
                                               if x["episode_id"] == e])
            self.assertEqual({k: v for k, v in jev.items() if k not in ("episodes",)},
                             {k: v for k, v in plain_jev.items() if k not in ("episodes",)})
        rev = json.loads(mem[f"{ep(1)}/revision.json"][1])
        self.assertEqual(rev["episode_id"], ep(1))
        self.assertEqual(rev["detection_status"], "complete")
        self.assertEqual(rev["fetch_date"], "2026-10-10")
        self.assertEqual(rev["fetch_date_basis"], "fetch_observation.finished_at")
        self.assertEqual(rev["fetch_observation"], {
            "id": 1, "finished_at": "2026-10-10T05:00:00+00:00",
            "final_url": f"https://cdn2.example/{ep(1)}.mp3", "http_status": 200,
            "bytes": 1000, "sha256": "a" * 64})
        self.assertEqual(set(rev["components"]), {"fetch.json", "transcript.json",
                                                  "detections.json", "jev.json", "names.json"})
        for comp, digest in rev["components"].items():
            self.assertEqual(digest, hashlib.sha256(mem[f"{ep(1)}/{comp}"][1]).hexdigest())
        self.assertIsInstance(rev["revision_seq"], int)
        # attribution stays separate; uncertain stays uncertain
        det = json.loads(mem[f"{ep(1)}/detections.json"][1])[0]
        self.assertEqual((det["brand_spoken"], det["brand_url"], det["brand_code"]),
                         ("squarespace", "squarespace.com", "EARWITNESS"))
        self.assertIs(det["needs_review"], True)
        named = json.loads(mem[f"{ep(1)}/names.json"][1])[0]
        self.assertIsNone(named["brand_url"])
        self.assertIs(named["needs_review"], True)

    def test_tar_has_only_regular_strict_members_and_no_media(self):
        self.add_done(ep(1))
        clip_dir = self.dirs["clips"] / ep(1)
        clip_dir.mkdir(parents=True)
        (clip_dir / "c0.mp3").write_bytes(b"ID3fake")
        self.write_json(clip_dir / "index.json", {"status": "ok", "clips": [{"file": "c0.mp3"}]})
        _, res = self.night()
        mem = self.members(res["package_id"])
        pat = re.compile(r"(status\.json|ep_[0-9a-f]{16}/(revision|transcript|detections|jev|"
                         r"names|shownotes|fetch)\.json)")
        for name, (info, _) in mem.items():
            self.assertTrue(info.isreg() and not info.issym() and not info.islnk(), name)
            self.assertRegex(name, pat)
            self.assertFalse(name.endswith((".mp3", ".m4a", ".wav")))

    def test_signature_verifies_with_ssh_keygen(self):
        self.add_done(ep(1))
        _, res = self.night()
        f = self.files(res["package_id"])
        with open(f["manifest.json"], "rb") as fh:
            ok = subprocess.run([SSH_KEYGEN, "-Y", "verify", "-f", str(self.package_signers),
                                 "-I", "mini-1", "-n", "earwitness-package-v1",
                                 "-s", str(f["manifest.json.sig"])], stdin=fh,
                                capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_unchanged_rerun_makes_no_new_revision(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq = self.revision(r1["package_id"], ep(1))["revision_seq"]
        _, r2 = self.night(days=1)
        self.assertEqual(r2["counts"]["new_revisions"], 0)
        self.assertEqual(self.offered(r2["package_id"]), set())  # still pending in r1
        self.receipt(self.host_receipt(r1))
        _, r3 = self.night(days=2)
        self.assertEqual(self.offered(r3["package_id"]), set())
        self.assertEqual(r3["counts"]["new_revisions"], 0)
        self.assertEqual(seq, self.revision(r1["package_id"], ep(1))["revision_seq"])

    def test_component_change_makes_a_higher_revision(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq1 = self.revision(r1["package_id"], ep(1))["revision_seq"]
        self.receipt(self.host_receipt(r1))
        self.write_transcript(ep(1), 1, SPONSOR + " Re-transcribed.")  # re-transcription
        _, r2 = self.night(days=1)
        self.assertEqual(r2["counts"]["new_revisions"], 1)
        rev2 = self.revision(r2["package_id"], ep(1))
        self.assertGreater(rev2["revision_seq"], seq1)
        self.assertEqual(self.offered(r2["package_id"]), {(ep(1), rev2["revision_seq"])})

    def test_backfill_only_change_makes_a_new_revision(self):
        self.add_done(ep(1), jev="skipped: no jev worker", names="skipped: no jev result")
        _, r1 = self.night()
        rev1 = self.revision(r1["package_id"], ep(1))
        self.assertNotIn(rev1["detection_status"], ("complete",))
        self.assertNotIn("jev.json", rev1["components"])
        self.receipt(self.host_receipt(r1))
        self.write_jev(ep(1))  # jev-backfill
        self.set_episode(ep(1), jev_status="ok (backfill)")
        _, r2 = self.night(days=1)
        rev2 = self.revision(r2["package_id"], ep(1))
        self.assertGreater(rev2["revision_seq"], rev1["revision_seq"])
        self.assertIn("jev.json", rev2["components"])
        self.assertEqual(rev2["components"]["transcript.json"],
                         rev1["components"]["transcript.json"])

    def test_detection_status_follows_section_13(self):
        self.add_done(ep(1))
        self.add_done(ep(2), jev="jev_skipped_budget", names=None)
        self.add_done(ep(3), jev="failed: permanent 401", names=None)
        self.add_done(ep(5), jev="skipped: no jev worker", names=None)
        _, res = self.night()
        got = {e: self.revision(res["package_id"], e)["detection_status"]
               for e in (ep(1), ep(2), ep(3), ep(5))}
        self.assertEqual(got[ep(1)], "complete")
        self.assertEqual(got[ep(2)], "jev_skipped_budget")
        self.assertEqual(got[ep(3)], "jev_failed")
        # a full-tier done episode always has a detections file (the existing
        # export requires it), so transcript_only is checked on the rule itself
        self.assertEqual(ed.detection_status(False, False, False, None, None),
                         "transcript_only")
        # not run is not a pass: an off-table status the host quarantines
        self.assertNotIn(got[ep(5)], ("complete", "jev_skipped_budget", "jev_failed",
                                      "transcript_only"))

    def test_inventory_scope_tier_and_stage_filters(self):
        self.add_done(ep(1))                                  # in
        self.add_done(ep(2), feed=FEED_X)                     # show outside the inventory
        self.add_done(ep(3), pub="2026-10-07")                # before the corpus start
        self.add_done(ep(4), pub="2027-01-01")                # after the pilot end
        self.add_done(ep(5), tier="transcript")               # not full tier
        self.add_done(ep(6), stage="dead")                    # not done
        _, res = self.night()
        self.assertEqual({e for e, _ in self.offered(res["package_id"])}, {ep(1)})
        c = res["counts"]
        self.assertEqual((c["out_of_inventory"], c["out_of_scope"]), (1, 2))

    def test_removed_items_are_never_offered(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        self.add_done(ep(3))
        self.removal.write_text(json.dumps({"episode_ids": [ep(1)], "show_keys": ["show-b"]}))
        _, r1 = self.night()
        self.assertEqual({e for e, _ in self.offered(r1["package_id"])}, {ep(3)})
        self.assertEqual(r1["counts"]["removed"], 2)
        # an item removed while its package waits is withdrawn and never re-offered
        self.removal.write_text(json.dumps({"episode_ids": [ep(1), ep(3)],
                                            "show_keys": ["show-b"]}))
        _, r2 = self.night(days=1)
        self.assertFalse(self.files(r1["package_id"])["tar"].exists())
        self.assertNotIn(r1["package_id"], r2["offered"])
        for pid in r2["offered"]:
            self.assertEqual({e for e, _ in self.offered(pid)}, set())

    def test_empty_night_still_packages_a_status_block(self):
        self.add_done(ep(9), stage="dead")
        code, res = self.night()
        self.assertEqual((code, res["code"]), (0, "ok"))
        mem = self.members(res["package_id"])
        self.assertEqual(set(mem), {"status.json"})
        status = json.loads(mem["status.json"][1])
        self.assertEqual(self.manifest(res["package_id"])["revisions"], [])
        for key in ("queue", "dead", "waiting_retries", "jev_spend", "last_discovery",
                    "free_disk_bytes"):
            self.assertIn(key, status)
        self.assertEqual(status["dead"]["count"], 1)
        self.assertEqual(status["dead"]["episodes"],
                         [{"episode_id": ep(9), "dead_stage": "transcribe",
                           "reason": "transcript_unusable"}])
        self.assertEqual(status["jev_spend"]["day_usd"], "unknown")
        self.assertEqual(status["jev_spend"]["month_usd"], "unknown")
        self.assertNotIn("secret body text", mem["status.json"][1].decode())

    def test_limits_split_work_across_nights(self):
        for n in range(1, 6):
            self.add_done(ep(n), pub=f"2026-10-{10 + n:02d}")
        self.limits = {"max_episodes": 2}
        self.write_config()
        seen = []
        for night in range(3):
            _, res = self.night(days=night)
            got = sorted(self.offered(res["package_id"]), key=lambda x: x[1])
            self.assertLessEqual(len(got), 2)
            seen += [e for e, _ in got]
        self.assertEqual(seen, [ep(n) for n in range(1, 6)])  # oldest first, each once

    def test_byte_limit_bounds_a_package(self):
        for n in range(1, 4):
            self.add_done(ep(n), text=SPONSOR * 20)
        self.limits = {"max_tar_bytes": 40 * 1024}
        self.write_config()
        _, res = self.night()
        self.assertLessEqual(self.files(res["package_id"])["tar"].stat().st_size, 40 * 1024)
        self.assertGreaterEqual(len(self.offered(res["package_id"])), 1)
        self.assertGreater(res["counts"]["held_by_limits"], 0)

    def test_three_unacknowledged_packages_stop_the_build(self):
        self.add_done(ep(1))
        pids = []
        for night in range(3):
            code, res = self.night(days=night)
            self.assertEqual(code, 0)
            pids.append(res["package_id"])
        before = sorted(p.name for p in self.outbox.iterdir())
        code, res = self.night(days=3)
        self.assertEqual(res["code"], "INBOX_BACKLOG")
        self.assertNotEqual(code, 0)
        self.assertIsNone(res.get("package_id"))
        self.assertEqual(sorted(res["offered"]), sorted(pids))
        self.assertEqual(sorted(p.name for p in self.outbox.iterdir()), before)
        con = ed.open_ledger(ed.load_config(self.cfg_path)["ledger"])
        try:
            codes = [r[0] for r in con.execute("SELECT code FROM alerts")]
        finally:
            con.close()
        self.assertIn("INBOX_BACKLOG", codes)

    def test_lost_receipt_offers_the_same_package_bytes_again(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        pid = r1["package_id"]
        before = {k: p.read_bytes() for k, p in self.files(pid).items()}
        _, r2 = self.night(days=1)  # no receipt arrived
        self.assertIn(pid, r2["offered"])
        self.assertEqual({k: p.read_bytes() for k, p in self.files(pid).items()}, before)
        self.assertEqual(self.offered(r2["package_id"]), set())
        # after the 8-day store the current revision is offered in a new package
        _, r3 = self.night(days=9)
        self.assertNotIn(pid, r3["offered"])
        self.assertFalse(self.files(pid)["tar"].exists())
        self.assertEqual({e for e, _ in self.offered(r3["package_id"])}, {ep(1)})

    def test_cli_prints_one_json_line_of_codes_and_counts(self):
        self.add_done(ep(1), text="SECRET TRANSCRIPT WORDS " + SPONSOR)
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "corpus_run.py"),
                              "export-delta", "--config", str(self.cfg_path)],
                             capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual((doc["cmd"], doc["code"]), ("export-delta", "ok"))
        self.assertNotIn("SECRET", out.stdout + out.stderr)


class ExportReceiptTest(ExportDeltaBase):
    def acks(self):
        con = ed.open_ledger(ed.load_config(self.cfg_path)["ledger"])
        try:
            return {(r[0], r[1]): r[2] for r in con.execute(
                "SELECT db_identity, episode_id, revision_seq FROM acks")}
        finally:
            con.close()

    def test_good_receipt_advances_only_the_named_target(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq = self.revision(r1["package_id"], ep(1))["revision_seq"]
        code, res = self.receipt(self.host_receipt(r1, accepted_by=["db-judges"]))
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertEqual(self.acks(), {("db-judges", ep(1)): seq})
        # not acknowledged by every target: offered again in the next package
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered(r2["package_id"]), {(ep(1), seq)})
        self.assertNotIn(r1["package_id"], r2["offered"])
        code, _ = self.receipt(self.host_receipt(r2))
        _, r3 = self.night(days=2)
        self.assertEqual(self.offered(r3["package_id"]), set())

    def test_bad_signature_is_refused(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        code, res = self.receipt(self.host_receipt(r1), sig_key=self.other_key)
        self.assertEqual(res["code"], "refused:BAD_SIGNATURE")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.acks(), {})

    def test_missing_signature_is_refused(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.host_receipt(r1)
        with mock.patch.object(ed, "_now", lambda: self.now):
            code, res = ed.export_receipt(ed.load_config(self.cfg_path), path)
        self.assertEqual(res["code"], "refused:BAD_SIGNATURE")
        self.assertEqual(self.acks(), {})

    def test_wrong_epoch_is_refused(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        code, res = self.receipt(self.host_receipt(r1, epoch=2))
        self.assertEqual(res["code"], "refused:WRONG_EPOCH")
        self.assertEqual(self.acks(), {})

    def test_unknown_db_identity_is_refused(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        code, res = self.receipt(self.host_receipt(r1, extra_targets=["db-elsewhere"]))
        self.assertEqual(res["code"], "refused:UNKNOWN_TARGET")
        self.assertEqual(self.acks(), {})

    def test_deferred_and_refused_revisions(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        seqs = {e: s for e, s in self.offered(r1["package_id"])}
        path = self.host_receipt(
            r1, db_judges={"accepted": [], "deferred": [
                {"episode_id": ep(1), "revision_seq": seqs[ep(1)], "code": "deferred_disk"}],
                "refused": [{"episode_id": ep(2), "revision_seq": seqs[ep(2)],
                             "code": "FETCH_FUTURE"}]})
        self.receipt(path)
        _, r2 = self.night(days=1)
        # deferred: offered again; refused: not again at the same revision
        self.assertEqual(self.offered(r2["package_id"]), {(ep(1), seqs[ep(1)])})
        self.write_transcript(ep(2), 2, SPONSOR + " fixed")
        _, r3 = self.night(days=2)
        self.assertEqual({e for e, _ in self.offered(r3["package_id"])}, {ep(2)})

    def test_duplicate_receipt_changes_nothing(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.host_receipt(r1)
        self.receipt(path)
        code, res = self.receipt(path)
        self.assertEqual((code, res["code"]), (0, "duplicate"))

    def test_reconcile_resets_the_cursor_and_old_receipts_are_refused(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        seqs = {e: s for e, s in self.offered(r1["package_id"])}
        old = self.host_receipt(r1)
        old_copy = self.tmp / "old-receipt.json"
        old_copy.write_bytes(old.read_bytes())
        self.receipt(old)
        self.assertEqual(len(self.acks()), 4)
        # host restored: judges hold ep1 only, pool-1 holds nothing
        rec = self.write_receipt("reconcile-2.json", {
            "kind": "reconcile", "restore_epoch": 2, "targets": [
                {"db_identity": "db-judges",
                 "revisions": [{"episode_id": ep(1), "revision_seq": seqs[ep(1)]}]},
                {"db_identity": "db-pool-1", "revisions": []}]})
        code, res = self.receipt(rec)
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertEqual(self.acks(), {("db-judges", ep(1)): seqs[ep(1)]})
        code, res = self.receipt(old_copy)
        self.assertEqual(res["code"], "refused:WRONG_EPOCH")
        self.assertEqual(self.acks(), {("db-judges", ep(1)): seqs[ep(1)]})
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered(r2["package_id"]), set(seqs.items()))
        self.assertEqual(self.manifest(r2["package_id"])["restore_epoch"], 2)
        # a reconcile at the same or a lower epoch is refused
        again = self.write_receipt("reconcile-1.json", {
            "kind": "reconcile", "restore_epoch": 1, "targets": [
                {"db_identity": "db-judges", "revisions": []}]})
        _, res = self.receipt(again)
        self.assertEqual(res["code"], "refused:WRONG_EPOCH")

    def test_cli_receipt_prints_one_json_line(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.host_receipt(r1)
        sign(self.host_key, path, "earwitness-receipt-v1")
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "corpus_run.py"),
                              "export-receipt", "--config", str(self.cfg_path), str(path)],
                             capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["code"], "ok")


class ExportDeltaReviewR1Test(ExportDeltaBase):
    """Code review r1 (HIGH-1/2, MED-3..6, LOW, NIT)."""

    def ledger(self):
        return ed.open_ledger(ed.load_config(self.cfg_path)["ledger"])

    def revisions_of(self, ep_id):
        con = self.ledger()
        try:
            return [r[0] for r in con.execute(
                "SELECT revision_seq FROM revisions WHERE episode_id=?", (ep_id,))]
        finally:
            con.close()

    # HIGH-1
    def test_another_episodes_jev_model_leaves_a_revision_unchanged(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1))
        self.add_done(ep(2), feed=FEED_B, jev=None)
        self.write_jev(ep(2), model="jev-model-2")
        self.set_episode(ep(2), jev_status="ok")
        _, r2 = self.night(days=1)
        self.assertEqual(r2["counts"]["new_revisions"], 1)
        self.assertEqual(len(self.revisions_of(ep(1))), 1)
        self.assertEqual({e for e, _ in self.offered(r2["package_id"])}, {ep(2)})
        jev = json.loads(self.members(r2["package_id"])[f"{ep(2)}/jev.json"][1])
        self.assertEqual(jev["model_returned"], "jev-model-2")

    def test_a_jev_prompt_or_price_constant_change_leaves_a_revision_unchanged(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1))
        with mock.patch.object(cr.jev_screen, "QUESTION", "another prompt"), \
                mock.patch.object(cr.jev_screen, "INPUT_USD_PER_MTOK", 9.99):
            _, r2 = self.night(days=1)
        self.assertEqual(r2["counts"]["new_revisions"], 0)
        self.assertEqual(len(self.revisions_of(ep(1))), 1)

    def test_a_same_model_rescreen_after_a_prompt_change_ships_the_new_header(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq1 = self.revision(r1["package_id"], ep(1))["revision_seq"]
        self.receipt(self.host_receipt(r1))
        with mock.patch.object(cr.jev_screen, "QUESTION", "another prompt"):
            self.write_jev(ep(1), screened_at="2026-10-23T06:00:00Z")  # re-screen, same model
            _, r2 = self.night(days=1)
        rev2 = self.revision(r2["package_id"], ep(1))
        self.assertGreater(rev2["revision_seq"], seq1)
        jev = json.loads(self.members(r2["package_id"])[f"{ep(1)}/jev.json"][1])
        self.assertEqual(jev["question"], "another prompt")
        self.assertEqual(jev["model_returned"], "jev-model")

    def test_a_ledger_from_before_the_alert_and_header_columns_is_upgraded(self):
        path = ed.load_config(self.cfg_path)["ledger"]
        import sqlite3
        con = sqlite3.connect(str(path))
        con.executescript("CREATE TABLE alerts (id INTEGER PRIMARY KEY, at TEXT NOT NULL,"
                          " code TEXT NOT NULL, counts TEXT);"
                          "CREATE TABLE jev_headers (episode_id TEXT PRIMARY KEY,"
                          " header TEXT NOT NULL);")
        con.close()
        self.add_done(ep(1))
        code, res = self.night()
        self.assertEqual((code, res["code"]), (0, "ok"))
        con = self.ledger()
        try:
            cols = {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
                    for t in ("alerts", "jev_headers")}
        finally:
            con.close()
        self.assertIn("reported", cols["alerts"])
        self.assertIn("entries_sha256", cols["jev_headers"])

    def test_named_reads_of_another_show_leave_a_revision_unchanged(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1))
        for n in (2, 3):  # the same brand recurring in another show
            self.add_done(ep(n), feed=FEED_B)
        _, r2 = self.night(days=1)
        self.assertEqual(len(self.revisions_of(ep(1))), 1)
        self.assertEqual({e for e, _ in self.offered(r2["package_id"])}, {ep(2), ep(3)})

    def test_status_only_change_makes_a_new_revision(self):
        self.add_done(ep(1), names="skipped: no name worker")
        _, r1 = self.night()
        rev1 = self.revision(r1["package_id"], ep(1))
        self.receipt(self.host_receipt(r1))
        self.set_episode(ep(1), name_status="failed: namer crashed")
        _, r2 = self.night(days=1)
        rev2 = self.revision(r2["package_id"], ep(1))
        self.assertEqual(rev2["components"], rev1["components"])
        self.assertNotEqual(rev2["detection_status"], rev1["detection_status"])
        self.assertGreater(rev2["revision_seq"], rev1["revision_seq"])

    # HIGH-2
    def test_work_export_holds_no_media_or_speaker_transcripts(self):
        self.add_done(ep(1))
        clip_dir = self.dirs["clips"] / ep(1)
        clip_dir.mkdir(parents=True)
        (clip_dir / "c0.mp3").write_bytes(b"ID3fake")
        self.write_json(clip_dir / "index.json", {"status": "ok", "clips": [{"file": "c0.mp3"}]})
        self.night()
        work = self.tmp / "work"
        media = [p for p in work.rglob("*") if p.is_file()
                 and (p.suffix == ".mp3" or "clips" in p.parts
                      or "speaker-transcripts" in p.parts)]
        self.assertEqual(media, [])

    def test_work_dir_on_another_volume_is_refused(self):
        self.add_done(ep(1))
        with mock.patch.object(ed, "_same_volume", lambda a, b: False):
            code, res = self.night()
        self.assertEqual(res["code"], "refused:WORK_DIR_VOLUME")
        self.assertNotEqual(code, 0)

    def test_low_disk_skips_the_export_but_packages_the_status_block(self):
        self.add_done(ep(1))
        with mock.patch.object(ed, "_free_bytes", lambda path: 0):
            code, res = self.night()
        self.assertEqual(res["code"], "LOW_DISK")
        self.assertNotEqual(code, 0)
        mem = self.members(res["package_id"])
        self.assertEqual(set(mem), {"status.json"})
        self.assertIn("LOW_DISK", json.loads(mem["status.json"][1])["alerts"])

    # MED-3
    def test_reconcile_with_an_unknown_revision_is_not_acked_and_alerts(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq = self.revision(r1["package_id"], ep(1))["revision_seq"]
        rec = self.write_receipt("reconcile-2.json", {
            "kind": "reconcile", "restore_epoch": 2, "targets": [
                {"db_identity": "db-judges", "revisions": [
                    {"episode_id": ep(1), "revision_seq": seq + 50},
                    {"episode_id": ep(7), "revision_seq": 1}]},
                {"db_identity": "db-pool-1", "revisions": [
                    {"episode_id": ep(1), "revision_seq": seq}]}]})
        code, res = self.receipt(rec)
        self.assertEqual(res["code"], "ok")
        self.assertIn("UNKNOWN_REVISIONS", res["alerts"])
        self.assertEqual(res["counts"]["unknown_revisions"], 2)
        con = self.ledger()
        try:
            acks = {(r[0], r[1]): r[2] for r in con.execute("SELECT * FROM acks")}
        finally:
            con.close()
        self.assertEqual(acks, {("db-pool-1", ep(1)): seq})
        _, r2 = self.night(days=1)
        status = json.loads(self.members(r2["package_id"])["status.json"][1])
        self.assertIn("UNKNOWN_REVISIONS", status["alerts"])

    def test_receipt_with_an_unknown_revision_is_not_acked_and_alerts(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        seq = self.revision(r1["package_id"], ep(1))["revision_seq"]
        path = self.host_receipt(r1, db_judges={"accepted": [
            {"episode_id": ep(1), "revision_seq": seq + 9}]})
        code, res = self.receipt(path)
        self.assertIn("UNKNOWN_REVISIONS", res["alerts"])
        con = self.ledger()
        try:
            acks = {r[0] for r in con.execute("SELECT db_identity FROM acks")}
        finally:
            con.close()
        self.assertEqual(acks, {"db-pool-1"})

    # MED-4
    def test_revision_records_detector_ran_even_with_zero_candidates(self):
        self.add_done(ep(1))
        self.write_json(self.dirs["detections"] / f"{ep(1)}.json", [])
        _, res = self.night()
        rev = self.revision(res["package_id"], ep(1))
        self.assertIs(rev["detector_ran"], True)
        self.assertEqual(rev["detection_status"], "complete")

    def test_member_id_disagreement_skips_the_episode(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        self.add_done(ep(3))
        self.write_json(self.dirs["transcripts"] / f"{ep(1)}.json", {
            "ep_id": ep(1), "id": ep(9), "segments": [{"start": 0, "end": 1, "text": "x"}]})
        dets = json.loads((self.dirs["detections"] / f"{ep(2)}.json").read_text())
        dets[0]["candidate_id"] = f"{ep(9)}.c0"
        self.write_json(self.dirs["detections"] / f"{ep(2)}.json", dets)
        _, res = self.night()
        self.assertEqual({e for e, _ in self.offered(res["package_id"])}, {ep(3)})
        self.assertEqual(res["counts"]["skipped"].get("ID_MISMATCH"), 2)

    # MED-5
    def test_one_bad_episode_does_not_stop_the_night(self):
        self.add_done(ep(1))
        self.add_done(ep(2), detections=False)                 # done, no detections file
        self.add_done(ep(3))
        (self.dirs["transcripts"] / f"{ep(3)}.json").unlink()  # done, no transcript
        self.add_done(ep(4), feed=FEED_X, detections=False)    # outside the inventory, bad too
        code, res = self.night()
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertEqual({e for e, _ in self.offered(res["package_id"])}, {ep(1)})
        self.assertEqual(res["counts"]["skipped"].get("MISSING_DETECTIONS"), 1)
        self.assertEqual(res["counts"]["skipped"].get("MISSING_TRANSCRIPT"), 1)

    # LOW
    def test_receipt_with_no_targets_is_refused(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.write_receipt("empty.json", {
            "kind": "receipt", "restore_epoch": 1,
            "package_digest": self.manifest_sha(r1["package_id"]), "targets": []})
        code, res = self.receipt(path)
        self.assertEqual(res["code"], "refused:EMPTY_TARGETS")

    def test_done_is_written_only_for_a_complete_set(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        f = self.files(r1["package_id"])
        f["done"].unlink()
        f["manifest.json.sig"].unlink()
        self.night(days=1)
        self.assertFalse(f["done"].exists())

    def test_malformed_removal_list_fails_closed(self):
        self.add_done(ep(1))
        self.removal.write_text(json.dumps({"episode_ids": ep(1), "show_keys": []}))
        code, res = self.night()
        self.assertEqual(res["code"], "refused:REMOVAL_LIST_INVALID")
        self.assertEqual(list(self.outbox.glob("p-*")) if self.outbox.exists() else [], [])

    # NIT
    def test_status_lists_only_inventory_and_unremoved_dead_episodes(self):
        self.add_done(ep(1), stage="dead")
        self.add_done(ep(2), stage="dead", feed=FEED_X)
        self.add_done(ep(3), stage="dead", feed=FEED_B)
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-b"]}))
        _, res = self.night()
        status = json.loads(self.members(res["package_id"])["status.json"][1])
        self.assertEqual([d["episode_id"] for d in status["dead"]["episodes"]], [ep(1)])

    def test_duplicate_feed_identity_in_the_inventory_is_refused(self):
        doc = json.loads(self.inventory.read_text())
        doc["shows"].append({**doc["shows"][0], "show_key": "show-a2"})
        self.inventory.write_text(json.dumps(doc))
        _, res = self.night()
        self.assertEqual(res["code"], "refused:INVENTORY_INVALID")


class JevSpendS8R1Test(ExportDeltaBase):
    """Astra step 8 r1 S8R1-12: the status block reads Jev spend through the
    real budget module (JevBudget(ledger, pin).status()), keeping the
    recovery and unknown states."""

    def setUp(self):
        super().setUp()
        from earwitness.pipeline import jev_budget as jb
        self.jb = jb
        self.jledger = self.tmp / "jev" / "jev-budget.sqlite3"
        self.jpin = self.tmp / "jevpin" / "earwitness-jev-budget.pin"
        self.write_config(jev_budget_ledger=str(self.jledger), jev_budget_pin=str(self.jpin))

    def spend(self):
        _, res = self.night()
        mem = self.members(res["package_id"])
        return json.loads(mem["status.json"][1]), mem["status.json"][1].decode()

    def test_healthy_budget_reports_day_and_month_spend(self):
        self.jb.init(self.jledger, self.jpin, first_activation=True)
        b = self.jb.JevBudget(self.jledger, self.jpin)
        r = b.reserve(purpose="screen", episode=ep(1), body_bytes=1_000_000,
                      output_tokens_max=100)
        b.settle(r, 0.03)
        lost = b.reserve(purpose="screen", episode=ep(2), body_bytes=1_000_000,
                         output_tokens_max=100)
        b.mark_unknown(lost)                 # counted at its maximum
        want = b.status()
        status, raw = self.spend()
        js = status["jev_spend"]
        self.assertEqual((js["source"], js["mode"]), ("jev_budget", "normal"))
        self.assertAlmostEqual(js["day_usd"], want["day"]["committed_usd"])
        self.assertAlmostEqual(js["month_usd"], want["month"]["committed_usd"])
        self.assertAlmostEqual(js["day_usd"], 0.03 + lost.max_usd, places=6)
        self.assertEqual(js["unknown"], {"day": 1, "month": 1})
        self.assertEqual((js["day_exhausted"], js["month_exhausted"]), (False, False))
        self.assertNotIn(str(self.jledger), raw)   # codes and amounts only, no paths
        self.assertNotIn(str(self.jpin), raw)

    def test_recovery_mode_is_reported_as_unknown_with_its_code(self):
        self.jb.init(self.jledger, self.jpin, first_activation=True)
        self.jpin.unlink()
        status, raw = self.spend()
        js = status["jev_spend"]
        self.assertEqual((js["source"], js["mode"], js["code"], js["reason"]),
                         ("jev_budget", "recovery", "JEV_BUDGET_RECOVERY", "PIN_MISSING"))
        self.assertEqual((js["day_usd"], js["month_usd"]), ("unknown", "unknown"))
        self.assertNotIn("missing", raw.lower().replace("pin_missing", ""))

    def test_missing_ledger_is_recovery_not_a_healthy_zero(self):
        status, _ = self.spend()
        js = status["jev_spend"]
        self.assertEqual((js["mode"], js["reason"]), ("recovery", "LEDGER_MISSING"))
        self.assertEqual(js["day_usd"], "unknown")

    def test_a_recovered_ledger_reports_the_period_exhausted(self):
        self.jb.init(self.jledger, self.jpin, first_activation=True)
        self.jb.recover(self.jledger, self.jpin, "test restore")
        status, _ = self.spend()
        js = status["jev_spend"]
        self.assertEqual(js["mode"], "normal")
        self.assertEqual((js["day_exhausted"], js["month_exhausted"]), (True, True))

    def test_a_budget_status_error_never_fails_the_night(self):
        self.jb.init(self.jledger, self.jpin, first_activation=True)
        with mock.patch.object(self.jb.JevBudget, "status", side_effect=RuntimeError("x")):
            status, _ = self.spend()
        self.assertEqual(status["jev_spend"]["source"], "unreadable")
        self.assertEqual(status["jev_spend"]["day_usd"], "unknown")


class StageStatusS8R1Test(ExportDeltaBase):
    """Astra step 8 r1 S8R1-05, contract C3 (exporter side): revision.json
    carries the run's jev_status and name_status as stage_status, each cut
    to 64 characters, null when absent."""

    def test_revision_carries_stage_status_for_each_detection_status(self):
        long_fail = "failed: transient: TimeoutError: " + "x" * 200
        self.add_done(ep(1))
        self.add_done(ep(2), jev="jev_skipped_budget", names=None)
        self.add_done(ep(3), jev=long_fail, names="failed: namer crashed")
        _, res = self.night()
        rev = {e: self.revision(res["package_id"], e) for e in (ep(1), ep(2), ep(3))}
        self.assertEqual(rev[ep(1)]["detection_status"], "complete")
        self.assertEqual(rev[ep(1)]["stage_status"], {"jev": "ok", "names": "ok"})
        self.assertEqual(rev[ep(2)]["detection_status"], ed.BUDGET_SKIP)
        self.assertEqual(rev[ep(2)]["stage_status"], {"jev": ed.BUDGET_SKIP, "names": None})
        self.assertEqual(rev[ep(3)]["detection_status"], "jev_failed")
        self.assertEqual(rev[ep(3)]["stage_status"],
                         {"jev": long_fail[:64], "names": "failed: namer crashed"})
        # the fetch row records jev_status in full; its first 64 characters agree
        fetch = json.loads(self.members(res["package_id"])[f"{ep(3)}/fetch.json"][1])
        self.assertEqual(fetch["jev_status"][:64], rev[ep(3)]["stage_status"]["jev"])

    def test_a_stage_status_only_change_makes_a_new_revision(self):
        # same detection_status, same components: the revision doc of one
        # revision_seq never changes, so a new stage_status is a new revision
        # (name_status is in no component, so only stage_status changes here)
        self.add_done(ep(1), jev="failed: transient: timeout", names="failed: namer crashed")
        _, r1 = self.night()
        rev1 = self.revision(r1["package_id"], ep(1))
        self.receipt(self.host_receipt(r1))
        self.set_episode(ep(1), name_status="failed: namer timed out")
        _, r2 = self.night(days=1)
        rev2 = self.revision(r2["package_id"], ep(1))
        self.assertEqual(rev2["components"], rev1["components"])
        self.assertEqual(rev2["detection_status"], rev1["detection_status"])
        self.assertGreater(rev2["revision_seq"], rev1["revision_seq"])
        self.assertEqual(rev2["stage_status"]["names"], "failed: namer timed out")

    def test_a_ledger_without_the_stage_status_column_is_upgraded(self):
        self.add_done(ep(1))
        con = sqlite3.connect(str(self.tmp / "export-ledger.sqlite3"))
        con.executescript(ed.LEDGER_SCHEMA.replace(", stage_status TEXT", ""))
        con.close()
        code, res = self.night()
        self.assertEqual(res["code"], "ok")
        self.assertEqual(self.revision(res["package_id"], ep(1))["stage_status"],
                         {"jev": "ok", "names": "ok"})


class RemovalsS8R1Test(ExportDeltaBase):
    """Astra step 8 r1 S8R1-06, contract C2 (Mini side): a verified host
    removal list is kept as a union in a store apart from the export ledger
    and applied before replay and before any package; a removed episode or
    show is never offered again. An unverifiable or mismatched list is
    refused and stops offering until a valid list arrives."""

    def setUp(self):
        super().setUp()
        self.ledger_path = self.tmp / "export-ledger.sqlite3"

    def rl(self, eps=(), shows=()):
        return {"episode_ids": sorted(set(eps)), "show_keys": sorted(set(shows))}

    def revisions_of(self, ep_id):
        con = ed.open_ledger(self.ledger_path)
        try:
            return [r[0] for r in con.execute(
                "SELECT revision_seq FROM revisions WHERE episode_id=?", (ep_id,))]
        finally:
            con.close()

    def package_state(self, pid):
        con = ed.open_ledger(self.ledger_path)
        try:
            return con.execute("SELECT state FROM packages WHERE package_id=?",
                               (pid,)).fetchone()[0]
        finally:
            con.close()

    def offered_eps(self, res):
        return {e for e, _ in self.offered(res["package_id"])}

    def assert_paused(self, code, res):
        self.assertEqual(res["code"], "REMOVALS_UNVERIFIED")
        self.assertNotEqual(code, 0)
        mem = self.members(res["package_id"])
        self.assertEqual(set(mem), {"status.json"})       # heartbeat only, no revisions
        self.assertIn("REMOVALS_UNVERIFIED", json.loads(mem["status.json"][1])["alerts"])
        self.assertEqual(res["offered"], [res["package_id"]])

    # -- removal by receipt, then higher revisions, reconcile and ledger restore
    def test_a_removed_episode_or_show_is_never_offered_again_at_a_higher_revision(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        self.add_done(ep(3))
        _, r1 = self.night()
        self.assertEqual(self.offered_eps(r1), {ep(1), ep(2), ep(3)})
        code, res = self.receipt(self.host_receipt(r1, removals=self.rl([ep(1)], ["show-b"])))
        self.assertEqual(res["code"], "ok")
        before = (self.revisions_of(ep(1)), self.revisions_of(ep(2)))
        self.write_transcript(ep(1), 1, SPONSOR + " repaired")   # content changes
        self.write_transcript(ep(2), 2, SPONSOR + " repaired")
        self.set_episode(ep(1), name_status="failed: x")
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered_eps(r2), set())
        self.assertEqual((self.revisions_of(ep(1)), self.revisions_of(ep(2))), before)
        self.assertEqual(r2["counts"]["removed"], 2)
        # the local removal file is still an input next to the store
        self.removal.write_text(json.dumps({"episode_ids": [ep(3)], "show_keys": []}))
        self.write_transcript(ep(3), 3, SPONSOR + " repaired")
        _, r3 = self.night(days=2)
        self.assertEqual(self.offered_eps(r3), set())

    def test_new_removals_withdraw_offered_packages_before_any_replay(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.add_done(ep(2))
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered_eps(r2), {ep(2)})
        # the receipt for r1 brings a removal of ep2: r2 is withdrawn at once,
        # before the uploader could offer it again
        _, res = self.receipt(self.host_receipt(r1, removals=self.rl([ep(2)])))
        self.assertEqual(res["code"], "ok")
        self.assertFalse(any(p.exists() for p in self.files(r2["package_id"]).values()))
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        _, r3 = self.night(days=2)
        self.assertEqual(self.offered_eps(r3), set())
        self.assertNotIn(r2["package_id"], r3["offered"])

    def test_reconcile_after_removal_never_reoffers_the_removed_episode(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl([ep(1)])))
        # restore: the host holds nothing; even a list without ep1 never shrinks the union
        rec = self.write_receipt("reconcile-2.json", {
            "kind": "reconcile", "restore_epoch": 2, "targets": [
                {"db_identity": t, "revisions": []} for t in self.targets]},
            removals=self.rl())
        _, res = self.receipt(rec)
        self.assertEqual(res["code"], "ok")
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered_eps(r2), {ep(2)})
        store = json.loads(self.removal_store.read_text())
        self.assertEqual(store["episode_ids"], [ep(1)])

    def test_an_export_ledger_restore_or_loss_keeps_the_removals(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        snap = self.tmp / "ledger-backup"
        snap.mkdir()
        for p in self.tmp.glob("export-ledger.sqlite3*"):
            shutil.copy2(p, snap / p.name)
        self.receipt(self.host_receipt(r1, removals=self.rl([ep(1)])))
        # restore the export ledger from before the receipt
        for p in self.tmp.glob("export-ledger.sqlite3*"):
            p.unlink()
        for p in snap.iterdir():
            shutil.copy2(p, self.tmp / p.name)
        self.write_transcript(ep(1), 1, SPONSOR + " repaired")
        _, r2 = self.night(days=1)
        self.assertNotIn(ep(1), self.offered_eps(r2))
        self.assertNotIn(r1["package_id"], r2["offered"])      # replay held ep1: withdrawn
        self.assertFalse(self.files(r1["package_id"])["tar"].exists())
        # the export ledger lost outright (recreated): still never offered
        for p in self.tmp.glob("export-ledger.sqlite3*"):
            p.unlink()
        shutil.rmtree(self.outbox)
        _, r3 = self.night(days=2)
        self.assertEqual(self.offered_eps(r3), {ep(2)})

    def test_the_store_is_a_union_that_never_shrinks(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl([ep(5)], ["show-z"])))
        _, r2 = self.night(days=1)
        self.receipt(self.host_receipt(r2, removals=self.rl([ep(6)])))
        store = json.loads(self.removal_store.read_text())
        self.assertEqual((store["episode_ids"], store["show_keys"]),
                         ([ep(5), ep(6)], ["show-z"]))
        self.assertFalse(list(self.removal_store.parent.glob("*.part")))

    def test_a_verified_list_in_a_refused_receipt_is_still_kept(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        _, res = self.receipt(self.host_receipt(r1, epoch=5, removals=self.rl([ep(1)])))
        self.assertEqual(res["code"], "refused:WRONG_EPOCH")
        code, r2 = self.night(days=1)
        self.assertEqual((code, r2["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r2), set())
        self.assertNotIn(r1["package_id"], r2["offered"])

    # -- unverifiable or mismatched lists
    def bad_receipt_pauses(self, make):
        self.add_done(ep(1))
        _, r1 = self.night()
        code, res = make(r1)
        self.assertTrue(res["code"].startswith("refused:"), res)
        self.assertNotEqual(code, 0)
        con = ed.open_ledger(self.ledger_path)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM acks").fetchone()[0], 0)
        finally:
            con.close()
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)
        self.assertFalse(self.files(r1["package_id"])["tar"].exists())   # replay stopped too
        return res, r2

    def list_receipt(self, r1, rl, sha=None):
        digest = self.manifest_sha(r1["package_id"])
        doc = {"kind": "receipt", "restore_epoch": 1, "package_digest": digest,
               "targets": [{"db_identity": t, "accepted": [], "stale": [], "refused": [],
                            "deferred": []} for t in self.targets],
               "removal_list": rl}
        doc["removal_list_sha256"] = sha or hashlib.sha256(json.dumps(
            rl, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return self.receipt(self.write_receipt("r.json", doc, raw=True))

    def test_a_receipt_without_a_removal_list_is_refused_and_stops_offering(self):
        def make(r1):
            digest = self.manifest_sha(r1["package_id"])
            path = self.write_receipt("r.json", {
                "kind": "receipt", "restore_epoch": 1, "package_digest": digest,
                "targets": [{"db_identity": t, "accepted": []} for t in self.targets]},
                raw=True)
            return self.receipt(path)
        res, _ = self.bad_receipt_pauses(make)
        self.assertEqual((res["code"], res.get("detail")),
                         ("refused:RECEIPT_INVALID", "removal_list"))

    def test_a_digest_mismatch_is_refused_and_stops_offering(self):
        res, _ = self.bad_receipt_pauses(
            lambda r1: self.list_receipt(r1, self.rl([ep(1)]), sha="0" * 64))
        self.assertEqual((res["code"], res.get("detail")),
                         ("refused:RECEIPT_INVALID", "removal_list"))
        self.assertNotIn(ep(1), json.loads(self.removal_store.read_text())["episode_ids"])

    def test_an_unsorted_or_duplicated_list_is_refused(self):
        res, _ = self.bad_receipt_pauses(lambda r1: self.list_receipt(
            r1, {"episode_ids": [ep(2), ep(1)], "show_keys": []}))
        self.assertEqual(res["code"], "refused:RECEIPT_INVALID")

    def test_a_list_over_50000_entries_is_refused(self):
        big = {"episode_ids": [], "show_keys": ["s%06d" % i for i in range(50_001)]}
        res, _ = self.bad_receipt_pauses(lambda r1: self.list_receipt(r1, big))
        self.assertEqual(res["code"], "refused:RECEIPT_INVALID")

    def test_a_bad_signature_stops_offering(self):
        res, _ = self.bad_receipt_pauses(lambda r1: self.receipt(
            self.host_receipt(r1, removals=self.rl()), sig_key=self.other_key))
        self.assertEqual(res["code"], "refused:BAD_SIGNATURE")

    def test_a_valid_list_after_a_refusal_resumes_offering(self):
        _, r2 = self.bad_receipt_pauses(
            lambda r1: self.list_receipt(r1, self.rl(), sha="f" * 64))
        code, r3 = self.night(days=2)                      # still paused, every night
        self.assert_paused(code, r3)
        code, res = self.receipt(self.host_receipt(r3, removals=self.rl()))
        self.assertEqual(res["code"], "ok")
        code, r4 = self.night(days=3)
        self.assertEqual((code, r4["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r4), {ep(1)})

    def test_a_lost_removal_store_stops_offering_until_a_valid_list(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, accepted_by=[], removals=self.rl([ep(1)])))
        self.removal_store.unlink()
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)
        self.receipt(self.host_receipt(r2, removals=self.rl([ep(1)])))
        code, r3 = self.night(days=2)
        self.assertEqual((code, r3["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r3), {ep(2)})


class BoundedReceiptReadS8R1Test(ExportDeltaBase):
    """Astra step 8 r1 S8R1-07 (exporter part): export-receipt never reads an
    unbounded receipt or signature. Over 16 MiB is refused before the file is
    read whole, whatever its size claims; the signature is bounded too."""
    LIMIT = 16 * 1024 * 1024

    def sparse(self, name, size):
        path = self.tmp / "receipts" / name
        path.parent.mkdir(exist_ok=True)
        with open(path, "wb") as fh:
            fh.truncate(size)
        return path

    def run_receipt(self, path):
        import tracemalloc
        with mock.patch.object(ed, "_now", lambda: self.now):
            tracemalloc.start()
            try:
                code, res = ed.export_receipt(ed.load_config(self.cfg_path), path)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        return code, res, peak

    def test_an_oversized_receipt_is_refused_before_it_is_read(self):
        path = self.sparse("big.json", 256 * 1024 * 1024)
        Path(str(path) + ".sig").write_text("x")
        real = Path.read_bytes

        def guarded(p):
            if p == path:
                raise AssertionError("receipt read whole")
            return real(p)
        with mock.patch.object(Path, "read_bytes", guarded):
            code, res, peak = self.run_receipt(path)
        self.assertEqual(res["code"], "refused:RECEIPT_TOO_LARGE")
        self.assertNotEqual(code, 0)
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_a_receipt_larger_than_its_size_claim_reads_at_most_the_limit(self):
        path = self.sparse("liar.json", 256 * 1024 * 1024)
        Path(str(path) + ".sig").write_text("x")
        real_fstat = os.fstat

        def small(fd):
            st = real_fstat(fd)
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid,
                                   st.st_gid, 100, 0, 0, 0))
        with mock.patch.object(ed.os, "fstat", small):
            code, res, peak = self.run_receipt(path)
        self.assertEqual(res["code"], "refused:RECEIPT_TOO_LARGE")
        self.assertLess(peak, 2 * self.LIMIT + 4 * 1024 * 1024)

    def test_an_oversized_signature_is_refused_without_running_ssh_keygen(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.host_receipt(r1)
        Path(str(path) + ".sig").write_bytes(b"-----BEGIN SSH SIGNATURE-----" + b"A" * 2_000_000)
        real_run = subprocess.run
        calls = []

        def spy(args, *a, **kw):
            calls.append(args)
            return real_run(args, *a, **kw)
        with mock.patch.object(ed.subprocess, "run", spy):
            code, res, _ = self.run_receipt(path)
        self.assertEqual(res["code"], "refused:SIGNATURE_TOO_LARGE")
        self.assertEqual([c for c in calls if "verify" in c], [])

    def test_a_receipt_at_the_limit_is_still_checked(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        path = self.host_receipt(r1)
        doc = path.read_bytes()
        path.write_bytes(doc + b" " * (self.LIMIT - len(doc)))   # exactly 16 MiB, valid JSON
        code, res = self.receipt(path)
        self.assertEqual((code, res["code"]), (0, "ok"))


class RemovalsReviewR1Test(RemovalsS8R1Test):
    """Code review r1 of 94deac7 (S8R1-06 / S8R1-07 rework)."""

    def mark(self):
        con = ed.open_ledger(self.ledger_path)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='removal_store_entries'"
                              ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()

    def store(self):
        return json.loads(self.removal_store.read_text())

    def bad_receipt(self, r1):
        digest = self.manifest_sha(r1["package_id"])
        return self.receipt(self.write_receipt("bad.json", {
            "kind": "receipt", "restore_epoch": 1, "package_digest": digest,
            "targets": [{"db_identity": t} for t in self.targets]}, raw=True))

    # 1 MED: a missing store pauses
    def test_store_and_ledger_lost_together_pause_offering(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, accepted_by=["db-judges"],
                                       removals=self.rl([ep(1)])))
        for p in self.tmp.glob("export-ledger.sqlite3*"):
            p.unlink()
        self.removal_store.unlink()
        shutil.rmtree(self.outbox)
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)
        self.assertEqual(self.store()["paused"], "STORE_MISSING")
        self.receipt(self.host_receipt(r2, removals=self.rl([ep(1)])))
        code, r3 = self.night(days=2)
        self.assertEqual((code, r3["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r3), {ep(2)})

    def test_first_activation_costs_one_status_only_night(self):
        self.removal_store.unlink()
        self.add_done(ep(1))
        code, r1 = self.night()
        self.assert_paused(code, r1)
        code, res = self.receipt(self.host_receipt(r1))
        self.assertEqual(res["code"], "ok")
        code, r2 = self.night(days=1)
        self.assertEqual((code, r2["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r2), {ep(1)})

    # 2 MED: any failure while checking a receipt pauses
    def test_a_verifier_crash_pauses_offering(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        rec = self.host_receipt(r1, removals=self.rl([ep(1)]))
        sign(self.host_key, rec, "earwitness-receipt-v1")
        with mock.patch.object(ed.subprocess, "run",
                               side_effect=FileNotFoundError("ssh-keygen")), \
                mock.patch.object(ed, "_now", lambda: self.now):
            code, res = ed.export_receipt(ed.load_config(self.cfg_path), rec)
        self.assertEqual(res["code"], "failed:FileNotFoundError")
        self.assertNotEqual(code, 0)
        self.assertIsNotNone(self.store()["paused"])
        self.add_done(ep(2))
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)

    def test_a_ledger_failure_before_the_list_is_kept_pauses_offering(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        rec = self.host_receipt(r1, removals=self.rl([ep(1)]))
        sign(self.host_key, rec, "earwitness-receipt-v1")
        with mock.patch.object(ed, "open_ledger",
                               side_effect=sqlite3.OperationalError("disk I/O error")):
            code, res = ed.export_receipt(ed.load_config(self.cfg_path), rec)
        self.assertEqual(res["code"], "failed:OperationalError")
        self.assertIsNotNone(self.store()["paused"])

    # 3 LOW: a damaged store is moved aside and its readable entries kept
    def test_a_damaged_store_is_moved_aside_and_its_readable_entries_kept(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        damaged = json.dumps({"format": "earwitness-mini-removals/1",
                              "episode_ids": [ep(5), 7], "show_keys": "show-z"})
        self.removal_store.write_text(damaged)
        code, res = self.receipt(self.host_receipt(r1, removals=self.rl([ep(6)])))
        self.assertEqual(res["code"], "ok")
        aside = list(self.removal_store.parent.glob(self.removal_store.name + ".damaged-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(), damaged)
        st = self.store()
        self.assertEqual(st["episode_ids"], [ep(5), ep(6)])
        self.assertEqual(st["paused"], "STORE_UNREADABLE")   # the list lacks ep5: still paused
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)
        self.receipt(self.host_receipt(r2, removals=self.rl([ep(5), ep(6)])))
        code, r3 = self.night(days=2)
        self.assertEqual(r3["code"], "ok")

    # 4 LOW: only a current, complete list resumes; the mark never goes down
    def test_an_older_list_does_not_resume_offering(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        old = self.tmp / "older-receipt.json"              # written before the removal
        old.write_bytes(self.host_receipt(r1, accepted_by=["db-pool-1"],
                                          removals=self.rl()).read_bytes())
        self.receipt(self.host_receipt(r1, accepted_by=["db-judges"],
                                       removals=self.rl([ep(9)])))
        _, r2 = self.night(days=1)
        self.bad_receipt(r2)
        code, res = self.receipt(old)                      # out of order, current epoch
        self.assertEqual(res["code"], "ok")
        self.assertIsNotNone(self.store()["paused"])
        code, r3 = self.night(days=2)
        self.assert_paused(code, r3)
        self.receipt(self.host_receipt(r3, removals=self.rl([ep(9)])))
        code, r4 = self.night(days=3)
        self.assertEqual(r4["code"], "ok")

    def test_the_high_water_mark_never_goes_down(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, accepted_by=["db-judges"],
                                       removals=self.rl([ep(8), ep(9)])))
        self.assertEqual(self.mark(), 2)
        self.removal_store.unlink()                         # lost store
        code, r2 = self.night(days=1)
        self.assert_paused(code, r2)
        self.receipt(self.host_receipt(r2, removals=self.rl([ep(9)])))   # a smaller list
        self.assertEqual(self.mark(), 2)
        code, r3 = self.night(days=2)
        self.assert_paused(code, r3)
        self.receipt(self.host_receipt(r3, removals=self.rl([ep(8), ep(9)])))
        code, r4 = self.night(days=3)
        self.assertEqual(r4["code"], "ok")

    # 5 LOW: never block on, or follow, a non-regular receipt
    def test_a_fifo_or_symlink_receipt_is_refused_without_blocking(self):
        import threading
        fifo = self.tmp / "receipts" / "fifo.json"
        fifo.parent.mkdir(exist_ok=True)
        os.mkfifo(fifo)
        Path(str(fifo) + ".sig").write_text("x")
        out = {}

        def run():
            out["r"] = ed.export_receipt(ed.load_config(self.cfg_path), fifo)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "export-receipt blocked on a FIFO")
        self.assertEqual(out["r"][1]["code"], "refused:RECEIPT_UNREADABLE")
        self.add_done(ep(1))
        _, r1 = self.night()
        real = self.host_receipt(r1)
        sign(self.host_key, real, "earwitness-receipt-v1")
        link = self.tmp / "receipts" / "link.json"
        link.symlink_to(real)
        Path(str(link) + ".sig").write_bytes(Path(str(real) + ".sig").read_bytes())
        _, res = ed.export_receipt(ed.load_config(self.cfg_path), link)
        self.assertEqual(res["code"], "refused:RECEIPT_UNREADABLE")


for _name in [n for n in dir(RemovalsS8R1Test) if n.startswith("test_")]:
    setattr(RemovalsReviewR1Test, _name, None)   # helpers only; the parent runs its own


class RemovalsReviewR2Test(RemovalsS8R1Test):
    """Code review r2 of 8e90af6: an unwritable removal store never leaves a
    removed item offered. Offered packages are withdrawn before any pause or
    list is saved; the pause and pending list fall back to the export
    ledger; a night that can save the pause nowhere offers nothing."""

    def meta(self, key):
        con = ed.open_ledger(self.ledger_path)
        try:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def two_packages(self):
        """P1 acknowledged, P2 offered holding ep3."""
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl()))
        self.add_done(ep(3))
        _, r2 = self.night(days=1)
        self.assertEqual(self.offered_eps(r2), {ep(3)})
        return r2

    def gone(self, res):
        return not any(p.exists() for p in self.files(res["package_id"]).values())

    def unwritable_store(self):
        return mock.patch.object(ed, "_write_store", side_effect=PermissionError("ro"))

    def test_a_removal_received_while_the_store_is_unwritable_is_never_offered(self):
        r2 = self.two_packages()
        rec = self.host_receipt(r2, accepted_by=["db-judges"], removals=self.rl([ep(3)]))
        with self.unwritable_store():
            code, res = self.receipt(rec)
        self.assertNotEqual(code, 0)
        self.assertTrue(self.gone(r2))                               # withdrawn first
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        self.assertIsNotNone(self.meta("removals_pause"))
        self.assertIn(ep(3), json.loads(self.meta("removals_pending"))["episode_ids"])
        with self.unwritable_store():                                # still unwritable
            code, r3 = self.night(days=2)
        self.assert_paused(code, r3)
        code, r4 = self.night(days=3)                                # writable again
        self.assert_paused(code, r4)
        self.assertIn(ep(3), self.store()["episode_ids"])            # pending reached the store
        self.receipt(self.host_receipt(r4, removals=self.rl([ep(3)])))
        self.assertIsNone(self.meta("removals_pause"))
        code, r5 = self.night(days=4)
        self.assertEqual((code, r5["code"]), (0, "ok"))
        self.assertNotIn(ep(3), self.offered_eps(r5))

    def test_a_ledger_pause_needs_a_complete_current_list_to_resume(self):
        r2 = self.two_packages()
        with self.unwritable_store():
            self.receipt(self.host_receipt(r2, accepted_by=["db-judges"],
                                           removals=self.rl([ep(3)])))
        _, r3 = self.night(days=2)
        self.receipt(self.host_receipt(r3, removals=self.rl()))      # lacks ep3: no resume
        self.assertIsNotNone(self.meta("removals_pause"))
        code, r4 = self.night(days=3)
        self.assert_paused(code, r4)

    def test_a_lost_and_unwritable_store_withdraws_and_pauses_in_the_ledger(self):
        r2 = self.two_packages()
        self.removal_store.unlink()
        with self.unwritable_store():
            code, r3 = self.night(days=2)
        self.assertTrue(self.gone(r2))
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        self.assert_paused(code, r3)
        self.assertEqual(self.meta("removals_pause"), "STORE_MISSING")

    def test_a_night_that_can_save_the_pause_nowhere_offers_nothing(self):
        r2 = self.two_packages()
        self.removal_store.unlink()
        before = set(p.name for p in self.outbox.iterdir())
        with self.unwritable_store(), \
                mock.patch.object(ed, "_ledger_pause", side_effect=sqlite3.OperationalError("ro")):
            code, r3 = self.night(days=2)
        self.assertNotEqual(code, 0)
        self.assertEqual(r3["code"], "refused:REMOVALS_PAUSE_UNSAVED")
        self.assertIsNone(r3["package_id"])
        self.assertTrue(self.gone(r2))
        self.assertFalse({p.name for p in self.outbox.iterdir()} - before)

    def test_a_receipt_that_can_save_nothing_still_withdraws(self):
        r2 = self.two_packages()
        rec = self.host_receipt(r2, accepted_by=["db-judges"], removals=self.rl([ep(3)]))
        with self.unwritable_store(), \
                mock.patch.object(ed, "_ledger_pause", side_effect=sqlite3.OperationalError("ro")):
            code, res = self.receipt(rec)
        self.assertNotEqual(code, 0)
        self.assertTrue(self.gone(r2))

    def test_a_bad_receipt_with_an_unwritable_store_pauses_in_the_ledger(self):
        r2 = self.two_packages()
        with self.unwritable_store():
            code, res = self.receipt(self.host_receipt(r2, removals=self.rl()),
                                     sig_key=self.other_key)
        self.assertNotEqual(code, 0)
        self.assertTrue(self.gone(r2))
        self.assertEqual(self.meta("removals_pause"), "BAD_SIGNATURE")
        code, r3 = self.night(days=2)
        self.assert_paused(code, r3)

    def test_a_ledger_only_pause_resumes_on_a_complete_receipt(self):
        r2 = self.two_packages()
        good = self.later(r2, removals=self.rl())
        with self.unwritable_store():
            self.receipt(self.host_receipt(r2, removals=self.rl()), sig_key=self.other_key)
        self.assertIsNone(self.store()["paused"])            # only the ledger holds it
        _, res = self.receipt(good)
        self.assertEqual(res["counts"].get("removals_resumed"), 1)
        self.assertIsNone(self.meta("removals_pause"))

    def test_a_night_whose_ledger_write_fails_still_withdraws_first(self):
        r2 = self.two_packages()
        self.removal_store.unlink()
        with self.unwritable_store(), mock.patch.object(
                ed, "_tidy_outbox", side_effect=sqlite3.OperationalError("readonly")):
            code, r3 = self.night(days=2)
        self.assertNotEqual(code, 0)
        self.assertIsNone(r3["package_id"])
        self.assertTrue(self.gone(r2))

    def later(self, r2, **kw):
        """A second receipt for r2, written now (r2 is withdrawn later)."""
        path = self.tmp / f"later-{len(list(self.tmp.glob('later-*')))}.json"
        path.write_bytes(self.host_receipt(r2, **kw).read_bytes())
        return path

    def test_a_complete_receipt_after_a_ledger_pause_resumes_and_clears_it(self):
        r2 = self.two_packages()
        second = self.later(r2, accepted_by=["db-pool-1"], removals=self.rl([ep(3)]))
        rec = self.host_receipt(r2, accepted_by=["db-judges"], removals=self.rl([ep(3)]))
        with self.unwritable_store():
            self.receipt(rec)
        self.assertIsNone(self.store()["paused"])          # the store never saw the pause
        _, res = self.receipt(second)
        self.assertEqual(res["counts"].get("removals_resumed"), 1)
        self.assertIsNone(self.meta("removals_pause"))
        self.assertEqual(self.store()["episode_ids"], [ep(3)])
        code, r3 = self.night(days=2)
        self.assertEqual(r3["code"], "ok")

    def test_a_pending_ledger_list_counts_before_it_reaches_the_store(self):
        r2 = self.two_packages()
        self.add_done(ep(4), stage="dead")
        second = self.later(r2, accepted_by=["db-pool-1"], removals=self.rl())
        rec = self.host_receipt(r2, accepted_by=["db-judges"],
                                removals=self.rl([ep(3), ep(4)]))
        with self.unwritable_store():
            self.receipt(rec)
            _, r3 = self.night(days=2)                     # still unwritable
        status = json.loads(self.members(r3["package_id"])["status.json"][1])
        self.assertNotIn(ep(4), [d["episode_id"] for d in status["dead"]["episodes"]])
        # a list without them never resumes, and they reach the store
        _, res = self.receipt(second)
        self.assertEqual(res["code"], "ok")
        self.assertNotIn("removals_resumed", res["counts"])
        self.assertEqual(self.store()["episode_ids"], [ep(3), ep(4)])
        self.assertIsNotNone(self.meta("removals_pause"))

    # NIT: name_status is re-checked after the export like jev_status
    def test_a_name_status_change_during_the_export_skips_the_episode(self):
        self.add_done(ep(1), names="failed: namer crashed")
        real = cr.export

        def export_then_change(*a, **kw):
            out = real(*a, **kw)
            self.set_episode(ep(1), name_status="ok")
            return out
        with mock.patch.object(ed.cr, "export", export_then_change):
            _, res = self.night()
        self.assertEqual(self.offered_eps(res), set())
        self.assertEqual(res["counts"]["skipped"].get("CHANGED_DURING_EXPORT"), 1)

    def store(self):
        return json.loads(self.removal_store.read_text())


for _name in [n for n in dir(RemovalsS8R1Test) if n.startswith("test_")]:
    setattr(RemovalsReviewR2Test, _name, None)


class RemovalsReviewR3Test(RemovalsReviewR2Test):
    """Code review r3 of 027c829: withdrawal never depends on the current
    inventory; an unsaved receipt pause has its own code; a crash between
    withdrawal and saving still leaves the pause in the ledger."""

    def drop_show_a(self):
        inv = json.loads(self.inventory.read_text())
        inv["shows"] = [x for x in inv["shows"] if x["show_key"] != "show-a"]
        self.inventory.write_text(json.dumps(inv))

    def packaged(self, pid):
        con = ed.open_ledger(self.ledger_path)
        try:
            return json.loads(con.execute("SELECT revisions FROM packages WHERE package_id=?",
                                          (pid,)).fetchone()[0])
        finally:
            con.close()

    # 1 MED
    def test_a_local_show_removal_with_the_show_out_of_inventory_withdraws(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.assertEqual([r["show_key"] for r in self.packaged(r1["package_id"])], ["show-a"])
        self.assertNotIn("show_key", self.manifest(r1["package_id"])["revisions"][0])
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        self.drop_show_a()
        _, r2 = self.night(days=1)
        self.assertEqual(self.package_state(r1["package_id"]), "superseded")
        self.assertTrue(self.gone(r1))
        self.assertNotIn(r1["package_id"], r2["offered"])

    def test_withdrawal_follows_the_packaged_show_not_a_renamed_inventory(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        inv = json.loads(self.inventory.read_text())   # feed A now maps to another key
        for x in inv["shows"]:
            if x["show_key"] == "show-a":
                x["show_key"] = "show-a2"
        self.inventory.write_text(json.dumps(inv))
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        self.night(days=1)
        self.assertTrue(self.gone(r1))
        self.assertEqual(self.package_state(r1["package_id"]), "superseded")

    def test_a_removed_show_still_in_the_inventory_is_withdrawn(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        _, r2 = self.night(days=1)
        self.assertTrue(self.gone(r1))
        self.assertEqual(self.offered_eps(r2), {ep(2)})

    def test_a_show_dropped_from_the_inventory_is_withdrawn(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.drop_show_a()
        _, r2 = self.night(days=1)
        self.assertTrue(self.gone(r1))
        self.assertEqual(self.offered_eps(r2), {ep(2)})

    def test_a_packaged_revision_without_a_stored_show_is_withdrawn(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        con = ed.open_ledger(self.ledger_path)        # a package row from before show_key
        try:
            revs = [{k: v for k, v in r.items() if k != "show_key"}
                    for r in self.packaged(r1["package_id"])]
            con.execute("UPDATE packages SET revisions=? WHERE package_id=?",
                        (json.dumps(revs), r1["package_id"]))
        finally:
            con.close()
        self.set_episode(ep(1), stage="transcribe")   # no longer a done corpus row
        _, r2 = self.night(days=1)
        self.assertTrue(self.gone(r1))
        self.assertEqual(self.package_state(r1["package_id"]), "superseded")

    # 2 LOW
    def test_an_unverifiable_receipt_whose_pause_is_saved_nowhere_says_so(self):
        r2 = self.two_packages()
        with self.unwritable_store(), \
                mock.patch.object(ed, "_ledger_pause", side_effect=OSError("ro")), \
                mock.patch.object(ed, "_ledger_withdraw_pause",
                                  side_effect=sqlite3.OperationalError("ro")):
            code, res = self.receipt(self.host_receipt(r2, removals=self.rl()),
                                     sig_key=self.other_key)
        self.assertEqual((code, res["code"]), (ed.EXIT_REFUSED,
                                               "refused:REMOVALS_PAUSE_UNSAVED"))
        self.assertTrue(self.gone(r2))

    def test_the_ledger_pause_is_saved_with_the_withdrawal(self):
        # a store failure and a failing ledger fallback after the withdrawal:
        # the pause went in with the supersede mark, so the night still pauses
        r2 = self.two_packages()
        with self.unwritable_store(), \
                mock.patch.object(ed, "_ledger_pause", side_effect=OSError("ro")):
            code, res = self.receipt(self.host_receipt(r2, removals=self.rl()),
                                     sig_key=self.other_key)
        self.assertEqual(res["code"], "refused:BAD_SIGNATURE")
        self.assertEqual(self.meta("removals_pause"), "BAD_SIGNATURE")
        code, r3 = self.night(days=2)
        self.assert_paused(code, r3)

    def test_a_crash_between_withdrawal_and_saving_still_pauses(self):
        r2 = self.two_packages()

        class Crash(BaseException):
            pass
        with mock.patch.object(ed, "_persist_pause", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.receipt(self.host_receipt(r2, removals=self.rl()), sig_key=self.other_key)
        self.assertTrue(self.gone(r2))
        code, r3 = self.night(days=2)
        self.assert_paused(code, r3)


for _name in [n for n in dir(RemovalsReviewR2Test) if n.startswith("test_")]:
    setattr(RemovalsReviewR3Test, _name, None)


class RemovalsReviewR4Test(RemovalsReviewR3Test):
    """Code review r4 of e6efdaa: a show removal follows every show key a
    feed or episode ever had (renames), when building and when withdrawing;
    a package file that cannot be deleted never stops the pause."""

    def rename_a(self, new="show-a2"):
        inv = json.loads(self.inventory.read_text())
        for x in inv["shows"]:
            if x["show_key"] == "show-a":
                x["show_key"] = new
        self.inventory.write_text(json.dumps(inv))

    def all_offered(self, res):
        return {e for pid in res["offered"] for e, _ in self.offered(pid)}

    # 1 MED
    def test_a_local_show_removal_survives_a_feed_rename(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl()))
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        self.night(days=1)
        self.rename_a()
        self.write_transcript(ep(1), 1, SPONSOR + " repaired")   # a new revision of ep1
        self.add_done(ep(6))                                       # new under show-a2
        _, r3 = self.night(days=2)
        self.assertEqual(r3["code"], "ok")
        self.assertEqual(self.all_offered(r3) & {ep(1), ep(6)}, set())

    def test_a_host_show_removal_survives_a_feed_rename(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl(shows=["show-a"])))
        self.add_done(ep(5))
        _, r2 = self.night(days=1)
        self.assertNotIn(ep(5), self.offered_eps(r2))
        self.rename_a()
        self.add_done(ep(6))
        _, r3 = self.night(days=2)
        self.assertEqual(r3["code"], "ok")
        self.assertEqual(self.all_offered(r3) & {ep(1), ep(5), ep(6)}, set())

    def test_withdrawal_follows_a_shows_earlier_keys(self):
        self.add_done(ep(1))
        _, r1 = self.night()                                       # feed A seen as show-a
        self.receipt(self.host_receipt(r1, removals=self.rl()))
        self.rename_a()
        self.add_done(ep(7))
        _, r2 = self.night(days=1)                                 # ep7 packaged as show-a2
        self.assertEqual(self.offered_eps(r2), {ep(7)})
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        _, r3 = self.night(days=2)
        self.assertTrue(self.gone(r2))
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        self.assertNotIn(ep(7), self.all_offered(r3))

    def test_the_show_history_survives_reopening_the_ledger(self):
        self.add_done(ep(1))
        self.night()
        con = ed.open_ledger(self.ledger_path)
        try:
            got = {tuple(r) for r in con.execute("SELECT * FROM feed_shows")}
        finally:
            con.close()
        self.assertEqual(got, {(FEED_A, "show-a"), (FEED_B, "show-b")})

    def test_an_episode_whose_feed_moved_keeps_its_earlier_shows(self):
        self.add_done(ep(1))
        _, r1 = self.night()                                       # ep1 seen as show-a
        self.receipt(self.host_receipt(r1, removals=self.rl()))
        self.set_episode(ep(1), feed_url=FEED_B)                   # now under show-b
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        self.write_transcript(ep(1), 1, SPONSOR + " repaired")
        _, r2 = self.night(days=1)
        self.assertNotIn(ep(1), self.all_offered(r2))

    def test_the_status_dead_list_follows_a_renamed_removed_show(self):
        self.add_done(ep(1))
        self.add_done(ep(9), stage="dead")
        self.night()
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        self.rename_a()
        _, r2 = self.night(days=1)
        status = json.loads(self.members(r2["package_id"])["status.json"][1])
        self.assertNotIn(ep(9), [d["episode_id"] for d in status["dead"]["episodes"]])

    # 2 LOW
    def failing_unlink(self):
        real = Path.unlink

        def bad(p, *a, **k):
            if p.name.startswith("p-") and p.parent == self.outbox:
                raise PermissionError("EPERM")
            return real(p, *a, **k)
        return mock.patch.object(Path, "unlink", bad)

    def test_an_undeletable_package_file_still_pauses_and_stops(self):
        r2 = self.two_packages()
        with self.failing_unlink():
            code, res = self.receipt(self.host_receipt(r2, removals=self.rl()),
                                     sig_key=self.other_key)
        self.assertEqual((code, res["code"]), (ed.EXIT_REFUSED, "refused:WITHDRAW_INCOMPLETE"))
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        self.assertEqual(self.meta("removals_pause"), "BAD_SIGNATURE")
        self.assertEqual(self.store()["paused"], "BAD_SIGNATURE")
        code, r3 = self.night(days=2)                              # deletes the leftovers
        self.assert_paused(code, r3)
        self.assertTrue(self.gone(r2))

    def test_a_valid_receipt_whose_withdrawal_left_files_says_so_and_tidy_retries(self):
        r2 = self.two_packages()
        # a valid receipt deferring ep3, whose list grows (ep9): every offered
        # package is withdrawn, though none holds a removed item
        deferred = [{"episode_id": e, "revision_seq": s, "code": "deferred_disk"}
                    for e, s in self.offered(r2["package_id"])]
        rec = self.host_receipt(r2, removals=self.rl([ep(9)]),
                                db_judges={"accepted": [], "deferred": deferred},
                                db_pool_1={"accepted": [], "deferred": deferred})
        with self.failing_unlink():
            code, res = self.receipt(rec)
        self.assertEqual((code, res["code"]), (ed.EXIT_REFUSED, "refused:WITHDRAW_INCOMPLETE"))
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        self.assertFalse(self.gone(r2))
        code, r3 = self.night(days=2)                              # not paused, nothing removed
        self.assertEqual((code, r3["code"]), (0, "ok"))
        self.assertTrue(self.gone(r2))                             # tidy retried the deletes

    def test_a_night_that_cannot_delete_a_withdrawn_package_stops(self):
        r2 = self.two_packages()
        self.removal.write_text(json.dumps({"episode_ids": [ep(3)], "show_keys": []}))
        with self.failing_unlink():
            code, r3 = self.night(days=2)
        self.assertEqual((code, r3["code"]), (ed.EXIT_REFUSED, "refused:WITHDRAW_INCOMPLETE"))
        self.assertIsNone(r3["package_id"])
        self.assertEqual(self.package_state(r2["package_id"]), "superseded")
        code, r4 = self.night(days=3)
        self.assertEqual(r4["code"], "ok")
        self.assertTrue(self.gone(r2))


for _name in [n for n in dir(RemovalsReviewR3Test) if n.startswith("test_")]:
    setattr(RemovalsReviewR4Test, _name, None)


class RemovalsReviewR5Test(RemovalsReviewR4Test):
    """Code review r5 of 3741820: the show-key history is also kept in the
    removal store, so an export-ledger loss cannot unlink a renamed feed
    from a removed show."""

    def lose_ledger(self):
        for p in self.tmp.glob("export-ledger.sqlite3*"):
            p.unlink()
        shutil.rmtree(self.outbox)

    def test_a_seen_rename_then_removal_survives_a_ledger_loss(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.rename_a()
        _, r2 = self.night(days=1)
        self.receipt(self.host_receipt(r2, removals=self.rl(shows=["show-a"])))
        self.lose_ledger()
        _, r4 = self.night(days=3)
        self.assertEqual(r4["code"], "ok")
        self.assertNotIn(ep(1), self.offered_eps(r4))
        self.assertEqual(self.store()["feed_shows"][FEED_A], ["show-a", "show-a2"])

    def test_a_removal_then_rename_survives_a_ledger_loss(self):
        self.add_done(ep(1))
        self.add_done(ep(2), feed=FEED_B)
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl(shows=["show-a"])))
        self.rename_a()
        self.night(days=1)
        self.lose_ledger()
        self.add_done(ep(7))
        _, r3 = self.night(days=2)
        self.assertEqual(r3["code"], "ok")
        self.assertEqual(self.offered_eps(r3) & {ep(1), ep(7)}, set())

    def test_a_moved_episode_keeps_its_shows_after_a_ledger_loss(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.receipt(self.host_receipt(r1, removals=self.rl()))
        self.set_episode(ep(1), feed_url=FEED_B)
        self.lose_ledger()
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        _, r2 = self.night(days=1)
        self.assertNotIn(ep(1), self.offered_eps(r2))

    def test_the_history_is_not_part_of_the_complete_check_or_the_mark(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.removal_store.unlink()                        # pause: STORE_MISSING
        _, r2 = self.night(days=1)
        self.assertTrue(self.store()["feed_shows"])        # history kept with the pause
        _, res = self.receipt(self.host_receipt(r2, removals=self.rl()))
        self.assertEqual(res["counts"].get("removals_resumed"), 1)
        code, r3 = self.night(days=2)
        self.assertEqual((code, r3["code"]), (0, "ok"))

    def test_an_unwritable_store_copy_of_the_history_alerts_but_the_night_goes_on(self):
        self.add_done(ep(1))
        with self.unwritable_store():
            code, r1 = self.night()
        self.assertEqual((code, r1["code"]), (0, "ok"))
        self.assertEqual(self.offered_eps(r1), {ep(1)})
        status = json.loads(self.members(r1["package_id"])["status.json"][1])
        self.assertIn("STORE_HISTORY_UNSAVED", status["alerts"])
        self.night(days=1)                                 # writable again: saved
        self.assertEqual(self.store()["feed_shows"][FEED_A], ["show-a"])


for _name in [n for n in dir(RemovalsReviewR4Test) if n.startswith("test_")]:
    setattr(RemovalsReviewR5Test, _name, None)


class ExportTidyTest(RemovalsReviewR4Test):
    """UPLOAD review r5: `export-tidy` runs exactly the withdrawal/tidy that
    export-delta runs before building (union removals with history, local
    removal file, inventory, pause, delete retries) and builds nothing."""

    def tidy(self):
        self.now = self.now + timedelta(hours=1)
        with mock.patch.object(ed, "_now", lambda: self.now):
            code, res = ed.export_tidy(ed.load_config(self.cfg_path))
        json.dumps(res)
        return code, res

    def outbox_names(self):
        return sorted(p.name for p in self.outbox.iterdir())

    def test_a_clean_outbox_is_ok_and_nothing_is_built(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.add_done(ep(2))                               # new work is not packaged
        before = self.outbox_names()
        code, res = self.tidy()
        self.assertEqual((code, res["code"], res["cmd"]), (0, "ok", "export-tidy"))
        self.assertEqual(res["offered"], [r1["package_id"]])
        self.assertEqual(self.outbox_names(), before)

    def test_a_local_removal_after_the_build_is_withdrawn(self):
        self.add_done(ep(1))
        self.add_done(ep(2))
        _, r1 = self.night()
        self.removal.write_text(json.dumps({"episode_ids": [ep(1)], "show_keys": []}))
        code, res = self.tidy()
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertTrue(self.gone(r1))
        self.assertEqual(self.package_state(r1["package_id"]), "superseded")
        self.assertEqual(res["offered"], [])

    def test_inventory_narrowing_and_a_renamed_removed_show_are_withdrawn(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.drop_show_a()
        code, res = self.tidy()
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertTrue(self.gone(r1))
        self.inventory.write_text(json.dumps({"shows": [
            {"show_key": "show-a2", "feed_identity": FEED_A, "feed_url": FEED_A},
            {"show_key": "show-b", "feed_identity": FEED_B, "feed_url": FEED_B}]}))
        self.add_done(ep(7))
        _, r2 = self.night(days=1)                         # ep7 packaged as show-a2
        self.removal.write_text(json.dumps({"episode_ids": [], "show_keys": ["show-a"]}))
        code, res = self.tidy()
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertTrue(self.gone(r2))

    def test_a_paused_store_withdraws_everything_offered(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.write_store(paused="BAD_SIGNATURE")
        code, res = self.tidy()
        self.assertEqual((code, res["code"]), (ed.EXIT_BACKLOG, "REMOVALS_UNVERIFIED"))
        self.assertTrue(self.gone(r1))
        self.assertEqual(res["offered"], [])
        self.assertIsNone(res.get("package_id"))

    def test_an_undeletable_withdrawn_file_stops(self):
        self.add_done(ep(1))
        _, r1 = self.night()
        self.removal.write_text(json.dumps({"episode_ids": [ep(1)], "show_keys": []}))
        with self.failing_unlink():
            code, res = self.tidy()
        self.assertEqual((code, res["code"]), (ed.EXIT_REFUSED, "refused:WITHDRAW_INCOMPLETE"))
        self.assertEqual(self.package_state(r1["package_id"]), "superseded")
        code, res = self.tidy()                            # retried
        self.assertEqual((code, res["code"]), (0, "ok"))
        self.assertTrue(self.gone(r1))

    def test_a_busy_ledger_is_refused(self):
        self.add_done(ep(1))
        self.night()
        with ed._Locked(self.ledger_path):
            code, res = self.tidy()
        self.assertEqual((code, res["code"]), (ed.EXIT_REFUSED, "refused:LEDGER_BUSY"))

    def test_cli_prints_one_json_line(self):
        self.add_done(ep(1))
        self.night()
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "corpus_run.py"),
                              "export-tidy", "--config", str(self.cfg_path)],
                             capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["code"], "ok")
        bad = self.tmp / "bad-config.json"
        bad.write_text("{}")
        out = subprocess.run([sys.executable, str(REPO / "scripts" / "corpus_run.py"),
                              "export-tidy", "--config", str(bad)],
                             capture_output=True, text=True, cwd=str(REPO))
        self.assertEqual((out.returncode, json.loads(out.stdout)["code"]),
                         (3, "refused:CONFIG_INVALID"))


for _name in [n for n in dir(RemovalsReviewR4Test) if n.startswith("test_")]:
    setattr(ExportTidyTest, _name, None)


if __name__ == "__main__":
    unittest.main()
