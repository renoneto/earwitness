"""S2 judgment-first review workspace contract (stdlib unittest).

Static contract on app/operator/views.js + app/operator/app.js: the Review workspace exposes a
visible primary path (queue context, excerpt, Judgment, Save + Save-and-next,
keyboard hint), keeps every other section collapsed but reachable, shows the
same source/record legend on Library and Review, and preserves all field IDs
and save/queue/transport wiring.
"""
from __future__ import annotations

import html
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VIEWS = REPO / "app" / "operator" / "views.js"
APP = REPO / "app" / "operator" / "app.js"

SECTION_ORDER = [
    "Listen",
    "Transcript",
    "Bounds correction",
    "Attribution channels",
    "Notes",
    "Provenance",
]

LEGEND_LINES = [
    "detector",
    "jev",
    "manual",
    "pipeline",
    "pipe:obs",
    "candidate",
    "observation",
]

PRESERVED_IDS = [
    "queue-position", "review-form", "review-judgment", "review-save-next",
    "review-keys-hint", "review-excerpt", "save-next", "clear-judgment",
    "review-start", "review-end", "bounds-verified", "review-notes",
    "audio", "play-evidence", "seek-back", "seek-fwd", "audio-rate",
    "loop-interval", "expand-context", "segments", "selection-status",
    "manual-form", "manual-start", "manual-end", "manual-origin",
    "assoc-form", "association-rows", "association-count", "add-association",
    "review-prev", "review-next",
    "split-form", "merge-form", "split-preview", "merge-preview",
    "split-merge-preview",
]

PRESERVED_BEHAVIOR = [
    "wireReviewKeys", "setJudgmentDraft", "saveReview", "App.generation",
    "412", "Draft retained", "End of queue. No further result in this queue.",
    "Effective bounds", "Effective transcript version",
    "Refined transcript alternative", "Source provenance",
]


class ReviewLayoutContractTest(unittest.TestCase):
    def test_primary_path_elements_present(self):
        src = VIEWS.read_text()
        for token in ("review-primary", "review-excerpt", "evidence.excerpt",
                      "review-judgment", "review-save-next",
                      "Save and next", "Save review", "queue-position",
                      "review-keys-hint", "Ctrl"):
            self.assertIn(token, src)

    def test_advanced_sections_collapsed_in_order(self):
        src = VIEWS.read_text()
        scope = src[src.index("const sourceLegend"):
                    src.index("Views.explore")]
        summaries = [html.unescape(m) for m in
                     re.findall(r"<summary>(.*?)</summary>", scope)]
        positions = []
        for title in SECTION_ORDER:
            hits = [i for i, s in enumerate(summaries) if title in s]
            self.assertTrue(hits, f"missing collapsed section: {title}")
            positions.append(hits[0])
        self.assertEqual(positions, sorted(positions),
                         f"sections out of order: {summaries}")
        tags = re.findall(r"<details[^>]*>", scope)
        open_tags = [t for t in tags if "open" in t.split(">")[0]]
        self.assertEqual(len(open_tags), 2,
                         f"Listen and Transcript open, nothing else: {open_tags}")
        opened = [m.group(1) for m in re.finditer(r"<details[^>]*\bopen\b[^>]*><summary>(.*?)</summary>", scope)]
        self.assertEqual(len(opened), 2, opened)
        self.assertIn("Listen", opened[0], "the first open section must be Listen & audio")
        self.assertIn("Transcript", opened[1], "the second open section must be Transcript")

    def test_legend_shared_by_library_and_review(self):
        src = VIEWS.read_text()
        self.assertGreaterEqual(src.count("sourceLegend()"), 2)
        for token in ("Source and record legend", *LEGEND_LINES):
            self.assertIn(token, src)

    def test_fields_and_wiring_preserved(self):
        src = VIEWS.read_text()
        for token in (*PRESERVED_IDS, *PRESERVED_BEHAVIOR):
            self.assertIn(token, src)
        self.assertIn('setAttribute("form", "review-form")', src)

    def test_form_owner_support_in_app(self):
        src = APP.read_text()
        self.assertIn("form.elements", src)
        self.assertIn("el.form", src)


if __name__ == "__main__":
    unittest.main()
