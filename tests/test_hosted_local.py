"""Hosted local tests: Tasks 10-14 connected features without live providers.

Registry routing, membership/binding double-check, OIDC session/CSRF
mechanics, Stripe webhook verification + entitlement reconciliation, and
Resend/sandbox provider selection. No network, no live provider actions.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi
from earwitness.app import billing as _billing
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from earwitness.app import db as _db
from earwitness.app import monitoring as _mon
from earwitness.app import services as _services


def _workspace_db(path, identity="ws-identity-1"):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _db.init_schema(con)
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('workspace_id', ?)",
                (identity,))
    con.commit()
    return con


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, "ws.sqlite3")
        con = _workspace_db(self.db_path)
        con.close()
        self.store = _conn.GrantsStore(os.path.join(self.tmp.name, "grants.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_resolve_round_trip(self):
        entry = _conn.register_workspace(
            self.store, "acme", self.db_path, self.data_dir,
            row_namespace="local", is_owner=False, operator="owner")
        self.assertEqual(entry["db_identity"], "ws-identity-1")
        resolved = _conn.resolve_workspace(self.store, "acme")
        self.assertEqual(resolved["db_identity"], "ws-identity-1")
        self.assertEqual(resolved["row_namespace"], "local")

    def test_register_missing_file_rejected(self):
        with self.assertRaises(_conn.GrantError):
            _conn.register_workspace(
                self.store, "ghost", os.path.join(self.data_dir, "nope.sqlite3"),
                self.data_dir)

    def test_register_outside_data_dir_rejected(self):
        outside = os.path.join(self.tmp.name, "outside.sqlite3")
        con = _workspace_db(outside)
        con.close()
        with self.assertRaises(_conn.GrantError):
            _conn.register_workspace(self.store, "out", outside, self.data_dir)

    def test_register_canonical_absolute_outside_rejected(self):
        """RED-CAPABLE correction: an already-canonical absolute DB path
        outside data_dir was accepted by register and opened. Before:
        registered + opened. After: GrantError on both paths."""
        outside = os.path.realpath(os.path.join(self.tmp.name, "canon.sqlite3"))
        con = _workspace_db(outside)
        con.close()
        self.assertEqual(outside, os.path.realpath(outside))
        with self.assertRaises(_conn.GrantError):
            _conn.register_workspace(self.store, "canon", outside,
                                     os.path.realpath(self.data_dir))

    def test_open_stored_outside_path_rejected(self):
        outside = os.path.realpath(os.path.join(self.tmp.name, "moved.sqlite3"))
        con = _workspace_db(outside)
        con.close()
        _conn.register_workspace(self.store, "acme", self.db_path, self.data_dir)
        raw = self.store._connect()
        try:
            raw.execute("UPDATE workspace_registry SET db_path=? WHERE workspace_id=?",
                        (outside, "acme"))
            raw.commit()
        finally:
            raw.close()
        with self.assertRaises(_conn.GrantError):
            _conn.open_registered_db(self.store, "acme", self.data_dir)

    def test_symlink_escape_rejected(self):
        outside = os.path.realpath(os.path.join(self.tmp.name, "linked.sqlite3"))
        con = _workspace_db(outside)
        con.close()
        link = os.path.join(self.data_dir, "link.sqlite3")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(_conn.GrantError):
            _conn.register_workspace(self.store, "link", link, self.data_dir)

    def test_sibling_prefix_not_contained(self):
        sibling = os.path.realpath(self.data_dir) + "-evil"
        os.makedirs(sibling, exist_ok=True)
        sib_db = os.path.join(sibling, "ws.sqlite3")
        con = _workspace_db(sib_db)
        con.close()
        with self.assertRaises(_conn.GrantError):
            _conn.register_workspace(self.store, "sib", sib_db, self.data_dir)

    def test_resolve_unknown_rejected_without_path(self):
        with self.assertRaises(_conn.GrantError) as ctx:
            _conn.resolve_workspace(self.store, "nope")
        self.assertNotIn("sqlite3", str(ctx.exception))

    def test_open_and_identity_tamper_rejected(self):
        _conn.register_workspace(self.store, "acme", self.db_path, self.data_dir)
        con, _entry = _conn.open_registered_db(self.store, "acme", self.data_dir)
        con.close()
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE meta SET value='tampered' WHERE key='workspace_id'")
        con.commit()
        con.close()
        with self.assertRaises(_conn.GrantError):
            _conn.open_registered_db(self.store, "acme", self.data_dir)

    def test_owner_binding_gates_hosted_access(self):
        _conn.register_workspace(self.store, "own", self.db_path, self.data_dir,
                                 is_owner=True)
        ctx = _access.AccessContext.create(
            principal_id="oidc:https://issuer.example:u1", principal_kind="owner",
            workspace_id="own", authority_kind="membership",
            credential_kind="browser_session")
        with self.assertRaises(_access.AuthorizationError):
            _services.open_customer_workspace(self.store, "own", self.data_dir, ctx)
        _conn.bind_workspace_account(self.store, "own", "https://issuer.example",
                                     "u1", operator="owner")
        con, _entry = _services.open_customer_workspace(
            self.store, "own", self.data_dir, ctx)
        con.close()
        wrong = _access.AccessContext.create(
            principal_id="oidc:https://issuer.example:u2", principal_kind="owner",
            workspace_id="own", authority_kind="membership",
            credential_kind="browser_session")
        with self.assertRaises(_access.AuthorizationError):
            _services.open_customer_workspace(self.store, "own", self.data_dir, wrong)


def _load_app_workspace():
    import sys
    from pathlib import Path
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    sys.path.insert(0, scripts)
    try:
        import app_workspace
        return app_workspace
    finally:
        sys.path.remove(scripts)


class OperatorBindWorkflowTest(unittest.TestCase):
    """Req 4: explicit trusted local-operator register+bind workflow.

    OIDC login alone never claims an owner workspace: the exact
    principal a login would mint is denied hosted access until the
    operator registers the existing database and binds that exact
    (issuer, subject); a different subject stays denied after bind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, "ws.sqlite3")
        con = _workspace_db(self.db_path)
        con.close()
        self.grants_db = os.path.join(self.tmp.name, "grants.sqlite3")
        self.cli = _load_app_workspace()
        self.store = _conn.GrantsStore(self.grants_db)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, principal):
        return _access.AccessContext.create(
            principal_id=principal, principal_kind="owner",
            workspace_id="own", authority_kind="membership",
            credential_kind="browser_session")

    def test_register_bind_workflow_gates_owner_access(self):
        rc = self.cli.main([
            "register-owner", "--grants-db", self.grants_db,
            "--data-dir", self.data_dir, "--workspace-id", "own",
            "--db-path", self.db_path, "--operator", "owner"])
        self.assertEqual(rc, 0)
        # The principal an OIDC login would mint is denied before bind.
        with self.assertRaises(_access.AuthorizationError):
            _services.open_customer_workspace(
                self.store, "own", self.data_dir,
                self._ctx("oidc:https://issuer.example:u1"))
        rc = self.cli.main([
            "bind-account", "--grants-db", self.grants_db,
            "--workspace-id", "own",
            "--issuer", "https://issuer.example", "--subject", "u1",
            "--operator", "owner"])
        self.assertEqual(rc, 0)
        con, _entry = _services.open_customer_workspace(
            self.store, "own", self.data_dir,
            self._ctx("oidc:https://issuer.example:u1"))
        con.close()
        with self.assertRaises(_access.AuthorizationError):
            _services.open_customer_workspace(
                self.store, "own", self.data_dir,
                self._ctx("oidc:https://issuer.example:u2"))

    def test_bind_unknown_workspace_fails_without_access(self):
        rc = self.cli.main([
            "bind-account", "--grants-db", self.grants_db,
            "--workspace-id", "missing",
            "--issuer", "https://issuer.example", "--subject", "u1"])
        self.assertEqual(rc, 1)


class MembershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data_dir)
        self.db_path = os.path.join(self.data_dir, "ws.sqlite3")
        con = _workspace_db(self.db_path)
        _collab.create_workspace(con, "local", "Acme", "oidc:https://i.example/a")
        inv = _collab.create_invite(con, "local", "oidc:https://i.example/b",
                                    "editor", "oidc:https://i.example/a")
        _collab.accept_invite(con, inv["invite_id"], "oidc:https://i.example/b")
        con.commit()
        con.close()
        self.store = _conn.GrantsStore(os.path.join(self.tmp.name, "grants.sqlite3"))
        _conn.register_workspace(self.store, "acme", self.db_path, self.data_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, principal):
        return _access.AccessContext.create(
            principal_id=principal, principal_kind="editor",
            workspace_id="acme", authority_kind="membership",
            credential_kind="browser_session")

    def test_member_opens(self):
        con, _entry = _services.open_customer_workspace(
            self.store, "acme", self.data_dir,
            self._ctx("oidc:https://i.example/b"))
        con.close()

    def test_non_member_denied(self):
        with self.assertRaises(_access.AuthorizationError):
            _services.open_customer_workspace(
                self.store, "acme", self.data_dir,
                self._ctx("oidc:https://i.example/stranger"))


def _auth_config():
    return _auth.BrowserAuthConfig(
        issuer="https://issuer.example", client_id="cid",
        audience="aud", redirect_uri="http://localhost:8771/session/callback",
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        jwks_uri="https://issuer.example/.well-known/jwks.json",
        allow_insecure_loopback=True)


class BrowserAuthTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row

    def tearDown(self):
        self.con.close()

    def test_unconfigured_login_refused(self):
        cfg = _auth.BrowserAuthConfig(
            issuer="", client_id="", audience="", redirect_uri="http://x/")
        with self.assertRaises(ValueError):
            _auth.start_login(self.con, cfg)

    def test_login_state_pkce_and_return_route(self):
        out = _auth.start_login(self.con, _auth_config(), return_to="/customer")
        self.assertIn("code_challenge_method=S256", out["authorize_url"])
        self.assertIn(out["state"], out["authorize_url"])
        with self.assertRaises(ValueError):
            _auth.start_login(self.con, _auth_config(),
                              return_to="https://evil.example/")

    def test_allowed_return_routes(self):
        self.assertTrue(_auth.allowed_return("/customer/campaigns"))
        self.assertFalse(_auth.allowed_return("https://evil.example/"))
        self.assertFalse(_auth.allowed_return("//evil.example/"))

    def test_csrf_token_and_origin(self):
        session = {"csrf_token": "tok123"}
        self.assertTrue(_auth.validate_csrf(session, "tok123"))
        self.assertFalse(_auth.validate_csrf(session, "wrong"))
        self.assertFalse(_auth.validate_csrf(session, "tok123",
                                             origin="https://evil.example",
                                             host="localhost:8771"))
        self.assertTrue(_auth.validate_csrf(session, "tok123",
                                            origin="http://localhost:8771/x",
                                            host="localhost:8771"))

    def test_session_expiry_and_revocation(self):
        _auth.ensure_auth_schema(self.con)
        presented = "sess-1"
        digest = hashlib.sha256(f"ew-browser-v1:{presented}".encode()).hexdigest()
        now = time.time()
        self.con.execute(
            "INSERT INTO browser_sessions (session_hash, issuer, subject, email,"
            " email_verified, csrf_token, active_workspace, created_at,"
            " expires_at, revoked_at) VALUES (?, 'https://i.example', 'u1', '',"
            " 0, 'c', '', ?, ?, NULL)", (digest, now, now + 3600))
        self.con.commit()
        self.assertIsNotNone(_auth.lookup_session(self.con, presented, now=now + 10))
        self.assertIsNone(_auth.lookup_session(self.con, presented, now=now + 7200))
        self.con.execute("UPDATE browser_sessions SET revoked_at=? WHERE session_hash=?",
                         (now + 20, digest))
        self.con.commit()
        self.assertIsNone(_auth.lookup_session(self.con, presented, now=now + 30))
        self.assertEqual(_auth.session_principal(
            {"issuer": "https://i.example", "subject": "u1"}),
            "oidc:https://i.example:u1")


