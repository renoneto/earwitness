"""DATE-FIX (plan review M9): Search's date bounds scope by calendar day.

validate_filter accepts any form datetime.date.fromisoformat takes
(basic 20260104, week 2026-W01-1, compact week 2026W011, 2026-W01), but
the episode and evidence SQL compare pub_date as text. A raw "20260104"
sorts after every "2026-..." date and a raw "2026-W01-1" after all of
2026, so published_to scoped every episode and published_from scoped
none. The validated filter now carries the normalized YYYY-MM-DD day.
"""
from __future__ import annotations

import sqlite3
import unittest

from earwitness.app import access, db, search, services

# 2026-W01-1 (ISO week 1, Monday) is 2025-12-29.
DATES = {"ep-1229": "2025-12-29", "ep-0103": "2026-01-03", "ep-0104": "2026-01-04",
         "ep-0105": "2026-01-05", "ep-0301": "2026-03-01"}
WEEK1 = ("2026-W01-1", "2026W011", "2026-W01")


def member_ctx():
    return access.AccessContext.create(
        principal_id="owner-1", principal_kind="owner", workspace_id="local",
        credential_kind="local_operator", scopes={"research:read", "transcripts:read"})


def seeded_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    db.init_schema(con)
    for ep, day in DATES.items():
        con.execute("INSERT INTO episodes (id, kind, show_title, pub_date, fetch_date,"
                    " updated_at) VALUES (?, 'supplied', 'Show', ?, '2026-03-02',"
                    " '2026-03-02T00:00:00+00:00')", (ep, day))
        con.execute("INSERT INTO evidence (id, source, episode_id, excerpt, created_at)"
                    " VALUES (?, 'manual', ?, ?, '2026-03-02T00:00:00+00:00')",
                    (f"ev-{ep}", ep, f"mention in {ep}"))
    con.commit()
    return con


def upto(day):
    return sorted(ep for ep, d in DATES.items() if d <= day)


def since(day):
    return sorted(ep for ep, d in DATES.items() if d >= day)


class DateFormScopeTest(unittest.TestCase):
    def setUp(self):
        self.con = seeded_db()

    def tearDown(self):
        self.con.close()

    def transcript_scope(self, raw):
        episodes, _pin = search.transcript_episodes(self.con, search.validate_filter(raw))
        return sorted(e["id"] for e in episodes)

    def evidence_scope(self, raw):
        page = services.search_evidence(member_ctx(), self.con, {"query": "", **raw},
                                        limit=50)
        return sorted(i["episode_id"] for i in page["items"])

    def cases(self):
        yield {"published_to": "20260104"}, upto("2026-01-04")
        yield {"published_from": "20260104"}, since("2026-01-04")
        for week in WEEK1:
            yield {"published_to": week}, upto("2025-12-29")
            yield {"published_from": week}, since("2025-12-29")

    def test_transcript_scope_uses_calendar_day(self):
        for raw, want in self.cases():
            with self.subTest(**raw):
                self.assertEqual(self.transcript_scope(raw), want)

    def test_evidence_scope_uses_calendar_day(self):
        for raw, want in self.cases():
            with self.subTest(**raw):
                self.assertEqual(self.evidence_scope(raw), want)

    def test_yyyy_mm_dd_scope_unchanged(self):
        raw = {"published_from": "2026-01-03", "published_to": "2026-01-05"}
        want = ["ep-0103", "ep-0104", "ep-0105"]
        self.assertEqual(self.transcript_scope(raw), want)
        self.assertEqual(self.evidence_scope(raw), want)


class DateFormValidateTest(unittest.TestCase):
    def test_to_before_from_rejected_in_any_form(self):
        for to in ("20260109", "2026-01-09"):
            with self.subTest(published_to=to), self.assertRaises(search.FilterRejected):
                search.validate_filter({"published_from": "2026-01-10", "published_to": to})
        with self.assertRaises(search.FilterRejected):
            search.validate_filter({"published_from": "2026-01-01", "published_to": "2026-W01-1"})

    def test_filter_echoes_normalized_day(self):
        for raw, day in (("20260104", "2026-01-04"), ("2026-W01-1", "2025-12-29"),
                         ("2026W011", "2025-12-29"), ("2026-W01", "2025-12-29"),
                         ("2026-01-04", "2026-01-04")):
            with self.subTest(raw=raw):
                filt = search.validate_filter({"published_from": raw, "published_to": raw})
                self.assertEqual((filt["published_from"], filt["published_to"]), (day, day))


if __name__ == "__main__":
    unittest.main()
