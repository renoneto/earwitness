"""Build the FUNC-2b old-report fixture at the pre-FUNC-2b code (base 5e5f8edc).

Writes report-4.1-pre-func2b.json (the stored report_json text) and
report-4.1-pre-func2b.html (the stored export payload bytes) beside this
file and prints both sha256 values. The report carries a judge-confirmed
named candidate in ep-1 and a fully reviewed attached read in ep-2, both in
one show window with the brief code WIDGET10. Time is fixed so two runs give
identical bytes.

Run from the repo root: PYTHONPATH=. .venv/bin/python tests/fixtures/func2b/make_fixture.py
"""
from __future__ import annotations

import hashlib
import itertools
import uuid
from pathlib import Path
from unittest import mock

from earwitness.app import campaigns
from earwitness.app import db as _db
from tests import test_episode_verdicts as ev

FIXED = "2026-03-27T10:00:00+00:00"
OUT = Path(__file__).resolve().parent


class _Build(ev._Base):
    def runTest(self):  # pragma: no cover - never run as a test
        pass


def build() -> tuple[str, str]:
    # Every id and time that reaches the report is fixed: brief and placement
    # ids come from uuid4, and fixtures stamp db.utcnow().
    counter = itertools.count(1)
    with mock.patch.object(uuid, "uuid4", lambda: uuid.UUID(int=next(counter))), \
            mock.patch.object(_db, "utcnow", return_value=FIXED):
        return _build()


def _build() -> tuple[str, str]:
    h = _Build()
    h.setUp()
    try:
        h.episode("ep-1", "2026-03-10")
        h.episode("ep-2", "2026-03-12")
        pid = h.window(ev.BRIEF)
        add = ev.add_candidate
        add(h.con, "det:c", episode_id="ep-1", start=120.0, end=150.0,
            attrs=dict(ev.ACME, **ev.CODE_ATTRS))
        h.attach(pid, h.read("r", "ep-2", 120.0, 150.0, code="WIDGET10"))
        with mock.patch.object(campaigns, "_now_iso", return_value=FIXED):
            campaigns.save_episode_review(h.con, h.cid, pid, "ep-1", "det:c", "confirm", 0)
            draft = campaigns.build_customer_draft(h.con, h.cid)
            rev = h.con.execute("SELECT revision FROM campaigns WHERE id=?", (h.cid,)).fetchone()[0]
            frozen = campaigns.freeze_customer_report(h.con, h.cid, rev, draft["pins"])
        row = h.con.execute(
            "SELECT r.report_json, r.renderer_version, e.payload FROM campaign_reports r"
            " JOIN exports e ON e.id=r.export_id WHERE r.campaign_id=? AND r.report_no=?",
            (h.cid, frozen["report_no"])).fetchone()
        assert row["renderer_version"] == "campaign-report/4.1", row["renderer_version"]
        return row["report_json"], row["payload"]
    finally:
        h.con.close()


def main() -> None:
    report_json, payload = build()
    (OUT / "report-4.1-pre-func2b.json").write_bytes(report_json.encode("utf-8"))
    (OUT / "report-4.1-pre-func2b.html").write_bytes(payload.encode("utf-8"))
    print("json", hashlib.sha256(report_json.encode("utf-8")).hexdigest())
    print("html", hashlib.sha256(payload.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