class BrowserBindingTest(unittest.TestCase):
    """OIDC transactions are bound to the initiating browser (Req 1).

    Local signed-protocol fixture: an in-test RSA key signs ID tokens,
    JWKS/token HTTP is stubbed, no provider traffic, no real account."""

    def setUp(self):
        from joserfc.jwk import RSAKey
        from joserfc import jwt
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.cfg = _auth_config()
        key = RSAKey.generate_key(
            2048, parameters={"kid": "fixture-k1", "use": "sig"})
        pub = key.as_dict(private=False)
        pub["alg"] = "RS256"
        self.key = key
        self.jwks = {"keys": [pub]}
        self._jwt = jwt
        self.token_calls = []
        self.token_override = None
        self._real_fetch = _auth._fetch_json
        self._real_post = _auth._post_form
        fixture = self

        def fake_fetch(url, *, timeout=10.0):
            self.assertEqual(url, self.cfg.jwks_uri)
            return fixture.jwks

        def fake_post(url, fields, *, timeout=10.0):
            fixture.token_calls.append(dict(fields))
            if fixture.token_override is not None:
                return {"id_token": fixture.token_override,
                        "access_token": "at-fixture",
                        "token_type": "Bearer"}
            row = fixture.con.execute(
                "SELECT nonce FROM oidc_login_txns WHERE used=1"
                " ORDER BY created_at DESC LIMIT 1").fetchone()
            nonce = row["nonce"] if row else "wrong-nonce"
            return {"id_token": fixture.mint(nonce),
                    "access_token": "at-fixture",
                    "token_type": "Bearer"}

        _auth._fetch_json = fake_fetch
        _auth._post_form = fake_post

    def tearDown(self):
        _auth._fetch_json = self._real_fetch
        _auth._post_form = self._real_post
        self.con.close()

    def mint(self, nonce, sub="user-a", iss=None, aud=None):
        now = int(time.time())
        token = self._jwt.encode(
            {"alg": "RS256", "kid": "fixture-k1"},
            {"iss": iss or self.cfg.issuer, "aud": aud or self.cfg.client_id,
             "sub": sub, "nonce": nonce, "iat": now, "exp": now + 300},
            self.key)
        return token.decode("ascii") if isinstance(token, bytes) else token

    def session_count(self):
        return self.con.execute(
            "SELECT COUNT(*) AS n FROM browser_sessions").fetchone()["n"]

    def test_same_browser_flow_succeeds(self):
        started = _auth.start_login(self.con, self.cfg)
        self.con.commit()
        self.assertIn("browser_proof", started)
        self.assertNotIn(started["browser_proof"],
                         started["authorize_url"])
        finished = _auth.finish_login(
            self.con, self.cfg,
            {"state": started["state"], "code": "code-1"},
            browser_proof=started["browser_proof"])
        self.con.commit()
        self.assertEqual(finished["subject"], "user-a")
        live = _auth.lookup_session(self.con, finished["session_id"])
        self.assertIsNotNone(live)
        self.assertEqual(self.token_calls[0]["grant_type"],
                         "authorization_code")
        self.assertIn("code_verifier", self.token_calls[0])

    def test_cross_browser_callback_fails_without_session(self):
        # Browser B holds a live session of its own.
        b_started = _auth.start_login(self.con, self.cfg)
        b_finished = _auth.finish_login(
            self.con, self.cfg,
            {"state": b_started["state"], "code": "code-b"},
            browser_proof=b_started["browser_proof"])
        self.con.commit()
        # Browser A starts a transaction; its callback URL leaks to B.
        a_started = _auth.start_login(self.con, self.cfg)
        self.con.commit()
        leaked = {"state": a_started["state"], "code": "code-a"}
        before = self.session_count()
        for proof in ("", "wrong-proof"):
            with self.assertRaises(ValueError):
                _auth.finish_login(self.con, self.cfg, leaked,
                                   browser_proof=proof)
            self.con.commit()
        # B's session is untouched and no session was created for A.
        self.assertIsNotNone(
            _auth.lookup_session(self.con, b_finished["session_id"]))
        self.assertEqual(self.session_count(), before)

    def test_missing_proof_refused_before_token_exchange(self):
        started = _auth.start_login(self.con, self.cfg)
        self.con.commit()
        calls_before = len(self.token_calls)
        with self.assertRaises(ValueError):
            _auth.finish_login(self.con, self.cfg,
                               {"state": started["state"], "code": "code-x"})
        self.assertEqual(len(self.token_calls), calls_before)
        self.assertEqual(self.session_count(), 0)

    def test_replay_after_success_fails(self):
        started = _auth.start_login(self.con, self.cfg)
        self.con.commit()
        params = {"state": started["state"], "code": "code-1"}
        _auth.finish_login(self.con, self.cfg, params,
                           browser_proof=started["browser_proof"])
        self.con.commit()
        with self.assertRaises(ValueError):
            _auth.finish_login(self.con, self.cfg, params,
                               browser_proof=started["browser_proof"])

    def test_unknown_state_fails_without_session(self):
        with self.assertRaises(ValueError):
            _auth.finish_login(self.con, self.cfg,
                               {"state": "no-such-state", "code": "code-x"},
                               browser_proof="anything")
        self.assertEqual(self.session_count(), 0)

    def test_bad_token_claims_create_no_session(self):
        b_started = _auth.start_login(self.con, self.cfg)
        b_finished = _auth.finish_login(
            self.con, self.cfg,
            {"state": b_started["state"], "code": "code-b"},
            browser_proof=b_started["browser_proof"])
        self.con.commit()
        cases = [
            ("nonce", self.mint("wrong-nonce")),
            ("issuer", self.mint("unused-nonce",
                                 iss="https://evil.example")),
            ("audience", self.mint("unused-nonce",
                                   aud="other-client")),
        ]
        for label, bad_token in cases:
            started = _auth.start_login(self.con, self.cfg)
            self.con.commit()
            self.token_override = bad_token
            try:
                with self.assertRaises(ValueError, msg=label):
                    _auth.finish_login(
                        self.con, self.cfg,
                        {"state": started["state"], "code": "code-" + label},
                        browser_proof=started["browser_proof"])
                self.con.commit()
            finally:
                self.token_override = None
        # B's session is unreplaced and no session was minted for anyone.
        self.assertIsNotNone(
            _auth.lookup_session(self.con, b_finished["session_id"]))
        self.assertEqual(self.session_count(), 1)

    def test_tx_cookie_scoping(self):
        cookie = _auth.tx_cookie_value(self.cfg, "secret-1",
                                       host="localhost:8771")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        strict = _auth.tx_cookie_value(self.cfg, "secret-1",
                                       host="app.example:443")
        self.assertIn("Secure", strict)
        self.assertEqual(
            _auth.clear_tx_cookie(),
            "ew_tx=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")


