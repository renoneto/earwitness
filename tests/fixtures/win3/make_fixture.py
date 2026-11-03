"""Build the WIN3 old-report fixture at the pre-WIN3 code (base d8db4a28,
campaign-report/4.2, "Heard in" wording).

Writes report-4.2-pre-win3.json (the stored report_json text) and
report-4.2-pre-win3.html (the stored export payload bytes) beside this file
and prints both sha256 values. Same report as tests/fixtures/func2b: a
judge-confirmed named candidate in ep-1 and a fully reviewed attached read
in ep-2, one show window, brief code WIDGET10; at 4.2 it carries episode
verdicts. Time and ids are fixed so two runs give identical bytes.

Run from a d8db4a28 checkout: PYTHONPATH=<checkout> .venv/bin/python <this file>
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
        ev.add_candidate(h.con, "det:c", episode_id="ep-1", start=120.0, end=150.0,
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
        assert row["renderer_version"] == "campaign-report/4.2", row["renderer_version"]
        assert "Heard in 2 of 2 checked episodes" in row["payload"], "4.2 wording"
        return row["report_json"], row["payload"]
    finally:
        h.con.close()


def main() -> None:
    report_json, payload = build()
    (OUT / "report-4.2-pre-win3.json").write_bytes(report_json.encode("utf-8"))
    (OUT / "report-4.2-pre-win3.html").write_bytes(payload.encode("utf-8"))
    print("json", hashlib.sha256(report_json.encode("utf-8")).hexdigest())
    print("html", hashlib.sha256(payload.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
