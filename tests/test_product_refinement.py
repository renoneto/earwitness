"""P02 refinement/selection/effective-transcript regressions (stdlib unittest).

Every test fails on a plausible bug: double-mapped crop offsets,
refinement failure hiding the fast transcript, spoken attribution from
URL evidence, paid inference on read paths, or moved review pins.
Disposable fixtures only; no owner data touched.
"""
from __future__ import annotations

import unittest

from earwitness.app import access, contracts
from earwitness.pipeline import detection as det
from earwitness.pipeline import transcription as t


def _ctx(**kwargs):
    args = {"principal_id": "owner-1", "principal_kind": "owner",
            "workspace_id": "local", "credential_kind": "local_operator",
            "scopes": {"research:read", "transcripts:read"}}
    args.update(kwargs)
    return access.AccessContext.create(**args)


def _profile(**over):
    base = {
        "backend": "faster-whisper", "backend_version": "1.2.1",
        "model": "small", "weights_digest": "sha256:abc123",
        "device": "cpu", "precision": "int8",
        "batch_size": 1, "beam_size": 5,
        "vad": {"skip_silence": True},
        "language_policy": {"mode": "auto"},
        "word_timestamp_mode": "segment",
        "thread_limit": 4, "memory_limit_mb": 2048,
    }
    base.update(over)
    return base


def _segs():
    return [
        {"start": 0.0, "end": 10.0, "text": "welcome to the show"},
        {"start": 10.0, "end": 20.0, "text": "brought to you by Acme today"},
        {"start": 20.0, "end": 30.0, "text": "visit acme dot com for deals"},
    ]


def _full():
    return t.build_full_transcript(
        "ep-1", "media-1", "digest-1", _segs(), _profile(),
        language=None, fetch_date="2026-09-22",
        fetch_date_source="test")


class OffsetMappingTest(unittest.TestCase):
    def test_nonzero_crop_maps_once_not_twice(self):
        # RED-CAPABLE: crop at 120s, backend-internal 5.0s must map to
        # 125.0s exactly once. A double-mapping bug yields 245.0s.
        crop_start = 120.0
        self.assertEqual(t.map_crop_to_original(crop_start, 5.0), 125.0)
        ref = t.build_refinement_transcript(
            "ep-1", "full:ep-1:x", "media-1", "digest-1",
            crop_start, 180.0,
            [{"start": 5.0, "end": 9.0, "text": "ad segment"}],
            _profile())
        self.assertEqual(ref["segments"][0]["start"], 125.0)
        self.assertEqual(ref["segments"][0]["end"], 129.0)
        self.assertEqual(ref["crop_start_s"], 120.0)
        self.assertEqual(ref["backend_timestamps"][0]["start"], 5.0)

    def test_refinement_failure_leaves_searchable_fast_transcript(self):
        full = _full()
        out = t.apply_refinement_failure(full, "backend offline")
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["searchable_version_id"], full["version_id"])
        self.assertEqual(out["segments"], full["segments"])
        self.assertIn("backend offline", out["reason"])


class AttributionTest(unittest.TestCase):
    def test_url_only_evidence_spoken_unknown(self):
        segs = [{"start": 0.0, "end": 8.0, "text": "welcome back"},
                {"start": 8.0, "end": 16.0,
                 "text": "go to example dot com for 20 percent off"}]
        cands = det.deterministic_candidates("ep-9", segs)
        url_cands = [c for c in cands if c["anchor"] == "cta_url"]
        self.assertTrue(url_cands)
        for c in url_cands:
            self.assertIsNone(c["brand_spoken"])
            self.assertEqual(c["brand"], "(unnamed)")
            self.assertTrue(c["urls"])
            self.assertIsNotNone(c["url_association"])

    def test_below_threshold_never_triggers_paid_inference_on_read(self):
        with self.assertRaises(contracts.ContractError):
            t.refuse_paid_on_read("search_evidence", True)
        with self.assertRaises(contracts.ContractError):
            t.refuse_paid_on_read("transcript_window", True)
        # Non-read paths do not raise from the read guard itself.
        t.refuse_paid_on_read("submit_processing", True)