def _billing_config(**over):
    base = {"secret_key": "sk_test_x", "webhook_secret": "whsec_x",
            "success_url": "/billing/success", "cancel_url": "/billing/cancel",
            "prices": {"price_pro": "Pro"}}
    base.update(over)
    return _billing.BillingConfig(**base)


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        _billing.ensure_billing_schema(self.con)

    def tearDown(self):
        self.con.close()

    def test_unconfigured_checkout_refused(self):
        with self.assertRaises(ValueError):
            _billing.create_checkout_session(
                _billing.BillingConfig(), price_id="price_pro",
                workspace_id="acme")

    def test_unknown_price_rejected_from_catalog(self):
        with self.assertRaises(ValueError):
            _billing.create_checkout_session(
                _billing_config(), price_id="price_unknown",
                workspace_id="acme")

    def test_checkout_sets_subscription_workspace_metadata(self):
        """Producer leg (Req 2): the Checkout request must stamp
        subscription_data metadata, the leg the reconciler trusts.
        Before: subscription_data metadata was None while Session
        metadata carried the workspace, so first purchases failed
        closed. Session metadata/client reference stay supplemental."""
        captured = {}
        real = _billing._stripe_request
        _billing._stripe_request = lambda config, method, path, fields: (
            captured.update(fields) or
            {"id": "cs_test", "url": "https://checkout.invalid/s",
             "livemode": True})
        try:
            _billing.create_checkout_session(
                _billing_config(), price_id="price_pro",
                workspace_id="workspace-acme")
        finally:
            _billing._stripe_request = real
        self.assertEqual(
            captured.get("subscription_data[metadata][workspace_id]"),
            "workspace-acme")
        self.assertEqual(captured.get("metadata[workspace_id]"),
                         "workspace-acme")
        self.assertEqual(captured.get("client_reference_id"),
                         "workspace-acme")

    def test_webhook_signature_verified_and_rejected(self):
        cfg = _billing_config()
        payload = b'{"id":"evt_1"}'
        ts = int(time.time())
        signed = f"{ts}.".encode() + payload
        good = hmac.new(b"whsec_x", signed, hashlib.sha256).hexdigest()
        _billing.verify_webhook_signature(cfg, payload, f"t={ts},v1={good}")
        with self.assertRaises(ValueError):
            _billing.verify_webhook_signature(cfg, payload, f"t={ts},v1=deadbeef")
        with self.assertRaises(ValueError):
            _billing.verify_webhook_signature(cfg, payload, "t=1,v1=deadbeef")
        with self.assertRaises(ValueError):
            _billing.verify_webhook_signature(_billing.BillingConfig(), payload,
                                              f"t={ts},v1={good}")

    def test_entitlement_unknown_fails_closed(self):
        out = _billing.entitlement_status(self.con, "acme")
        self.assertFalse(out["allowed"])
        self.assertEqual(out["state"], "unknown")

    def test_entitlement_active_and_grace(self):
        _billing.ensure_billing_schema(self.con)
        now = int(time.time())
        self.con.execute(
            "INSERT INTO billing_entitlement (workspace_id, status, price_id,"
            " current_period_end, past_due_since, updated_at)"
            " VALUES ('acme', 'active', 'price_pro', ?, 0, '2026-09-28T00:00:00Z')",
            (now + 3600,))
        self.con.commit()
        out = _billing.entitlement_status(self.con, "acme", now=now)
        self.assertTrue(out["allowed"])
        self.con.execute("UPDATE billing_entitlement SET status='past_due',"
                         " past_due_since=? WHERE workspace_id='acme'", (now,))
        self.con.commit()
        cfg = _billing_config()
        out = _billing.entitlement_status(self.con, "acme", cfg, now=now + 60)
        self.assertTrue(out["allowed"])
        out = _billing.entitlement_status(
            self.con, "acme", cfg, now=now + cfg.past_due_grace_s + 10)
        self.assertFalse(out["allowed"])
        self.assertEqual(out["state"], "blocked")


