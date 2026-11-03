"""Browser filter keeps the pipeline source (fresh machine proposals)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from earwitness.app import ops


class PipelineFilterTest(unittest.TestCase):
    def test_pipeline_source_survives(self):
        filt = ops.normalize_filter({"source": ["pipeline"]})
        self.assertEqual(filt["source"], ["pipeline"])

    def test_all_known_sources_survive(self):
        filt = ops.normalize_filter({"source": ["jev", "detector", "manual", "pipeline"]})
        self.assertEqual(filt["source"], ["jev", "detector", "manual", "pipeline"])

    def test_unknown_source_still_dropped(self):
        filt = ops.normalize_filter({"source": ["pipeline", "bogus"]})
        self.assertEqual(filt["source"], ["pipeline"])


if __name__ == "__main__":
    unittest.main()
