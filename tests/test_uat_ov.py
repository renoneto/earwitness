"""UAT 2026-10-15 fix round, UAT-009 (ov), backend half.

Overview carries `alert_lifecycle_counts`: how many of the workspace's
alerts are active, paused and stopped (the Alerts page lists all of them).
`alert_totals` only names alerts with changes, so without this count an
Overview with quiet alerts could not tell they exist and showed the
first-use card. Hosted and local share customer.overview.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import unittest
from pathlib import Path

from earwitness.app import customer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backend_trackers import query_acme, seed_corpus  # noqa: E402
from test_customer_campaigns import fresh_db  # noqa: E402  (file workspace, as the local route needs)


def _alerts(con, lifecycles):
    ids = []
    for i, life in enumerate(lifecycles):
        aid = customer.save_alert(con, f"quiet {i}", query_acme())["id"]
        if life != "active_local":
            rev = con.execute("SELECT revision FROM customer_alerts WHERE id=?", (aid,)).fetchone()[0]
            customer.set_alert_lifecycle(con, aid, life, rev)
        ids.append(aid)
    con.commit()
    return ids


class OverviewAlertCountTest(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db()
        seed_corpus(self.con)

    def tearDown(self):
        self.con.close()

    def test_empty_workspace_counts_zero(self):
        out = customer.overview(self.con)
        self.assertEqual(out["alert_lifecycle_counts"], {"active_local": 0, "paused": 0, "stopped": 0})

    def test_quiet_alerts_are_counted_by_lifecycle(self):
        _alerts(self.con, ["active_local"] * 4 + ["paused", "stopped"])
        out = customer.overview(self.con)
        self.assertEqual(out["alert_totals"], [])
        self.assertEqual(out["alert_lifecycle_counts"], {"active_local": 4, "paused": 1, "stopped": 1})
        self.assertEqual(sum(out["alert_lifecycle_counts"].values()), len(customer.list_alerts(self.con)))


class LocalRouteTest(unittest.TestCase):
    PORT = 18799
    HOST = f"127.0.0.1:{PORT}"

    def setUp(self):
        from scripts import app_server as srv
        self.srv = srv
        self.con = fresh_db()
        seed_corpus(self.con)
        _alerts(self.con, ["active_local", "active_local"])
        self.ws = Path(self.con.execute("PRAGMA database_list").fetchone()[2]).parent
        self._prev = {k: srv._state.get(k) for k in ("workspace", "port", "csrf")}
        srv._state.update({"workspace": self.ws, "port": self.PORT, "csrf": "csrf-local"})

    def tearDown(self):
        self.srv._state.update(self._prev)
        self.con.close()

    def test_local_overview_carries_the_counts(self):
        handler = self.srv.Handler.__new__(self.srv.Handler)
        handler.command, handler.path = "GET", "/api/v1/customer/overview"
        handler.request_version = "HTTP/1.1"
        handler.requestline = "GET /api/v1/customer/overview HTTP/1.1"
        handler._headers_buffer = []
        handler.log_message = lambda *a: None
        handler.headers = {"Host": self.HOST, "Content-Length": "0"}
        handler.rfile, handler.wfile = io.BytesIO(b""), io.BytesIO()
        handler.close_connection = True
        handler.do_GET()
        head, _, rest = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertEqual(int(head.split(b" ")[1]), 200, rest)
        self.assertEqual(json.loads(rest.decode())["alert_lifecycle_counts"],
                         {"active_local": 2, "paused": 0, "stopped": 0})


class HostedRouteTest(unittest.TestCase):
    def setUp(self):
        from tests.test_backend_http import HostedHttpTest
        self._harness = HostedHttpTest
        HostedHttpTest.setUp(self)
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            self.before = customer.overview(con)["alert_lifecycle_counts"]
            _alerts(con, ["active_local", "paused"])
        finally:
            con.close()

    def tearDown(self):
        self._harness.tearDown(self)

    def test_hosted_overview_carries_the_counts(self):
        status, _h, data = self._harness._call(self, "/api/v1/customer/overview")
        self.assertEqual(status, 200, data)
        counts = data["alert_lifecycle_counts"]
        self.assertEqual(counts["active_local"], self.before["active_local"] + 1)
        self.assertEqual(counts["paused"], self.before["paused"] + 1)


if __name__ == "__main__":
    unittest.main()
