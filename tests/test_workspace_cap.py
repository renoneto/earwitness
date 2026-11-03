"""The workspace in-flight cap is a setting (LIMITER-WAIT fallback, DEP7b).

EARWITNESS_WORKSPACE_MAX_IN_FLIGHT sets how many requests may run at once
in one workspace: unset or empty is 10; a whole number of ASCII digits
from 1 to 64 is used as given; anything else refuses to start in every
mode (health.check_config), and the error names the setting, not the
value. The per-session cap (2) and the rate buckets are unchanged. This
branch adds no waiting: a full cap still answers 429 at once.
"""
from __future__ import annotations

import asyncio
import time
import unittest

from earwitness.app import asgi as _asgi
from earwitness.app import health as _health

ENV = "EARWITNESS_WORKSPACE_MAX_IN_FLIGHT"


def problems_for(value, mode="local-test"):
    config = _asgi.config_from_env({"EARWITNESS_MODE": mode, ENV: value})
    return [p for p in _health.check_config(config) if ENV in p]


class WorkspaceCapSettingTest(unittest.TestCase):
    def test_the_setting_name_and_default(self):
        self.assertEqual(_asgi.WORKSPACE_MAX_IN_FLIGHT_ENV, ENV)
        self.assertEqual(_asgi.config_from_env({}).workspace_max_in_flight, 10)
        self.assertEqual(_asgi.config_from_env({ENV: ""}).workspace_max_in_flight, 10)
        self.assertEqual(_asgi.HostedConfig().workspace_max_in_flight, 10)

    def test_the_app_limiter_uses_the_setting(self):
        for value, cap in (("", 10), ("16", 16), (" 16 ", 16), ("1", 1), ("64", 64)):
            with self.subTest(value=value):
                app = _asgi.HostedApp(_asgi.config_from_env({ENV: value}))
                self.addCleanup(app.close)
                self.assertEqual(app.limiter._max_ws, cap)
                self.assertEqual(app.limiter._max_grant, 2)
                self.assertEqual(problems_for(value), [])

    def test_cap_sixteen_admits_sixteen_and_refuses_the_seventeenth(self):
        app = _asgi.HostedApp(_asgi.config_from_env({ENV: "16"}))
        self.addCleanup(app.close)

        async def go():
            for n in range(16):
                self.assertEqual(await app.limiter.admit(
                    f"g{n}", "acme", time.time()), 0.0)
            self.assertGreater(await app.limiter.admit("g99", "acme", time.time()), 0)
        asyncio.run(go())

    def test_a_bad_value_refuses_to_start_in_both_modes(self):
        bad = ("0", "-3", "x", "65", "1.5", "16.0", "99999999999999999999",
               "1_6", "+16", "١٦", "１６", "1 6")
        for value in bad:
            for mode in ("local-test", "production"):
                with self.subTest(value=value, mode=mode):
                    found = problems_for(value, mode)
                    self.assertEqual(len(found), 1, found)
                    self.assertNotIn(value.strip(), found[0].replace(ENV, ""))

    def test_rate_buckets_and_session_cap_are_unchanged(self):
        self.assertEqual((_asgi.READS_PER_MINUTE, _asgi.BURST,
                          _asgi.SESSION_BURST, _asgi.WORKSPACE_READS_PER_MINUTE,
                          _asgi.MAX_CONCURRENT_PER_GRANT,
                          _asgi.MAX_CONCURRENT_PER_WORKSPACE),
                         (60, 20, 40, 600, 2, 10))


if __name__ == "__main__":
    unittest.main()
