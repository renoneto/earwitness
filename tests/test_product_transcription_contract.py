"""P03 transcription-contract regressions (stdlib unittest).

Every test fails on a plausible bug: missing profile fields, silent
device fallback, unrecorded profile switches, unqualified winner
claims, forced-English transcription, or ad-deciding VAD.
"""
from __future__ import annotations

import unittest

from earwitness.pipeline import transcription as t


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


def _sheet(**over):
    base = {
        "source_hashes": ["sha256:media1"],
        "source_categories": ["host_read"],
        "reference_provenance": {"author": "owner",
                                 "inspected": True},
        "required_names": ["Acme"],
        "required_codes": ["ACME20"],
        "required_domains": ["example.com"],
        "normalization": "casefold; keep numbers/codes/domains",
        "error_limits": {"critical_detail": 0},
        "timestamp_tolerance_s": 1.0,
        "resource_caps": {"peak_rss_mb": 8192},
    }
    base.update(over)
    return base


class ProfileContractTest(unittest.TestCase):
    def test_profile_exposes_all_required_fields(self):
        prof = t.validate_profile(_profile())
        for field in t.REQUIRED_PROFILE_FIELDS:
            self.assertIn(field, prof)

    def test_missing_profile_field_rejected(self):
        bad = _profile()
        del bad["weights_digest"]
        with self.assertRaises(t.TranscriptError):
            t.validate_profile(bad)

    def test_unsupported_device_explicit_error_no_fallback(self):
        with self.assertRaises(t.UnsupportedDeviceError):
            t.validate_profile(_profile(device="tpu-9"))

    def test_no_english_forcing(self):
        with self.assertRaises(t.TranscriptError):
            t.validate_language_policy({"mode": "force_english"})
        self.assertEqual(t.validate_language_policy({"mode": "auto"})["mode"],
                         "auto")

    def test_vad_skips_silence_only(self):
        with self.assertRaises(t.TranscriptError):
            t.validate_vad({"skip_silence": True, "decide_ads": True})
        self.assertTrue(t.validate_vad({"skip_silence": True})["skip_silence"])


class ResidencyTest(unittest.TestCase):
    def test_one_load_per_worker_and_switch_recorded(self):
        reg = t.ModelResidency()
        first = reg.ensure_loaded(_profile(), load_fn=lambda p: None,
                                  now_s=1.5)
        self.assertFalse(first["reused"])
        second = reg.ensure_loaded(_profile(), load_fn=lambda p: None,
                                   now_s=0.0)
        self.assertTrue(second["reused"])  # same profile keeps loaded model
        switched = reg.ensure_loaded(
            _profile(model="medium"), load_fn=lambda p: None, now_s=2.0)
        self.assertFalse(switched["reused"])
        kinds = [e["event"] for e in reg.load_events]
        self.assertEqual(kinds, ["load", "unload", "load"])
        self.assertEqual(reg.load_events[0]["load_time_s"], 1.5)
        self.assertEqual(reg.load_events[2]["load_time_s"], 2.0)


class QualificationTest(unittest.TestCase):
    def test_sheet_freeze_requires_full_contract(self):
        with self.assertRaises(t.TranscriptError):
            t.freeze_qualification_sheet({"source_hashes": ["x"]})
        frozen = t.freeze_qualification_sheet(_sheet())
        self.assertTrue(frozen["frozen"])

    def test_comparison_refuses_without_approved_media(self):
        sheet = t.freeze_qualification_sheet(_sheet())
        out = t.run_comparison(sheet, [], {}, approved_media=None,
                               selected_profile_id="cpu-small")
        self.assertEqual(out["result"], "blocked")

    def test_comparison_requires_explicit_profile_selection(self):
        sheet = t.freeze_qualification_sheet(_sheet())
        with self.assertRaises(t.TranscriptError):
            t.run_comparison(sheet, [], {}, approved_media={"hashes": ["h"]},
                             selected_profile_id=None)

    def test_capless_sheet_cannot_qualify_huge_footprint(self):
        # F4: fail closed -- a sheet without resource caps cannot yield
        # qualified, no matter how small the measured footprint looks.
        sheet = _sheet()
        del sheet["resource_caps"]
        sheet["frozen"] = True
        profiles = [{"profile_id": "cpu-small"}]
        out = t.run_comparison(
            sheet, profiles,
            {"cpu-small": {"wall_time_s": 10.0, "critical_detail_errors": 0,
                           "timestamp_errors_s": [0.1],
                           "peak_rss_mb": 1e12}},
            approved_media={"hashes": ["h"],
                            "reference_provenance": {"author": "owner",
                                                     "inspected": True}},
            selected_profile_id="cpu-small")
        self.assertEqual(out["result"], "throughput-only")
        self.assertNotIn("selected_profile_id", out,
                         msg="throughput-only must not name a quality winner")

    def test_throughput_only_without_reference_no_invented_winner(self):
        sheet = t.freeze_qualification_sheet(_sheet())
        profiles = [{"profile_id": "cpu-small"}]
        out = t.run_comparison(
            sheet, profiles, {"cpu-small": {"wall_time_s": 10.0}},
            approved_media={"hashes": ["h"]},
            selected_profile_id="cpu-small")
        self.assertEqual(out["result"], "throughput-only")
        self.assertNotIn("selected_profile_id", out,
                         msg="throughput-only must not name a quality winner")


if __name__ == "__main__":
    unittest.main()
