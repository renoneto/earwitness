"""K03 source publication/retention/media regression tests (red-capable).

Publications are immutable rows written in producer transactions;
resolution uses pinned components, never latest-state substitution.
All fixtures are synthetic with genuine hashes; no corpus is read.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from earwitness.app import citations as _citations
from earwitness.app import customer as _customer
from earwitness.app import db as _db
from earwitness.app import ops as _ops
from tests import backend_fixtures


def _con():
    con = backend_fixtures.make_memory_workspace()
    backend_fixtures.publish_sources(con)
    return con


class PublicationTest(unittest.TestCase):
    def test_publish_reuses_identical_projection(self):
        con = _con()
        try:
            before = con.execute("SELECT COUNT(*) FROM source_states"
                                 ).fetchone()[0]
            first = _citations.current_source_ref(con, "evidence",
                                                  "fixture-ad")
            again = _citations.publish_source_state(
                con, kind="evidence", record_id="fixture-ad", actor="t")
            self.assertEqual(again, first)
            after = con.execute("SELECT COUNT(*) FROM source_states"
                                ).fetchone()[0]
            self.assertEqual(after, before)
        finally:
            con.close()

    def test_private_note_only_leaves_ref_unchanged(self):
        con = _con()
        try:
            before = _citations.current_source_ref(con, "evidence",
                                                   "fixture-ad")
            _ops.save_review(con, "fixture-ad", {"notes": "private note only"},
                             1, "tester")
            self.assertEqual(
                _citations.current_source_ref(con, "evidence", "fixture-ad"),
                before)
        finally:
            con.close()

    def test_review_reset_rotates_ref(self):
        con = _con()
        try:
            before = _citations.current_source_ref(con, "evidence",
                                                   "fixture-ad")
            _ops.save_review(con, "fixture-ad", {"judgment": "ad"}, 1,
                             "tester")
            after = _citations.current_source_ref(con, "evidence",
                                                  "fixture-ad")
            self.assertNotEqual(after, before)
            resolved = _citations.resolve_source_state(con, before)
            self.assertEqual(resolved["canonical_source_ref"], after)
            self.assertEqual(resolved["ref"], before)
        finally:
            con.close()

    def test_assoc_change_rotates_ref(self):
        con = _con()
        try:
            before = _citations.current_source_ref(con, "evidence",
                                                   "fixture-ad")
            _ops.set_brand_assocs(con, "fixture-ad",
                                  [{"brand": "Lumen", "basis": "confirmed",
                                    "origin": "detector", "channel": "url"}],
                                  0, "tester")
            after = _citations.current_source_ref(con, "evidence",
                                                  "fixture-ad")
            self.assertNotEqual(after, before)
        finally:
            con.close()

    def test_unknown_ref_is_unavailable_not_substituted(self):
        con = _con()
        try:
            with self.assertRaises(_citations.RevisionUnavailable):
                _citations.resolve_source_state(
                    con, "sr1:" + "0" * 32)
            with self.assertRaises(_citations.CitationError):
                _citations.resolve_source_state(con, "ev:fixture-ad@v1")
        finally:
            con.close()

    def test_resolve_returns_captured_text_and_bounds(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence", "fixture-ad")
            out = _citations.resolve_source_state(con, ref)
            self.assertEqual(out["canonical_source_ref"], ref)
            self.assertIn("Lumen", out["text"])
            self.assertEqual(out["original_bounds"], {"start_s": 10.0, "end_s": 20.0})
            self.assertEqual(out["effective_bounds"], {"start_s": 10.0, "end_s": 20.0})
            self.assertEqual(out["bounds_basis"], "original")
            self.assertEqual(out["relationship"], "current")
            self.assertEqual(out["availability"], "available")
            self.assertEqual(out["media"]["timing"], "unknown")
        finally:
            con.close()

    def test_resolve_survives_live_row_removal(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence", "fixture-ad")
            con.execute("DELETE FROM brand_assocs WHERE evidence_id='fixture-ad'")
            con.execute("DELETE FROM reviews WHERE evidence_id='fixture-ad'")
            con.execute("DELETE FROM evidence WHERE id='fixture-ad'")
            con.commit()
            out = _citations.resolve_source_state(con, ref)
            self.assertIn("Lumen", out["text"])
            self.assertEqual(out["relationship"], "historical")
            self.assertEqual(out["availability"], "source_removed")
        finally:
            con.close()

    def test_foreign_version_tuple_denied(self):
        con = _con()
        try:
            e2ver = con.execute("SELECT transcript_version_id FROM episodes"
                                " WHERE id='e2'").fetchone()[0]
            with self.assertRaises(_citations.CitationError):
                _citations.transcript_citation(con, "e1", e2ver)
            ok = _citations.transcript_citation(con, "e2", e2ver)
            self.assertEqual(ok["episode_id"], "e2")
        finally:
            con.close()

    def test_ev_old_ref_unique_ambiguous_unknown(self):
        con = _con()
        try:
            sv = con.execute("SELECT source_version_id FROM evidence WHERE"
                             " id='fixture-ad'").fetchone()[0]
            out = _customer.record(con, f"ev:fixture-ad@v{sv}")
            self.assertIn("Lumen", out["text"])
            # Distinct published association states cannot share a legacy version-only locator.
            _ops.set_brand_assocs(con, "fixture-ad",
                [{"brand": "Lumen", "basis": "confirmed", "channel": "spoken"}], 0)
            with self.assertRaises(_citations.RevisionUnavailable):
                _customer.record(con, f"ev:fixture-ad@v{sv}")
            with self.assertRaises(_citations.RevisionUnavailable):
                _customer.record(con, "ev:fixture-ad@v999999")
        finally:
            con.close()

    def test_sr1_record_matches_pinned_authority(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence", "fixture-ad")
            rec = _customer.record(con, ref)
            self.assertEqual(rec["canonical_source_ref"], ref)
            self.assertIn("Lumen", rec["text"])
            self.assertEqual(rec["original_bounds"], {"start_s": 10.0, "end_s": 20.0})
            self.assertEqual(rec["selected_bounds"], rec["effective_bounds"])
            self.assertIsNone(rec["window"])
            self.assertEqual(rec["relationship"], "current")
        finally:
            con.close()


class AliasSyntheticRootTest(unittest.TestCase):
    """AliasAudioTest behavior on a minimal owned synthetic importer
    root: no supplied corpus is read."""

    def _root(self, tmp: Path) -> Path:
        from earwitness.app import import_sources as _src
        data = tmp / "data"
        (tmp / "transcripts").mkdir(parents=True)
        shared = [{"start": 0.0, "end": 12.0, "text": "Lumen keeps us on"},
                  {"start": 12.0, "end": 24.0, "text": "the air tonight"}]
        other = [{"start": 0.0, "end": 12.0, "text": "A different show"},
                 {"start": 12.0, "end": 24.0, "text": "with other words"}]
        data.mkdir(parents=True)
        # Benchmark identity comes from the transcript files alone, as
        # in the real importer: only the supplied episode has metadata.
        (data / "episodes.json").write_text(json.dumps([
            {"ep_id": "s000e0", "show_title": "Synthetic Show",
             "ep_title": "Synthetic Ep", "pub_date": "2026-09-21",
             "audio_url": "https://audio.example/s000e0.mp3",
             "categories": ["News"]}]))
        (tmp / "transcripts" / "s000e0.json").write_text(
            json.dumps({"segments": shared}))
        (tmp / "transcripts" / "bench_0.json").write_text(
            json.dumps({"segments": shared}))
        (tmp / "transcripts" / "bench_1.json").write_text(
            json.dumps({"segments": other}))
        jev = tmp / "experiments" / "jev-ad-triage-20260921"
        jev.mkdir(parents=True)
        (jev / "results.json").write_text(json.dumps({"episodes": []}))
        det = tmp / "experiments" / "next-validation-detector" / "output"
        det.mkdir(parents=True)
        (det / "candidates_normalized.json").write_text("[]")
        self.assertTrue(_src.EPISODES_REL and _src.TRANSCRIPTS_DIR)
        return tmp

    def test_alias_inherits_target_media(self):
        import tempfile
        from earwitness.app import import_sources as _src
        with tempfile.TemporaryDirectory() as raw:
            plan = _src._build_seed_plan(Path(self._root(Path(raw))))
        by_id = {e["id"]: e for e in plan["episodes"]}
        self.assertEqual(by_id["bench_0"]["kind"], "supplied-alias")
        self.assertEqual(by_id["bench_0"]["alias_of"], "s000e0")
        self.assertEqual(by_id["bench_0"]["audio_url"],
                         "https://audio.example/s000e0.mp3")
        self.assertEqual(by_id["bench_1"]["kind"], "benchmark-unresolved")
        self.assertIsNone(by_id["bench_1"]["audio_url"])


class SparseViewTest(unittest.TestCase):
    def _share_ctx(self):
        from earwitness.app import access
        return access.AccessContext.create(principal_id="named-guest", principal_kind="guest",
            workspace_id="local", credential_kind="browser_session", authority_kind="share",
            share_grant_id="fixture-share", scopes={"research:read"},
            content_policy={"fields": ["metadata", "excerpt"]})

    def test_share_ctx_gets_identifier_only_projection(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "evidence",
                                                "fixture-ad")
            full = _citations.resolve_source_state(con, ref)
            self.assertIn("canonical_source_ref", full)
            sparse = _citations.resolve_source_state(
                con, ref, ctx=self._share_ctx())
            self.assertEqual(sparse["record_id"], "fixture-ad")
            self.assertEqual(sparse["actions"], ["read"])
            for key in ("canonical_source_ref", "pins", "text",
                        "relationship", "reason"):
                self.assertNotIn(key, sparse)
        finally:
            con.close()

    def test_partial_grant_member_keeps_text_only_with_transcripts(self):
        from earwitness.app import access as _access
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            partial = _access.AccessContext.create(
                principal_id="ed-1", principal_kind="editor",
                workspace_id="local", authority_kind="membership",
                scopes={"research:read"},
                credential_kind="reader_bearer")
            sparse = _citations.resolve_source_state(con, ref, ctx=partial)
            self.assertNotIn("text", sparse)
            self.assertEqual(sparse["record_id"], "e1")
        finally:
            con.close()


class RecordWindowTest(unittest.TestCase):
    def test_full_window_read_is_complete(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            out = _customer.record_window(con, ref)
            self.assertEqual(out["ref"], ref)
            self.assertTrue(out["canonical_source_ref"].startswith("sr1:"))
            self.assertEqual(out["bounds_basis"], "full_effective_source")
            window = out["window"]
            self.assertTrue(window["text_complete"])
            self.assertIsNone(window["truncation_reason"])
            self.assertIsNone(window["next_cursor"])
            self.assertEqual(len(window["segments"]), 3)
        finally:
            con.close()

    def test_late_window_selects_overlapping_text(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            out = _customer.record_window(con, ref, 10.0, 20.0)
            texts = [s["text"] for s in out["window"]["segments"]]
            self.assertEqual(texts, ["This episode is brought to you by Lumen"])
            self.assertEqual(out["selected_bounds"],
                             {"start_s": 10.0, "end_s": 20.0})
            self.assertEqual(out["bounds_basis"], "overlapping_segments")
            self.assertTrue(out["window"]["text_complete"])
        finally:
            con.close()

    def test_chunk_pagination_continues_to_completeness(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            first = _customer.record_window(con, ref, limit=1,
                                            workspace_context="ctx-1")
            self.assertFalse(first["window"]["text_complete"])
            self.assertEqual(first["window"]["truncation_reason"], "limit")
            cursor = first["window"]["next_cursor"]
            self.assertTrue(cursor)
            seen = [s["text"] for s in first["window"]["segments"]]
            while cursor is not None:
                page = _customer.record_window(con, ref, cursor=cursor,
                                               limit=1,
                                               workspace_context="ctx-1")
                seen.extend(s["text"] for s in page["window"]["segments"])
                cursor = page["window"]["next_cursor"]
            self.assertTrue(page["window"]["text_complete"])
            self.assertEqual(len(seen), 3)
        finally:
            con.close()

    def test_out_of_bounds_and_unpaired_rejected(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            with self.assertRaises(_customer._search.FilterRejected):
                _customer.record_window(con, ref, 1000.0, 2000.0)
            with self.assertRaises(_customer._search.FilterRejected):
                _customer.record_window(con, ref, 5.0, None)
            with self.assertRaises(_customer._search.FilterRejected):
                _customer.record_window(con, ref, 20.0, 10.0)
        finally:
            con.close()

    def test_changed_context_reopens_with_409(self):
        con = _con()
        try:
            ref = _citations.current_source_ref(con, "episode", "e1")
            first = _customer.record_window(con, ref, limit=1,
                                            workspace_context="ctx-1")
            cursor = first["window"]["next_cursor"]
            with self.assertRaises(_customer.WindowChanged):
                _customer.record_window(con, ref, cursor=cursor, limit=1,
                                        workspace_context="ctx-2")
        finally:
            con.close()


class ProducerAtomicityTest(unittest.TestCase):
    def test_trigger_abort_rolls_back_domain_and_publication(self):
        con = _con()
        try:
            before_states = con.execute(
                "SELECT COUNT(*) FROM source_states").fetchone()[0]
            before_excerpt = con.execute(
                "SELECT excerpt FROM evidence WHERE id='fixture-ad'"
            ).fetchone()[0]
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("UPDATE evidence SET excerpt='doomed edit'"
                            " WHERE id='fixture-ad'")
                _citations.publish_source_state(
                    con, kind="evidence", record_id="fixture-ad",
                    actor="producer")
                # A downstream trigger abort before commit.
                raise RuntimeError("simulated trigger abort")
            except RuntimeError:
                con.execute("ROLLBACK")
            after_states = con.execute(
                "SELECT COUNT(*) FROM source_states").fetchone()[0]
            after_excerpt = con.execute(
                "SELECT excerpt FROM evidence WHERE id='fixture-ad'"
            ).fetchone()[0]
            self.assertEqual(after_states, before_states)
            self.assertEqual(after_excerpt, before_excerpt)
        finally:
            con.close()

    def test_claimed_invite_row_durable_after_commit(self):
        con = _con()
        try:
            from earwitness.app import collaboration as _collab
            con.execute("INSERT INTO memberships (workspace_id, member, role,"
                        " created_at) VALUES ('local', 'owner', 'owner', ?)",
                        (_db.utcnow(),))
            con.execute("BEGIN IMMEDIATE")
            invite = _collab.create_invite(con, "local", "ed-1", "editor",
                                           "owner")
            out = _collab.accept_invite(con, invite["invite_id"], "ed-1")
            con.execute("COMMIT")
            row = con.execute("SELECT status, member FROM invites"
                              " WHERE id=?", (invite["invite_id"],)).fetchone()
            self.assertEqual(row["status"], "accepted")
            self.assertEqual(row["member"], "ed-1")
            self.assertEqual(out["role"], "editor")
            member = con.execute(
                "SELECT role FROM memberships WHERE workspace_id='local'"
                " AND member='ed-1'").fetchone()
            self.assertEqual(member["role"], "editor")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