class BillingReconciliationTest(unittest.TestCase):
    """RED-CAPABLE correction: signed events are reconciliation triggers,
    not entitlement truth. Before: a valid HMAC webhook with
    customer=cus_other for workspace-acme stored cus_other and returned
    allowed trialing. After: binding + current provider resource decide;
    duplicates, reorderings, mismatches, foreign resources, and failed
    reconciliations are refused through the local protocol fixture."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.cfg = _billing_config()
        self.now = int(time.time())

    def tearDown(self):
        self.con.close()

    # BY1 (purchase design 8.6): no trial is sold and `trialing` is refused,
    # so the default verified resource is `active`.
    def _resource(self, sub="sub_1", customer="cus_acme", status="active",
                  price="price_pro", workspace="acme", period=None, **over):
        res = {"id": sub, "customer": customer, "status": status,
               "price_id": price,
               "current_period_end": period if period is not None
               else self.now + 99999,
               "trial_end": 0, "cancel_at_period_end": False,
               "metadata_workspace": workspace}
        res.update(over)
        return res

    def _signed(self, event):
        payload = json.dumps(event).encode()
        ts = int(time.time())
        signed = f"{ts}.".encode() + payload
        sig = hmac.new(b"whsec_x", signed, hashlib.sha256).hexdigest()
        return payload, f"t={ts},v1={sig}"

    def _event(self, eid, etype, sub="sub_1", customer="cus_acme", **obj):
        body = {"id": sub, "customer": customer, "subscription": sub}
        body.update(obj)
        return {"id": eid, "type": etype, "livemode": True,
                "data": {"object": body}}

    def _apply(self, event, provider):
        payload, sig = self._signed(event)
        out = _billing.handle_webhook(self.cfg, self.con, "acme", payload,
                                      sig, provider=provider)
        self.con.commit()
        return out

    def test_foreign_customer_refused_without_binding(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_foreign": self._resource(
                sub="sub_foreign", customer="cus_other",
                workspace="different-workspace")})
        out = self._apply(self._event("evt_foreign",
                                      "customer.subscription.created",
                                      sub="sub_foreign",
                                      customer="cus_other"), fix)
        self.assertEqual(out["status"], "unbound")
        ent = _billing.entitlement_status(self.con, "acme", self.cfg)
        self.assertFalse(ent["allowed"])
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM billing_entitlement").fetchone()[0],
            0)

    def test_verified_first_purchase_binds_and_allows(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource()})
        out = self._apply(self._event("evt_1", "checkout.session.completed"), fix)
        self.assertEqual(out["status"], "reconciled")
        self.assertEqual(_billing.bound_customer(self.con, "acme"), "cus_acme")
        ent = _billing.entitlement_status(self.con, "acme", self.cfg,
                                          now=self.now)
        self.assertTrue(ent["allowed"])
        self.assertEqual(ent["state"], "active")
        self.assertEqual(fix.calls, ["sub_1"])

    def test_checkout_metadata_reconciles_first_purchase(self):
        """Consumer leg (Req 2): a subscription carrying the workspace
        metadata stamped at checkout reconciles a first purchase; a
        subscription with no workspace metadata (the pre-fix producer
        gap) stays unbound."""
        captured = {}
        real = _billing._stripe_request
        _billing._stripe_request = lambda config, method, path, fields: (
            captured.update(fields) or
            {"id": "cs_test", "url": "https://checkout.invalid/s",
             "livemode": True})
        try:
            _billing.create_checkout_session(
                _billing_config(), price_id="price_pro",
                workspace_id="acme")
        finally:
            _billing._stripe_request = real
        stamped = captured.get("subscription_data[metadata][workspace_id]")
        self.assertEqual(stamped, "acme")
        # Bare subscription first: no workspace metadata (the pre-fix
        # producer gap) refuses first binding on the unbound workspace.
        bare = _billing.FixtureSubscriptionProtocol(resources={
            "sub_9": self._resource(sub="sub_9", workspace=None)})
        out = self._apply(self._event("evt_9", "checkout.session.completed",
                                      sub="sub_9"), bare)
        self.assertEqual(out["status"], "unbound")
        self.assertFalse(_billing.bound_customer(self.con, "acme"))
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(workspace=stamped)})
        out = self._apply(self._event("evt_1", "checkout.session.completed"),
                          fix)
        self.assertEqual(out["status"], "reconciled")
        self.assertTrue(_billing.entitlement_status(
            self.con, "acme", self.cfg, now=self.now)["allowed"])

    def test_mismatched_customer_refused_after_binding(self):
        _billing.bind_billing_customer(self.con, "acme", "cus_acme",
                                       operator="owner")
        self.con.commit()
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_x": self._resource(sub="sub_x", customer="cus_other",
                                    workspace="acme")})
        out = self._apply(self._event("evt_x", "customer.subscription.created",
                                      sub="sub_x", customer="cus_other"), fix)
        self.assertEqual(out["status"], "customer_mismatch")
        self.assertFalse(_billing.entitlement_status(
            self.con, "acme", self.cfg)["allowed"])

    def test_event_lies_about_resource_customer(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(customer="cus_real", workspace="acme")})
        out = self._apply(self._event("evt_1", "customer.subscription.created",
                                      customer="cus_claim"), fix)
        self.assertEqual(out["status"], "customer_mismatch")
        self.assertFalse(_billing.entitlement_status(
            self.con, "acme", self.cfg)["allowed"])

    def test_duplicate_returns_stored_outcome(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource()})
        first = self._apply(self._event("evt_1", "checkout.session.completed"),
                            fix)
        self.assertEqual(first["status"], "reconciled")
        second = self._apply(self._event("evt_1", "checkout.session.completed"),
                             fix)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["status"], "reconciled")
        self.assertEqual(fix.calls, ["sub_1"])

    def test_reordered_events_converge_on_verified_state(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(status="canceled",
                                    current_period_end=self.now + 100)})
        late = self._apply(self._event("evt_cancel",
                                       "customer.subscription.deleted"), fix)
        self.assertEqual(late["status"], "reconciled")
        early = self._apply(self._event("evt_create",
                                        "customer.subscription.created"), fix)
        self.assertEqual(early["status"], "reconciled")
        # Both orders converge on the verified canceled state. Since BY1
        # (purchase design 8.6) `canceled` is refused at once, whatever the
        # period end, so access is blocked both now and later.
        ent = _billing.entitlement_status(self.con, "acme", self.cfg,
                                          now=self.now)
        self.assertFalse(ent["allowed"])
        self.assertEqual(ent["state"], "blocked")
        self.assertEqual(ent["reason"], "subscription canceled")
        late = _billing.entitlement_status(self.con, "acme", self.cfg,
                                           now=self.now + 200)
        self.assertFalse(late["allowed"])
        self.assertEqual(late["state"], "blocked")

    def test_event_period_end_cannot_extend(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(status="active",
                                    current_period_end=self.now + 100)})
        self._apply(self._event("evt_1", "checkout.session.completed"), fix)
        # Since BY1 invoices name their subscription in the pinned API
        # version's place (billing.INVOICE_SUBSCRIPTION_PATH).
        evil = {"id": "evt_evil", "type": "invoice.paid", "livemode": True,
                "data": {"object": {"id": "in_1", "customer": "cus_acme",
                                    "parent": {"subscription_details": {
                                        "subscription": "sub_1"}},
                                    "current_period_end": self.now + 99999999}}}
        out = self._apply(evil, fix)
        self.assertEqual(out["status"], "reconciled")
        ent = _billing.entitlement_status(self.con, "acme", self.cfg,
                                          now=self.now)
        self.assertEqual(ent["current_period_end"], self.now + 100)

    def test_failed_reconciliation_retains_state(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(status="active")})
        self._apply(self._event("evt_1", "checkout.session.completed"), fix)
        before = _billing.entitlement_status(self.con, "acme", self.cfg,
                                             now=self.now)
        self.assertTrue(before["allowed"])
        broken = _billing.FixtureSubscriptionProtocol(failures={
            "sub_1": _billing.ProviderError("timeout")})
        # Since BY1 invoices name their subscription in the pinned API
        # version's place (billing.INVOICE_SUBSCRIPTION_PATH).
        out = self._apply(self._event(
            "evt_2", "invoice.payment_failed", subscription=None,
            parent={"subscription_details": {"subscription": "sub_1"}}),
            broken)
        self.assertEqual(out["status"], "reconcile_failed")
        after = _billing.entitlement_status(self.con, "acme", self.cfg,
                                            now=self.now)
        self.assertEqual(after, before)

    def test_gone_unknown_subscription_is_foreign(self):
        fix = _billing.FixtureSubscriptionProtocol(failures={
            "sub_ghost": _billing.ProviderNotFound("gone")})
        _billing.bind_billing_customer(self.con, "acme", "cus_acme",
                                       operator="owner")
        self.con.commit()
        out = self._apply(self._event("evt_g", "customer.subscription.deleted",
                                      sub="sub_ghost"), fix)
        self.assertEqual(out["status"], "foreign_resource")
        self.assertEqual(
            self.con.execute("SELECT COUNT(*) FROM billing_entitlement").fetchone()[0],
            0)

    def test_gone_verified_subscription_cancels_at_verified_end(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(status="active",
                                    current_period_end=self.now + 500)})
        self._apply(self._event("evt_1", "checkout.session.completed"), fix)
        gone = _billing.FixtureSubscriptionProtocol(failures={
            "sub_1": _billing.ProviderNotFound("deleted")})
        out = self._apply(self._event("evt_2", "customer.subscription.deleted"),
                          gone)
        self.assertEqual(out["status"], "reconciled")
        # A verified binding whose subscription is gone counts as canceled,
        # and since BY1 (purchase design 8.6) canceled is refused at once
        # rather than held to the old period end.
        ent = _billing.entitlement_status(self.con, "acme", self.cfg,
                                          now=self.now)
        self.assertFalse(ent["allowed"])
        self.assertEqual(ent["state"], "blocked")
        self.assertEqual(ent["reason"], "subscription canceled")
        stored = self.con.execute(
            "SELECT status, current_period_end FROM billing_entitlement"
            " WHERE workspace_id='acme'").fetchone()
        self.assertEqual(stored["status"], "canceled")
        self.assertEqual(stored["current_period_end"], self.now + 500)
        late = _billing.entitlement_status(self.con, "acme", self.cfg,
                                           now=self.now + 600)
        self.assertFalse(late["allowed"])

    def test_unpriced_verified_resource_refused(self):
        fix = _billing.FixtureSubscriptionProtocol(resources={
            "sub_1": self._resource(price="price_unknown")})
        out = self._apply(self._event("evt_1", "checkout.session.completed"),
                          fix)
        self.assertEqual(out["status"], "unpriced")
        self.assertFalse(_billing.entitlement_status(
            self.con, "acme", self.cfg)["allowed"])

    def test_unscripted_fixture_never_succeeds(self):
        fix = _billing.FixtureSubscriptionProtocol()
        out = self._apply(self._event("evt_1", "checkout.session.completed"),
                          fix)
        self.assertEqual(out["status"], "reconcile_failed")
        self.assertFalse(_billing.entitlement_status(
            self.con, "acme", self.cfg)["allowed"])


class DeliveryProviderTest(unittest.TestCase):
    def test_explicit_resend_selected(self):
        provider = _mon.select_delivery_provider(
            {"resend_api_key": "re_x", "resend_from": "ops@example.com"})
        self.assertIsInstance(provider, _mon.ResendDeliveryProvider)

    def test_sandbox_only_when_explicitly_allowed(self):
        with self.assertRaises(ValueError):
            _mon.select_delivery_provider({})
        provider = _mon.select_delivery_provider({}, allow_sandbox=True)
        self.assertIsInstance(provider, _mon.SandboxDeliveryProvider)

    def test_resend_requires_key_and_sender(self):
        with self.assertRaises(ValueError):
            _mon.ResendDeliveryProvider("", "ops@example.com")


class CsrfOriginTest(unittest.TestCase):
    """RED-CAPABLE correction: a matching token never excuses a
    mismatching origin. Before: Origin http://app.example.com:9999
    with Host app.example.com:443 returned True. After: scheme, host,
    and effective port must all match."""

    def test_coordinator_bypass_rejected(self):
        session = {"csrf_token": "tok123"}
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com:9999",
            host="app.example.com:443"))
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com:9999",
            host="app.example.com:443", scheme="http"))

    def test_same_origin_accepted(self):
        session = {"csrf_token": "tok123"}
        self.assertTrue(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com:9999/x",
            host="app.example.com:9999"))
        self.assertTrue(_auth.validate_csrf(
            session, "tok123", origin="https://app.example.com",
            host="app.example.com", scheme="https"))
        self.assertTrue(_auth.validate_csrf(
            session, "tok123", origin="https://app.example.com:443/y",
            host="app.example.com", scheme="https"))
        self.assertTrue(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com",
            host="app.example.com:80"))
        self.assertTrue(_auth.validate_csrf(
            session, "tok123", origin="HTTP://LOCALHOST:8771/",
            host="localhost:8771"))

    def test_cross_scheme_and_cross_host_rejected(self):
        session = {"csrf_token": "tok123"}
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com/x",
            host="app.example.com", scheme="https"))
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="http://evil.example/",
            host="app.example.com"))
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="http://app.example.com/x",
            host="app.example.com:8771"))
        self.assertFalse(_auth.validate_csrf(
            session, "tok123", origin="not a url",
            host="app.example.com"))

    def test_missing_origin_keeps_token_contract(self):
        session = {"csrf_token": "tok123"}
        self.assertTrue(_auth.validate_csrf(session, "tok123"))
        self.assertFalse(_auth.validate_csrf(session, "wrong"))


class AsgiSessionCsrfTest(unittest.TestCase):
    """RED-CAPABLE correction: POST /api/v1/session/workspace mutates
    the browser session outside _with_workspace and previously skipped
    CSRF. After: missing token, wrong token, and mismatching origin
    all 403; a valid same-origin token performs the switch. Logout
    revokes only with a valid token."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        db_path = os.path.join(self.data_dir, "ws.sqlite3")
        con = _workspace_db(db_path)
        _collab.create_workspace(con, "local", "Acme", "oidc:https://i.example:u1")
        con.commit()
        con.close()
        grants = os.path.join(self.tmp.name, "grants.sqlite3")
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir, grants_db=grants))
        _conn.register_workspace(self.app.store, "acme", db_path, self.data_dir)
        self._session_id = "sess-csrf-1"
        digest = hashlib.sha256(
            f"ew-browser-v1:{self._session_id}".encode()).hexdigest()
        now = time.time()
        raw = self.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject,"
                " email, email_verified, csrf_token, active_workspace,"
                " created_at, expires_at, revoked_at)"
                " VALUES (?, 'https://i.example', 'u1', '', 0, 'tok123', '',"
                " ?, ?, NULL)", (digest, now, now + 3600))
            raw.commit()
        finally:
            raw.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, path, method="POST", body=None, headers=None):
        raw_body = json.dumps(body or {}).encode()
        hdrs = [(b"host", b"127.0.0.1:8771"),
                (b"cookie", f"ew_session={self._session_id}".encode()),
                (b"content-length", str(len(raw_body)).encode())]
        for key, value in (headers or {}).items():
            hdrs.append((key.encode(), value.encode()))
        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "headers": hdrs, "query_string": b""}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body,
                    "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        status = next(m["status"] for m in sent
                      if m["type"] == "http.response.start")
        payload = b"".join(m.get("body", b"") for m in sent
                           if m["type"] == "http.response.body")
        try:
            data = json.loads(payload.decode() or "{}")
        except ValueError:
            data = {}
        return status, data

    def _csrf(self, origin="http://127.0.0.1:8771/"):
        headers = {"x-csrf-token": "tok123"}
        if origin is not None:
            headers["origin"] = origin
        return headers

    def test_workspace_switch_requires_token(self):
        status, _data = self._call(
            "/api/v1/session/workspace", body={"workspace_id": "acme",
                                               "expected_workspace_switch_revision": 0})
        self.assertEqual(status, 403)

    def test_workspace_switch_rejects_mismatching_origin(self):
        status, _data = self._call(
            "/api/v1/session/workspace", body={"workspace_id": "acme",
                                               "expected_workspace_switch_revision": 0},
            headers=self._csrf(origin="http://127.0.0.1:9999/"))
        self.assertEqual(status, 403)

    def test_workspace_switch_accepts_same_origin_token(self):
        status, data = self._call(
            "/api/v1/session/workspace", body={"workspace_id": "acme",
                                               "expected_workspace_switch_revision": 0},
            headers=self._csrf())
        self.assertEqual(status, 200)
        self.assertEqual(data.get("active_workspace"), "acme")

    def test_logout_requires_token(self):
        status, _data = self._call("/api/v1/session/logout", body={})
        self.assertEqual(status, 403)
        raw = self.app.store.auth_connection()
        try:
            session = _auth.lookup_session(raw, self._session_id)
        finally:
            raw.close()
        self.assertIsNotNone(session)

    def test_logout_revokes_with_token(self):
        status, _data = self._call("/api/v1/session/logout", body={},
                                   headers=self._csrf())
        self.assertEqual(status, 200)
        raw = self.app.store.auth_connection()
        try:
            session = _auth.lookup_session(raw, self._session_id)
        finally:
            raw.close()
        self.assertIsNone(session)