class SelectionTest(unittest.TestCase):
    def test_union_clips_and_joins_overlaps(self):
        cands = [{"candidate_id": "a", "start_s": 100.0, "end_s": 110.0},
                 {"candidate_id": "b", "start_s": 120.0, "end_s": 130.0}]
        spans = t.join_overlapping(
            t.union_spans(cands, 15.0, media_duration_s=3600.0))
        # 85-125 and 105-145 overlap after ±15s context → one compute span.
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["start"], 85.0)
        self.assertEqual(spans[0]["end"], 145.0)
        self.assertEqual(sorted(spans[0]["member_ids"]), ["a", "b"])

    def test_union_clipped_to_media_duration(self):
        cands = [{"candidate_id": "a", "start_s": 3590.0, "end_s": 3595.0}]
        spans = t.union_spans(cands, 15.0, media_duration_s=3600.0)
        self.assertEqual(spans[0]["end"], 3600.0)

    def test_high_coverage_selects_single_full_pass(self):
        cands = [{"candidate_id": "a", "start_s": 0.0, "end_s": 3000.0}]
        decision = t.selection_decision(cands, 3600.0)
        self.assertEqual(decision["mode"], "full_pass")
        self.assertEqual(len(decision["compute_spans"]), 1)
        self.assertIn("0.70", decision["basis"].replace("0.7", "0.70")
                      if "0.70" not in decision["basis"]
                      else decision["basis"])

    def test_below_threshold_sampling_needs_approved_run(self):
        with self.assertRaises(contracts.ContractError):
            t.sample_below_threshold([{"start_s": 1.0}], None)
        out = t.sample_below_threshold(
            [{"start_s": 1.0}],
            {"approved": True, "run_id": "run-1",
             "sampling_basis": "diagnostic music-heavy spot check"})
        self.assertEqual(out["basis"],
                         "diagnostic music-heavy spot check")


class EffectiveVersionTest(unittest.TestCase):
    def test_default_read_uses_pinned_full(self):
        full = _full()
        out = t.select_effective_version(full, [], {"start_s": 1.0,
                                                    "end_s": 2.0})
        self.assertEqual(out["effective_version_id"], full["version_id"])

    def test_explicit_refinement_only_when_fully_inside(self):
        full = _full()
        ref = t.build_refinement_transcript(
            "ep-1", full["version_id"], "media-1", "digest-1",
            100.0, 200.0, [{"start": 0.0, "end": 10.0, "text": "x"}],
            _profile())
        ok = t.select_effective_version(
            full, [ref], {"start_s": 110.0, "end_s": 115.0},
            requested_version_id=ref["version_id"])
        self.assertEqual(ok["effective_version_id"], ref["version_id"])
        edge = t.select_effective_version(
            full, [ref], {"start_s": 95.0, "end_s": 105.0},
            requested_version_id=ref["version_id"])
        self.assertEqual(edge["effective_version_id"], full["version_id"])
        self.assertIn(ref["version_id"], edge["alternatives"])

    def test_version_change_creates_revision_without_moving_pins(self):
        pins = {"review": "pin-1", "citation": "cite-1"}
        rev = t.new_revision_on_version_change(pins, "full:ep", "ref:ep:1-2")
        self.assertNotEqual(rev["revision_id"], "")
        self.assertEqual(rev["review_pins"], pins)
        self.assertEqual(rev["citation_pins"], pins)


class JevOperationTest(unittest.TestCase):
    def test_single_whole_transcript_request_with_strict_validation(self):
        segs = [{"start": float(i * 10), "end": float(i * 10 + 10),
                 "text": f"segment {i} content here"}
                for i in range(15)]  # 150s → 3 windows in one request
        req = det.build_jev_request("ep-1", segs)
        self.assertEqual(len(req["windows"]), 3)
        self.assertEqual(req["model"], det.PINNED_JEV_MODEL)
        calls = []

        def fake_jev(request):
            calls.append(request)
            return {"model": det.PINNED_JEV_MODEL,
                    "answers": {w["window_id"]:
                                {"noul": 0.9 if w["window_id"] == "window_000"
                                 else 0.1,
                                 "start_s": w["start_s"], "end_s": w["end_s"]}
                                for w in request["windows"]}}

        ctx = _ctx()
        budget = access.ExecutionBudget(deadline_s=30.0)
        out = det.run_jev_screening("ep-1", segs, ctx, budget, fake_jev,
                                    {"provider": "test"})
        self.assertEqual(len(calls), 1)  # whole transcript, one call
        self.assertEqual(out["jev_windows"]["queue"]["window_ids"],
                         ["window_000"])
        self.assertIn("not a calibrated", out["jev_windows"]["queue"]["basis"])
        # Raw scores persisted, detector evidence separate.
        self.assertEqual(len(out["jev_windows"]["record"]["raw_scores"]), 3)
        self.assertIn("deterministic_candidates", out)

    def test_window_mismatch_rejected_not_queued(self):
        segs = [{"start": 0.0, "end": 10.0, "text": "hello world"}]
        req = det.build_jev_request("ep-1", segs)
        bad = {"model": det.PINNED_JEV_MODEL,
               "answers": {"window_999": {"noul": 0.99, "start_s": 0.0,
                                          "end_s": 10.0}}}
        with self.assertRaises(det.WindowValidationError):
            det.validate_jev_response(req, bad)

    def test_jev_read_path_refuses_paid_inference(self):
        segs = [{"start": 0.0, "end": 10.0, "text": "hello world"}]
        ctx = _ctx()
        budget = access.ExecutionBudget(deadline_s=30.0)
        with self.assertRaises(contracts.ContractError):
            det.run_jev_screening("ep-1", segs, ctx, budget, lambda r: {},
                                  read_path="search_evidence")


if __name__ == "__main__":
    unittest.main()
