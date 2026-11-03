"""P3 rerun follow-up: the signed-in session reply names which pilot
features are on, so the browser can hide what is off (the Connections
nav item while agent access is off). Real HostedApp HTTP surface."""
from __future__ import annotations

import dataclasses
import time
import unittest

from earwitness.app import browser_auth as _auth
from earwitness.app import rights_admission as _rights
from tests import test_backend_authority as _authority_tests


class SessionFeaturesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = _authority_tests.HostedReaderHttpTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.headers = self.fixture._csrf()

    def _session(self, features):
        app = self.fixture.app
        app.config = dataclasses.replace(app.config, pilot_features=features)
        status, session = self.fixture._call("/api/v1/session", method="GET", headers=self.headers)
        self.assertEqual(status, 200, session)
        return session

    def test_features_off_are_false(self):
        self.assertEqual(self._session(())["features"],
                         {"invites": False, "sharing": False, "agent_access": False,
                          "ask": False, "meaning_alerts": False, "brief_check": False,
                          "brief_check_live": False})

    def test_only_listed_features_are_true(self):
        self.assertEqual(self._session(("invites",))["features"],
                         {"invites": True, "sharing": False, "agent_access": False,
                          "ask": False, "meaning_alerts": False, "brief_check": False,
                          "brief_check_live": False})

    def test_unset_outside_production_means_every_feature(self):
        # Except ask: an unset list never turns it on (T24).
        self.assertEqual(self._session(None)["features"],
                         {name: name not in ("ask", "meaning_alerts", "brief_check", "brief_check_live")
                          for name in _rights.FEATURES})

    def test_features_are_booleans_by_known_name_only(self):
        # An unknown name never reaches the browser; nothing else is added.
        features = self._session(("agent_access", "billing"))["features"]
        self.assertEqual(set(features), set(_rights.FEATURES))
        self.assertTrue(all(isinstance(v, bool) for v in features.values()))
        self.assertTrue(features["agent_access"])

    def test_workspace_switch_reply_carries_the_same_features(self):
        session = self._session(())
        status, switched = self.fixture._call("/api/v1/session/workspace",
            body={"workspace_id": "acme",
                  "expected_workspace_switch_revision": session["workspace_switch_revision"]},
            headers=self.headers)
        self.assertEqual(status, 200, switched)
        self.assertEqual(switched["features"], session["features"])

    def test_signed_out_reply_has_no_features(self):
        app = self.fixture.app
        app.config = dataclasses.replace(app.config, pilot_features=())
        status, out = self.fixture._call("/api/v1/session", method="GET", headers={})
        self.assertNotIn("features", out)

    def test_no_member_session_still_names_features(self):
        control = self.fixture.app.store.auth_connection()
        try:
            now = time.time()
            control.execute(
                "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
                "email_verified,csrf_token,active_workspace,created_at,expires_at)"
                " VALUES (?,'https://i.example','nobody','',0,'tok123','',?,?)",
                (_auth._hash_presented("sess-nobody"), now, now + 3600))
            control.commit()
        finally:
            control.close()
        app = self.fixture.app
        app.config = dataclasses.replace(app.config, pilot_features=())
        status, out = self.fixture._call("/api/v1/session", method="GET",
                                         headers={**self.headers, "cookie": "ew_session=sess-nobody"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["workspaces"], [])
        self.assertFalse(out["features"]["agent_access"])


if __name__ == "__main__":
    unittest.main()