class HostedBrowserBootTest(unittest.TestCase):
    """Hosted browser boot end to end (follow-up to b411866).

    Unauthenticated customer routes return the sign-in state (never a
    generic error); an authenticated session without a workspace gets
    the workspace chooser (409 + workspace list, no protected read);
    selecting a workspace leads to a 200 overview through the shared
    rate limiter (no _throttle AttributeError, no throttle leak across
    sequential requests); foreign workspaces stay 403; the sign-in UI
    return path is an accepted same-origin app route. Two isolated
    cookie jars stand in for two browser profiles; no real account."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.realpath(os.path.join(self.tmp.name, "data"))
        os.makedirs(self.data_dir)
        alpha_db = os.path.join(self.data_dir, "alpha.sqlite3")
        con = _workspace_db(alpha_db, identity="ws-alpha")
        _collab.create_workspace(con, "local", "Alpha",
                                 "oidc:https://issuer.example:alice")
        con.commit()
        con.close()
        beta_db = os.path.join(self.data_dir, "beta.sqlite3")
        con = _workspace_db(beta_db, identity="ws-beta")
        _collab.create_workspace(con, "local", "Beta",
                                 "oidc:https://issuer.example:carol")
        con.commit()
        con.close()
        grants = os.path.join(self.tmp.name, "grants.sqlite3")
        self.app = _asgi.HostedApp(_asgi.HostedConfig(
            data_dir=self.data_dir, grants_db=grants))
        _conn.register_workspace(self.app.store, "alpha", alpha_db,
                                 self.data_dir)
        _conn.register_workspace(self.app.store, "beta", beta_db,
                                 self.data_dir)
        self.app.browser_config_override = _auth.BrowserAuthConfig(
            issuer="https://issuer.example", client_id="cid",
            audience="aud",
            redirect_uri="http://127.0.0.1:8771/api/v1/session/callback",
            authorization_endpoint="https://issuer.example/authorize",
            token_endpoint="https://issuer.example/token",
            jwks_uri="https://issuer.example/.well-known/jwks.json",
            allow_insecure_loopback=True)
        self._real_fetch = _auth._fetch_json
        _auth._fetch_json = lambda url, *, timeout=10.0: {
            "issuer": "https://issuer.example",
            "authorization_endpoint": "https://issuer.example/authorize",
            "token_endpoint": "https://issuer.example/token",
            "jwks_uri": "https://issuer.example/.well-known/jwks.json"}
        self.jar_a = {}
        self.jar_b = {}

    def tearDown(self):
        _auth._fetch_json = self._real_fetch
        self.tmp.cleanup()

    def _call(self, path, method="GET", body=None, jar=None, headers=None,
              query=""):
        raw_body = json.dumps(body or {}).encode() if body is not None \
            else b""
        hdrs = [(b"host", b"127.0.0.1:8771")]
        if jar:
            hdrs.append((b"cookie", "; ".join(
                f"{k}={v}" for k, v in jar.items()).encode()))
        if raw_body:
            hdrs.append((b"content-length", str(len(raw_body)).encode()))
        for key, value in (headers or {}).items():
            hdrs.append((key.encode(), value.encode()))
        scope = {"type": "http", "method": method, "path": path,
                 "scheme": "http", "headers": hdrs,
                 "query_string": query.encode()}
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw_body,
                    "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        if jar is not None:
            for name, value in start.get("headers", []):
                if name == b"set-cookie":
                    pair = value.decode().split(";", 1)[0]
                    if "=" in pair:
                        key, _, val = pair.partition("=")
                        if val:
                            jar[key.strip()] = val.strip()
                        else:
                            jar.pop(key.strip(), None)
        payload = b"".join(m.get("body", b"") for m in sent
                           if m["type"] == "http.response.body")
        try:
            data = json.loads(payload.decode() or "{}")
        except ValueError:
            data = {}
        return start["status"], data

    def _seed_session(self, session_id, subject, csrf, workspace=""):
        digest = hashlib.sha256(
            f"ew-browser-v1:{session_id}".encode()).hexdigest()
        now = time.time()
        raw = self.app.store.auth_connection()
        try:
            _auth.ensure_auth_schema(raw)
            raw.execute(
                "INSERT INTO browser_sessions (session_hash, issuer, subject,"
                " email, email_verified, csrf_token, active_workspace,"
                " created_at, expires_at, revoked_at)"
                " VALUES (?, 'https://issuer.example', ?, '', 0, ?, ?,"
                " ?, ?, NULL)",
                (digest, subject, csrf, workspace, now, now + 3600))
            raw.commit()
        finally:
            raw.close()

    def _csrf(self, token, origin="http://127.0.0.1:8771/"):
        headers = {"x-csrf-token": token}
        if origin is not None:
            headers["origin"] = origin
        return headers

    def test_unauthenticated_overview_returns_signin_state(self):
        status, data = self._call("/api/v1/customer/overview", jar=self.jar_a)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "SIGNIN_REQUIRED")
        self.assertIn("signin", data)
        status, data = self._call("/api/v1/session", jar=self.jar_a)
        self.assertEqual(status, 401)
        self.assertIn("signin", data)

    def test_authenticated_no_workspace_returns_chooser(self):
        self._seed_session("sess-alice-1", "alice", "csrf-a")
        self.jar_a["ew_session"] = "sess-alice-1"
        status, data = self._call("/api/v1/customer/overview", jar=self.jar_a)
        self.assertEqual(status, 409)
        self.assertEqual(data["error"]["code"], "WORKSPACE_REQUIRED")
        self.assertIn("alpha", data.get("workspaces", []))
        self.assertNotIn("beta", data.get("workspaces", []))

    def test_workspace_selection_leads_to_200_overview(self):
        self._seed_session("sess-alice-2", "alice", "csrf-a")
        self.jar_a["ew_session"] = "sess-alice-2"
        status, data = self._call(
            "/api/v1/session/workspace", method="POST",
            body={"workspace_id": "alpha", "expected_workspace_switch_revision": 0}, jar=self.jar_a,
            headers=self._csrf("csrf-a"))
        self.assertEqual(status, 200)
        self.assertEqual(data["active_workspace"], "alpha")
        # The shared limiter admits every sequential request: no 500
        # from a missing limiter, no 429 from a leaked throttle key.
        for _ in range(4):
            status, data = self._call("/api/v1/customer/overview",
                                      jar=self.jar_a)
            self.assertEqual(status, 200)
        self.assertNotIn("error", data)

    def test_cross_workspace_selection_denied(self):
        self._seed_session("sess-alice-3", "alice", "csrf-a")
        self.jar_a["ew_session"] = "sess-alice-3"
        status, data = self._call(
            "/api/v1/session/workspace", method="POST",
            body={"workspace_id": "beta", "expected_workspace_switch_revision": 0}, jar=self.jar_a,
            headers=self._csrf("csrf-a"))
        self.assertEqual(status, 403)
        status, _data = self._call("/api/v1/customer/overview",
                                   jar=self.jar_a)
        self.assertEqual(status, 409)

    def test_signin_return_path_accepted(self):
        self.assertTrue(_auth.allowed_return("/app/#/settings"))
        self.assertFalse(_auth.allowed_return("#/settings"))
        status, data = self._call(
            "/api/v1/session/login", method="POST",
            body={"return_to": "/app/#/settings"}, jar=self.jar_a)
        self.assertEqual(status, 200)
        self.assertIn("authorize_url", data)
        self.assertIn("ew_tx", self.jar_a)
        status, _data = self._call(
            "/api/v1/session/login", method="POST",
            body={"return_to": "#/settings"}, jar=self.jar_b)
        self.assertEqual(status, 400)

    def test_callback_without_proof_sets_no_session(self):
        status, _data = self._call(
            "/api/v1/session/login", method="POST",
            body={"return_to": "/"}, jar=self.jar_a)
        self.assertEqual(status, 200)
        leaked_state = "stolen-state"
        # Browser B replays a callback URL with no transaction cookie.
        status, data = self._call(
            "/api/v1/session/callback", jar=self.jar_b,
            query=f"state={leaked_state}&code=code-x")
        self.assertGreaterEqual(status, 400)
        self.assertNotIn("ew_session", self.jar_b)

    def test_settings_surface_payloads(self):
        """Route-dispatch gap (follow-up to 8b65438): /workspaces, /shares,
        and /billing never reached _browser_surface from __call__, so the
        settings page read 404s. After: the real payloads arrive through
        the existing _with_workspace authorization."""
        self._seed_session("sess-alice-5", "alice", "csrf-a", "alpha")
        self.jar_a["ew_session"] = "sess-alice-5"
        status, data = self._call("/api/v1/workspaces/alpha/members",
                                  jar=self.jar_a)
        self.assertEqual(status, 200)
        members = [m.get("member") for m in data.get("members", [])]
        self.assertIn("oidc:https://issuer.example:alice", members)
        status, data = self._call("/api/v1/shares", jar=self.jar_a)
        self.assertEqual(status, 200)
        self.assertIn("shares", data)
        status, data = self._call("/api/v1/billing/status", jar=self.jar_a)
        self.assertEqual(status, 200)
        self.assertFalse(data["configured"])
        self.assertFalse(data["entitlement"]["allowed"])

    def test_cross_workspace_members_nondisclosing(self):
        self._seed_session("sess-alice-6", "alice", "csrf-a", "alpha")
        self.jar_a["ew_session"] = "sess-alice-6"
        status, data = self._call("/api/v1/workspaces/beta/members",
                                  jar=self.jar_a)
        self.assertIn(status, (403, 404))
        self.assertNotIn("carol", json.dumps(data))

    def test_logout_revokes_session(self):
        self._seed_session("sess-alice-4", "alice", "csrf-a", "alpha")
        self.jar_a["ew_session"] = "sess-alice-4"
        status, _data = self._call("/api/v1/session/logout", method="POST",
                                   body={}, jar=self.jar_a,
                                   headers=self._csrf("csrf-a"))
        self.assertEqual(status, 200)
        status, data = self._call("/api/v1/customer/overview", jar=self.jar_a)
        self.assertEqual(status, 401)
        self.assertIn("signin", data)


if __name__ == "__main__":
    unittest.main()
