"""AD-05 follow-up (plan 2026-10-19 section 3.10): a NEW occurrence links the
observation's current revision, checked inside the same write transaction.

"Current revision" means ad_observations.revision (the observation's own
revision history), never transcript freshness. Existing stored links to older
revisions stay valid, and map_refetch / mark_ambiguous keep their rules.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import campaigns, citations, creatives, db, observations, ops
from tests.test_customer_campaigns import (add_candidate, add_placement, seed_campaign,
                                           seed_episode, verify_full_ad)
from tests.test_product_observations import fresh_db, propose, seed


def _links(con, observation_id):
    return [r["basis"] for r in con.execute(
        "SELECT basis FROM occurrence_observations WHERE observation_id=?"
        " ORDER BY rowid", (observation_id,)).fetchall()]


class CurrentRevisionTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        vid = seed(self.con)
        self.obs = propose(self.con, vid)["observation_id"]
        # A human review adds observation revision 2; revision 1 stays stored.
        observations.review_observation(self.con, self.obs, "reviewed_ad", 1)
        self.con.commit()

    def tearDown(self):
        self.con.close()

    def test_stale_revision_is_refused_naming_the_current_revision(self):
        audits = self.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        with self.assertRaises(ops.BadInput) as caught:
            creatives.create_occurrence(self.con, "ep-1", self.obs, 1)
        message = str(caught.exception)
        self.assertIn("current revision is 2", message)
        self.assertIn("stale observation revision 1", message)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 0)
        self.assertEqual(_links(self.con, self.obs), [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
                         audits)

    def test_current_revision_is_accepted(self):
        out = creatives.create_occurrence(self.con, "ep-1", self.obs, 2)
        self.assertEqual(out["correspondence"], "confirmed")
        self.assertEqual(_links(self.con, self.obs), ["manual:obs_rev=2"])

    def test_invalid_links_are_still_refused(self):
        self.con.execute("INSERT INTO episodes(id,kind,updated_at) VALUES('other','supplied','now')")
        self.con.commit()
        for bad in (999, 0, -1, True, "2", 2.0, None):
            with self.assertRaises(ops.BadInput):
                creatives.create_occurrence(self.con, "ep-1", self.obs, bad)
        with self.assertRaises(ops.BadInput):
            creatives.create_occurrence(self.con, "other", self.obs, 2)
        with self.assertRaises(ops.BadInput):
            creatives.create_occurrence(self.con, "ep-1", "obs:missing", 2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 0)

    def test_refetch_and_ambiguous_keep_accepting_an_older_existing_revision(self):
        # A fresh occurrence (linked to another observation) so the refetch
        # actually stores its own revision-1 link to self.obs.
        vid = self.con.execute("SELECT transcript_version_id FROM episodes"
                               " WHERE id='ep-1'").fetchone()[0]
        other = propose(self.con, vid, start=0.0, end=10.0)["observation_id"]
        occ = creatives.create_occurrence(self.con, "ep-1", other, 1)["occurrence_id"]
        same = creatives.map_refetch(self.con, occ, self.obs, 1, media_digest="d1",
                                     source_interval="10-30", episode_id="ep-1",
                                     same_bytes=True, same_interval=True)
        self.assertEqual(same["correspondence"], "confirmed")
        amb = creatives.mark_ambiguous(self.con, self.obs, 1, "ep-1", "older read")
        self.assertEqual(amb["correspondence"], "ambiguous")
        self.assertEqual(_links(self.con, self.obs),
                         ["refetch-same:obs_rev=1", "ambiguous:older read:obs_rev=1"])

    def test_old_stored_link_still_loads(self):
        occ = creatives.create_occurrence(self.con, "ep-1", self.obs, 2)["occurrence_id"]
        observations.review_observation(self.con, self.obs, "reviewed_ad", 2)
        self.con.commit()
        self.assertEqual(self.con.execute("SELECT revision FROM ad_observations WHERE id=?",
                                          (self.obs,)).fetchone()[0], 3)
        counts = creatives.occurrence_counts(self.con, "ep-1")
        self.assertEqual(counts["exact_total"], 1)
        links = observations.affected_relations(self.con, self.obs)["occurrence_links"]
        self.assertEqual(links, [{"occurrence_id": occ, "basis": "manual:obs_rev=2"}])
        # A new occurrence now needs revision 3; the stored rev-2 link is untouched.
        with self.assertRaises(ops.BadInput):
            creatives.create_occurrence(self.con, "ep-1", self.obs, 2)
        self.assertEqual(_links(self.con, self.obs), ["manual:obs_rev=2"])


class OldLinkRendersInDraftTest(unittest.TestCase):
    """A stored link to an older observation revision still renders in the
    customer campaign draft as the same persisted delivery identity."""

    def setUp(self):
        from tests.test_customer_campaigns import fresh_db as campaign_db
        self.con = campaign_db()
        seed_episode(self.con)
        self.cid = seed_campaign(self.con)

    def tearDown(self):
        self.con.close()

    def test_draft_keeps_the_old_link(self):
        pid = add_placement(self.con, self.cid, expected_count=1, episode_id="ep-1")
        add_candidate(self.con, "x")
        link = verify_full_ad(self.con, "x")  # occurrence linked at revision 2
        rev = self.con.execute("SELECT revision FROM campaigns WHERE id=?",
                               (self.cid,)).fetchone()[0]
        campaigns.save_placement_evidence(
            self.con, self.cid, pid, citations.current_source_ref(self.con, "evidence", "x"),
            rev)
        occ = self.con.execute("SELECT id FROM occurrences").fetchone()[0]
        before = campaigns.build_customer_draft(self.con, self.cid)["allocation"]
        self.assertEqual(before["evaluations"][pid]["eligible_ids"], [occ])
        observations.review_observation(self.con, link["observation_id"], "reviewed_ad", 2,
                                        actor="owner")
        self.con.commit()
        self.assertEqual(_links(self.con, link["observation_id"]), ["manual:obs_rev=2"])
        after = campaigns.build_customer_draft(self.con, self.cid)["allocation"]
        self.assertEqual(after["evaluations"][pid]["eligible_ids"], [occ])
        self.assertEqual(after["evaluations"][pid]["outcome"],
                         before["evaluations"][pid]["outcome"])


class ConcurrentRevisionTest(unittest.TestCase):
    """Two connections on one WAL file database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.a = db.connect(Path(self._tmp.name))
        db.init_schema(self.a)
        vid = seed(self.a)
        self.obs = propose(self.a, vid)["observation_id"]
        self.b = db.connect(Path(self._tmp.name))
        self.b.execute("PRAGMA busy_timeout=100;")

    def tearDown(self):
        self.a.close()
        self.b.close()
        self._tmp.cleanup()

    def _current(self, con):
        return con.execute("SELECT revision FROM ad_observations WHERE id=?",
                           (self.obs,)).fetchone()[0]

    def test_revision_committed_after_the_callers_read_is_refused(self):
        seen = observations.observation_detail(self.a, self.obs)["revision"]
        self.assertEqual(seen, 1)
        observations.review_observation(self.b, self.obs, "reviewed_ad", 1)
        with self.assertRaises(ops.BadInput) as caught:
            creatives.create_occurrence(self.a, "ep-1", self.obs, seen)
        self.assertIn("current revision is 2", str(caught.exception))
        self.assertEqual(self.a.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 0)

    def test_revision_attempted_between_check_and_insert_waits_for_the_write(self):
        """Proves which: the check and the insert share one BEGIN IMMEDIATE
        transaction, so a concurrent revision cannot commit between them. It
        is refused by the write lock, the insert links the revision that is
        still current at commit, and the retried revision lands afterwards."""
        attempts = []
        real_now = creatives._now

        def interleave():
            # Runs after the link and currentness checks, before the insert.
            if not attempts:
                try:
                    observations.review_observation(self.b, self.obs, "reviewed_ad", 1)
                    attempts.append("committed")
                except sqlite3.OperationalError as exc:
                    attempts.append(f"refused: {exc}")
            return real_now()

        with mock.patch.object(creatives, "_now", interleave):
            out = creatives.create_occurrence(self.a, "ep-1", self.obs, 1)
        self.assertEqual(attempts, ["refused: database is locked"])
        self.assertEqual(_links(self.a, self.obs), ["manual:obs_rev=1"])
        self.assertEqual(self._current(self.b), 1)  # current when the insert committed
        later = observations.review_observation(self.b, self.obs, "reviewed_ad", 1)
        self.assertEqual(later["revision"], 2)
        self.assertGreater(later["aggregate_rev"], out["aggregate_rev"])
        # The stored rev-1 link stays valid; a new occurrence needs revision 2.
        with self.assertRaises(ops.BadInput):
            creatives.create_occurrence(self.a, "ep-1", self.obs, 1)
        self.assertEqual(_links(self.a, self.obs), ["manual:obs_rev=1"])

    def test_nested_caller_deferred_transaction_refuses_with_lock_error(self):
        """Inside a caller's deferred transaction create_occurrence is a
        SAVEPOINT. A revision committed by another connection after the
        caller's snapshot cannot produce a stale link: the insert's write
        upgrade fails with OperationalError, and nothing is stored."""
        self.a.execute("BEGIN")  # caller-owned deferred transaction
        self.assertEqual(self._current(self.a), 1)  # snapshot taken at rev 1
        observations.review_observation(self.b, self.obs, "reviewed_ad", 1)
        self.assertEqual(self._current(self.b), 2)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            creatives.create_occurrence(self.a, "ep-1", self.obs, 1)
        self.assertIn("database is locked", str(caught.exception))
        self.a.rollback()
        self.assertEqual(self.b.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 0)
        # Outside the stale snapshot the same call gets the BadInput message.
        with self.assertRaises(ops.BadInput) as stale:
            creatives.create_occurrence(self.a, "ep-1", self.obs, 1)
        self.assertIn("current revision is 2", str(stale.exception))


if __name__ == "__main__":
    unittest.main()
