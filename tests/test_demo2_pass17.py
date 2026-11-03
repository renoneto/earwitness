"""DEMO2 pass 17 (code review r13, DEMO2-R13-02): the "workspace role
changed" refusal carries a stable marker.

Owner decision: status 403, code FORBIDDEN and the message stay unchanged;
the error body gains "reason": "role_changed", the same pattern as
"access": "lost". It is not lost access, so it never carries that marker.
The customer app keys on the marker, never on the message text; the last
test ties the server's marker to the client's comparison.

The HTTP case runs the real HostedApp over the disposable
HostedReaderHttpTest fixture. The role changes inside the request, right
before the authority check, as when an owner changes this member's role
while the request runs.
"""
from __future__ import annotations

import ast
import json
import pathlib
import unittest
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi
from earwitness.app import collaboration as _collab
from earwitness.app import services as _services
from tests import test_backend_authority as _authority_tests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNER = "oidc:https://i.example:owner"


def _body(exc):
    app = _asgi.HostedApp.__new__(_asgi.HostedApp)
    status, raw, _headers = app._service_error(exc)
    return status, json.loads(raw)


class RoleChangedMarkerTest(unittest.TestCase):
    def test_role_changed_keeps_403_forbidden_and_the_message_and_adds_the_marker(self):
        self.assertTrue(issubclass(_access.RoleChanged, _access.AuthorizationError))
        self.assertFalse(issubclass(_access.RoleChanged, _access.AccessLost))
        status, body = _body(_access.RoleChanged("workspace role changed"))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "FORBIDDEN")
        self.assertEqual(body["error"]["status"], 403)
        self.assertEqual(body["error"]["message"], "workspace role changed")
        self.assertEqual(body["error"]["reason"], "role_changed")
        self.assertNotIn("access", body["error"])

    def test_other_refusals_have_no_reason(self):
        for exc in (_access.AuthorizationError("owner manages membership", 403),
                    _access.AccessLost("workspace membership required")):
            with self.subTest(message=str(exc)):
                _status, body = _body(exc)
                self.assertNotIn("reason", body["error"])

    def test_the_services_raise_site_uses_role_changed(self):
        tree = ast.parse((ROOT / "earwitness" / "app" / "services.py").read_text())
        sites = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == "workspace role changed":
                func = node.func
                sites.append(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
        self.assertEqual(sites, ["RoleChanged"])


class ClientServerTieTest(unittest.TestCase):
    def test_the_client_compares_the_marker_the_server_sends(self):
        _status, body = _body(_access.RoleChanged("workspace role changed"))
        marker = body["error"]["reason"]
        client = (ROOT / "app" / "app.js").read_text()
        self.assertTrue(f"error.reason === '{marker}'" in client,
                        "the client does not compare the server's marker")
        self.assertFalse("'workspace role changed'" in client,
                         "the client still matches the server's message text")


class RoleChangedHttpTest(unittest.TestCase):
    def setUp(self):
        self.fx = _authority_tests.HostedReaderHttpTest()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.headers = self.fx._csrf()

    def test_control_the_owner_reads(self):
        status, out = self.fx._call("/api/v1/customer/brands", method="GET", headers=self.headers)
        self.assertEqual(status, 200, out)

    def test_a_role_change_during_the_request_is_marked(self):
        # Admission reads the owner role; the authority check inside the
        # request then finds editor.
        real_check = _services._workspace_authority
        real_role = _collab.get_role
        state = {"demoted": False}

        def demoting(con, ctx, entry, **kwargs):
            state["demoted"] = True
            return real_check(con, ctx, entry, **kwargs)

        def role(con, namespace, principal):
            return "editor" if state["demoted"] and principal == OWNER else real_role(con, namespace, principal)

        with mock.patch.object(_services, "_workspace_authority", side_effect=demoting), \
                mock.patch.object(_collab, "get_role", side_effect=role):
            status, out = self.fx._call("/api/v1/customer/brands", method="GET", headers=self.headers)
        self.assertTrue(state["demoted"], "fixture: the authority check ran")
        self.assertEqual(status, 403, out)
        self.assertEqual(out["error"]["code"], "FORBIDDEN")
        self.assertEqual(out["error"]["message"], "workspace role changed")
        self.assertEqual(out["error"].get("reason"), "role_changed", out)
        self.assertNotIn("access", out["error"])


if __name__ == "__main__":
    unittest.main()
