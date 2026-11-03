"""BY2: purchase seats in admission (purchase design 5.2, 8.7 "Admission").

A real control store with `purchase_seats` rows written through
purchases.py, a real admission file, the real session lookup
(browser_auth.lookup_session), the real workspace creation
(services.collab_create_workspace) and the real workspace open
(services.open_customer_workspace). Start-up refusals go through
admission.config_problems and health.check_config in production mode.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import admission as _adm
from earwitness.app import asgi as _asgi
from earwitness.app import billing as _billing
from earwitness.app import browser_auth as _auth
from earwitness.app import collaboration as _collab
from earwitness.app import connections as _conn
from earwitness.app import health as _health
from earwitness.app import purchases as _p
from earwitness.app import services as _services
from tests import backend_fixtures

REPO = Path(__file__).resolve().parents[1]
ISSUER = "https://app.example.test"   # the local (password) issuer
LISTED = f"oidc:{ISSUER}:listed"
ENV_KEYS = ("EARWITNESS_ADMISSION_FILE", "EARWITNESS_MODE", "EARWITNESS_GRANTS_DB",
            "EARWITNESS_PURCHASE_LINK_LIMITS", "EARWITNESS_BILLING_COMPED_WORKSPACES",
            "EARWITNESS_BILLING_POOL_WORKSPACES", "EARWITNESS_BILLING_PRICES",
            "EARWITNESS_PUBLIC_BASE_URL")
POOLS = {"ew-a": ["pool-a1", "pool-a2"], "ew-b": ["pool-b1"]}
PRICES = {"price_a": {"offer": "ew-a"}, "price_a_year": {"offer": "ew-a"},
          "price_b": {"offer": "ew-b"}}


def doc(*, principals=(LISTED,), pools=None, enabled=True, removed=(),
        retired=(), max_accounts=10, max_workspaces=10):
    out = {"status": "approved", "principals": list(principals),
           "verified_emails": [], "max_accounts": max_accounts,
           "max_workspaces": max_workspaces,
           "purchase_seats": {"enabled": enabled,
                              "pools": POOLS if pools is None else pools}}
    if removed:
        out["removed_principals"] = list(removed)
    if retired:
        out["retired_workspace_ids"] = list(retired)
    return out


def subject_of(principal: str) -> str:
    return principal.rsplit(":", 1)[1]


class _Base(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ew-by2-adm-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        (self.work / "control").mkdir()
        self.data_dir = str(self.work / "workspaces")
        os.makedirs(self.data_dir)
        self.grants = str(self.work / "control" / "grants.sqlite3")
        self.store = _conn.GrantsStore(self.grants)
        os.environ["EARWITNESS_GRANTS_DB"] = self.grants
        os.environ["EARWITNESS_PUBLIC_BASE_URL"] = ISSUER + "/"
        self.n = 0

    def admit(self, document) -> str:
        path = self.work / "admission.json"
        path.write_text(json.dumps(document))
        os.environ["EARWITNESS_ADMISSION_FILE"] = str(path)
        return str(path)

    def seat(self, workspace, state, *, offer="ew-a", email=None):
        """A seat for a new buyer, walked to `state`; returns P."""
        self.n += 1
        sub = f"sub_{self.n}"
        email = email or f"buyer{self.n}@example.com"
        con = self.store.auth_connection()
        try:
            if state == "refused":
                row = _p.refuse(con, subscription_id=sub, offer=offer,
                                customer_id="cus", email=email, issuer=ISSUER,
                                run_key="r", reason="test")
            else:
                row = _p.reserve(con, subscription_id=sub, offer=offer,
                                 customer_id="cus", email=email, issuer=ISSUER,
                                 workspace_id=workspace, run_key="r")
                path = {"reserved": [], "recorded": ["recorded"],
                        "bound": ["recorded", "bound"],
                        "active": ["recorded", "bound", "active"],
                        "awaiting_account": ["recorded", "bound", "awaiting_account"],
                        "needs_refund": ["needs_refund"],
                        "ended": ["recorded", "bound", "active", "ended"],
                        "retired": ["recorded", "bound", "active", "ended", "retired"],
                        }[state]
                for step in path:
                    row = _p.transition(con, sub, step, run_key="r")
            con.commit()
        finally:
            con.close()
        self.last_sub = sub
        return row["principal"]

    def set_state(self, sub, state):
        con = self.store.auth_connection()
        try:
            _p.transition(con, sub, state, run_key="r2")
            con.commit()
        finally:
            con.close()

    def decide(self, principal, **kw):
        return _adm.decide(ISSUER, subject_of(principal), "", False, **kw)

    def register(self, name):
        db = backend_fixtures.make_workspace(Path(self.data_dir) / name, schema_version=8)
        con = sqlite3.connect(str(db), isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            _collab.create_workspace(con, "local", name, LISTED)
        finally:
            con.close()
        _conn.register_workspace(self.store, name, str(db), self.data_dir)
        return str(db)


class DecideTest(_Base):
    def test_only_active_admits(self):
        pool = [f"pool-a{i}" for i in range(1, len(_p.STATES) + 1)]
        self.admit(doc(pools={"ew-a": pool, "ew-b": []}, max_accounts=20))
        for n, state in enumerate(_p.STATES):
            with self.subTest(state=state):
                principal = self.seat(pool[n], state)
                if state == "refused":
                    # A refused row holds no workspace: its principal
                    # (from the same email rule) is not admitted.
                    self.assertEqual(self.decide(principal), _adm.EXCLUDED)
                    continue
                want = _adm.ADMITTED if state == "active" else _adm.EXCLUDED
                self.assertEqual(self.decide(principal), want)

    def test_a_seat_admits_only_its_holder(self):
        self.admit(doc())
        holder = self.seat("pool-a1", "active", email="holder@example.com")
        self.assertEqual(self.decide(holder), _adm.ADMITTED)
        from earwitness.app import local_accounts as _la
        other = _la.principal(ISSUER, "someone@example.com")
        self.assertEqual(self.decide(other), _adm.EXCLUDED)
        # Another issuer's subject with the same text is not the holder.
        self.assertEqual(_adm.decide("https://other.example", subject_of(holder)),
                         _adm.EXCLUDED)

    def test_the_workspace_must_be_in_its_offers_pool(self):
        self.admit(doc())
        in_other = self.seat("pool-b1", "active", offer="ew-a")
        self.assertEqual(self.decide(in_other), _adm.EXCLUDED)
        in_none = self.seat("not-a-pool", "active", offer="ew-a")
        self.assertEqual(self.decide(in_none), _adm.EXCLUDED)
        right = self.seat("pool-b1x", "active", offer="ew-b")
        self.assertEqual(self.decide(right), _adm.EXCLUDED)
        self.admit(doc(pools={"ew-a": ["pool-a1"], "ew-b": ["pool-b1x"]}))
        self.assertEqual(self.decide(right), _adm.ADMITTED)

    def test_disabled_block_admits_no_seat(self):
        self.admit(doc(enabled=False))
        principal = self.seat("pool-a1", "active")
        self.assertEqual(self.decide(principal), _adm.EXCLUDED)
        self.assertEqual(self.decide(LISTED), _adm.ADMITTED)

    def test_removed_principals_beats_a_seat(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        self.assertEqual(self.decide(principal), _adm.ADMITTED)
        self.admit(doc(removed=[principal]))
        self.assertEqual(self.decide(principal), _adm.EXCLUDED)

    def test_ending_a_seat_stops_admitting(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        self.set_state(self.last_sub, "ended")
        self.assertEqual(self.decide(principal), _adm.EXCLUDED)

    def test_unreadable_seats_deny_without_revoking(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        os.environ["EARWITNESS_GRANTS_DB"] = str(self.work / "absent.sqlite3")
        self.assertEqual(self.decide(principal), _adm.UNAVAILABLE)
        # A listed principal never needs the seat read.
        self.assertEqual(self.decide(LISTED), _adm.ADMITTED)

    def test_a_seat_never_admits_another_issuers_identity(self):
        # open_issuer_session serves the local issuer and the OIDC provider;
        # a seat is exactly oidc:<local issuer>:<subject>, nothing else.
        self.admit(doc())
        principal = self.seat("pool-a1", "active", email="buyer@example.com")
        subject = subject_of(principal)
        for issuer in ("https://accounts.google.com", ISSUER + "/", "http://app.example.test"):
            with self.subTest(issuer=issuer):
                self.assertEqual(_adm.decide(issuer, subject, "buyer@example.com",
                                             True), _adm.EXCLUDED)
                with self.assertRaises(_adm.AdmissionDenied):
                    _adm.require_admitted(issuer, subject, "buyer@example.com",
                                          True, seats=True)
        self.assertEqual(_adm.decide(ISSUER, subject, "", False), _adm.ADMITTED)

    def test_bearer_grants_and_require_admitted_stay_principals_only(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        self.assertFalse(_adm.is_principal_admitted(principal))
        with self.assertRaises(_adm.AdmissionDenied):
            _adm.require_admitted(ISSUER, subject_of(principal), "", True)
        _adm.require_admitted(ISSUER, subject_of(principal), "", True, seats=True)


class SessionTest(_Base):
    """Through the real session lookup, workspace creation and open."""

    def setUp(self):
        super().setUp()
        self.con = self.store.auth_connection()
        self.addCleanup(self.con.close)
        _auth.ensure_auth_schema(self.con)
        self.con.commit()

    def session(self, principal):
        now = time.time()
        sid = "sess-" + subject_of(principal)
        self.con.execute(
            "INSERT INTO browser_sessions(session_hash,issuer,subject,email,"
            "email_verified,csrf_token,active_workspace,created_at,expires_at)"
            " VALUES (?,?,?,?,1,'tok','',?,?)",
            (_auth._hash_presented(sid), ISSUER, subject_of(principal),
             "b@example.com", now, now + 3600))
        self.con.commit()
        return sid

    def test_an_active_seat_keeps_its_session_and_an_ended_one_loses_it(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        sid = self.session(principal)
        self.assertIsNotNone(_auth.lookup_session(self.con, sid))
        self.set_state(self.last_sub, "ended")
        self.assertIsNone(_auth.lookup_session(self.con, sid))
        row = self.con.execute("SELECT revoked_at FROM browser_sessions").fetchone()
        self.assertIsNotNone(row[0])

    def test_a_seat_cannot_create_a_workspace(self):
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        session = _auth.lookup_session(self.con, self.session(principal))
        self.assertIsNotNone(session)
        ctx = _auth.build_session_context(session, "", "viewer", "req-1")
        ctx.authority_kind = "bootstrap"
        ctx.scopes = frozenset({"workspace:create"})
        with self.assertRaises(_access.AuthorizationError) as raised:
            _services.collab_create_workspace(self.store, self.data_dir, ctx,
                                              "buyer-new", "Mine")
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(os.listdir(self.data_dir), [])
        self.assertEqual(self.store.list_registered_workspaces(), [])

    def test_a_seat_reaches_only_its_own_workspace(self):
        self.register("pool-a1")
        self.register("pool-a2")
        self.admit(doc())
        principal = self.seat("pool-a1", "active")
        # The window adds the holder as the owner member of their workspace.
        entry = _conn.resolve_workspace(self.store, "pool-a1")
        con = sqlite3.connect(entry["db_path"])
        try:
            con.execute("INSERT INTO memberships (workspace_id, member, role, created_at)"
                        " VALUES ('local', ?, 'owner', '2026-10-21T00:00:00+00:00')",
                        (principal,))
            con.commit()
        finally:
            con.close()
        session = _auth.lookup_session(self.con, self.session(principal))
        own = _auth.build_session_context(session, "pool-a1", "owner", "r1")
        opened, _entry = _services.open_customer_workspace(
            self.store, "pool-a1", self.data_dir, own)
        opened.close()
        other = _auth.build_session_context(session, "pool-a2", "owner", "r2")
        with self.assertRaises(_access.AuthorizationError) as raised:
            _services.open_customer_workspace(self.store, "pool-a2", self.data_dir, other)
        self.assertEqual(raised.exception.status, 403)


class StartupTest(_Base):
    """Every refusal of 5.2, each its own test, in config_problems and in
    the production start-up check."""

    def setUp(self):
        super().setUp()
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-a": 2, "ew-b": 1})
        os.environ["EARWITNESS_BILLING_PRICES"] = json.dumps(PRICES)
        for name in ("pool-a1", "pool-a2", "pool-b1"):
            self.register(name)
        self.kwargs = dict(
            mode="production", pilot_features=(), pilot_end="2099-12-31T23:59:59Z",
            grants_db=self.grants, data_dir=self.data_dir, app_dir=str(REPO / "app"),
            allowed_hosts=("app.example.test",),
            allowed_origins=("https://app.example.test",), web_processes=1,
            resource_uri="https://app.example.test/mcp",
            issuer="https://auth.example.test/")

    def problems(self):
        return _adm.config_problems()

    def refused(self, words):
        problems = self.problems()
        self.assertTrue(any(words in p for p in problems), problems)
        started = _health.check_config(_asgi.HostedConfig(**self.kwargs))
        self.assertTrue(any(words in p for p in started), started)

    def test_a_valid_block_passes(self):
        self.admit(doc())
        self.assertEqual(self.problems(), [])

    def test_a_workspace_in_two_pools(self):
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-b1"], "ew-b": ["pool-b1"]}))
        self.refused("two purchase pools")
        # Fail closed at run time too: the file does not load.
        self.assertEqual(_adm.decide(ISSUER, "listed"), _adm.UNAVAILABLE)

    def test_a_comped_pool_workspace(self):
        self.admit(doc())
        os.environ["EARWITNESS_BILLING_COMPED_WORKSPACES"] = "demo,pool-a2"
        self.refused("also in EARWITNESS_BILLING_COMPED_WORKSPACES")

    def test_an_unregistered_pool_workspace(self):
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2", "pool-a3"],
                              "ew-b": ["pool-b1"]}))
        self.refused("not registered")

    def test_a_retired_listed_workspace_with_a_retired_seat_is_allowed(self):
        self.seat("pool-a3", "retired")
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2", "pool-a3"],
                              "ew-b": ["pool-b1"]}, retired=["pool-a3"]))
        self.assertEqual(self.problems(), [])

    def test_a_retired_listed_workspace_without_a_retired_seat(self):
        self.seat("pool-a3", "ended")
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2", "pool-a3"],
                              "ew-b": ["pool-b1"]}, retired=["pool-a3"]))
        self.refused("not registered")

    def test_an_unlisted_workspace_with_a_retired_seat(self):
        self.seat("pool-a3", "retired")
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2", "pool-a3"],
                              "ew-b": ["pool-b1"]}))
        self.refused("not registered")

    def test_max_accounts_below_principals_plus_pools(self):
        self.admit(doc(max_accounts=3))   # 1 principal + 3 pool workspaces
        self.refused("max_accounts is below the principals plus the purchase pool")
        self.admit(doc(max_accounts=4))
        self.assertEqual(self.problems(), [])

    def test_max_workspaces_below_registered_plus_pools(self):
        self.register("demo")
        self.admit(doc(max_workspaces=3))  # 4 registered, pools inside them
        self.refused("max_workspaces is below the registered workspaces")
        self.admit(doc(max_workspaces=4))
        self.assertEqual(self.problems(), [])

    def test_a_retired_pool_workspace_still_counts_toward_max_workspaces(self):
        self.seat("pool-a3", "retired")
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2", "pool-a3"],
                              "ew-b": ["pool-b1"]}, retired=["pool-a3"], max_workspaces=3))
        self.refused("max_workspaces is below the registered workspaces")

    def test_a_payment_link_limit_above_its_pool(self):
        self.admit(doc())
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-a": 2, "ew-b": 2})
        self.refused("a Payment Link limit is above its plan's pool size")

    def test_empty_link_limits_are_refused(self):
        self.admit(doc())
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = "{}"
        self.refused("must state each Payment Link limit")

    def test_link_limits_without_ew_b_are_refused(self):
        self.admit(doc())
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-a": 2})
        self.refused("must state each Payment Link limit")

    def test_link_limits_without_ew_a_are_refused(self):
        self.admit(doc())
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-b": 1})
        self.refused("must state each Payment Link limit")

    def test_an_offer_with_an_empty_pool_needs_no_link_limit_or_price(self):
        self.admit(doc(pools={"ew-a": ["pool-a1", "pool-a2"], "ew-b": []}))
        os.environ["EARWITNESS_BILLING_PRICES"] = json.dumps({"price_a": {"offer": "ew-a"}})
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-a": 2})
        self.assertEqual(self.problems(), [])
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = json.dumps({"ew-a": 2, "ew-b": 0})
        self.assertEqual(self.problems(), [])

    def test_an_ew_a_pool_without_an_ew_a_price(self):
        self.admit(doc())
        os.environ["EARWITNESS_BILLING_PRICES"] = json.dumps(
            {"price_b": {"offer": "ew-b"}, "price_x": {"trial_days": 0}})
        self.refused("EARWITNESS_BILLING_PRICES must tag a price with each plan")

    def test_an_ew_b_pool_without_an_ew_b_price(self):
        self.admit(doc())
        os.environ["EARWITNESS_BILLING_PRICES"] = json.dumps(
            {"price_a": {"offer": "ew-a"}, "price_x": {"offer": "ew-c"}})
        self.refused("EARWITNESS_BILLING_PRICES must tag a price with each plan")

    def test_a_non_text_offer_tag_is_a_named_refusal_not_a_crash(self):
        # Code review r2 N1: a list or object as the offer crashed start-up.
        self.admit(doc())
        for bad in (["ew-a"], {"ew-a": 1}, 7):
            with self.subTest(bad=bad):
                os.environ["EARWITNESS_BILLING_PRICES"] = json.dumps(
                    {"price_a": {"offer": bad}, "price_b": {"offer": "ew-b"}})
                self.refused("EARWITNESS_BILLING_PRICES must tag a price with each plan")

    def test_no_prices_at_all(self):
        self.admit(doc())
        for raw in (None, "", "{}", "not json", "[]"):
            with self.subTest(raw=raw):
                if raw is None:
                    os.environ.pop("EARWITNESS_BILLING_PRICES", None)
                else:
                    os.environ["EARWITNESS_BILLING_PRICES"] = raw
                self.refused("EARWITNESS_BILLING_PRICES must tag a price with each plan")

    def test_a_disabled_block_needs_no_prices_or_link_limits(self):
        self.admit(doc(enabled=False))
        os.environ.pop("EARWITNESS_BILLING_PRICES")
        os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = "{}"
        self.assertEqual(self.problems(), [])

    def test_missing_or_malformed_link_limits(self):
        self.admit(doc())
        os.environ.pop("EARWITNESS_PURCHASE_LINK_LIMITS")
        self.refused("must state each Payment Link limit")
        for raw in ("[]", '{"ew-c": 1}', '{"ew-a": -1}', '{"ew-a": true}', "nope"):
            with self.subTest(raw=raw):
                os.environ["EARWITNESS_PURCHASE_LINK_LIMITS"] = raw
                self.refused("EARWITNESS_PURCHASE_LINK_LIMITS must be a JSON object")


class BillingPoolSourceTest(_Base):
    """BY1's comped-overlap refusal now reads the admission file's pools."""

    def test_comped_workspace_in_an_admission_pool_refuses_billing_config(self):
        self.admit(doc())
        env = dict(os.environ, EARWITNESS_BILLING_COMPED_WORKSPACES="demo,pool-b1")
        with self.assertRaises(ValueError) as raised:
            _billing.config_from_env(env)
        self.assertIn("purchase pools", str(raised.exception))
        env["EARWITNESS_BILLING_COMPED_WORKSPACES"] = "demo"
        config = _billing.config_from_env(env)
        self.assertEqual(config.pool_workspaces, ("pool-a1", "pool-a2", "pool-b1"))

    def test_the_old_pool_setting_is_refused(self):
        env = dict(os.environ, EARWITNESS_BILLING_POOL_WORKSPACES="a1")
        with self.assertRaises(ValueError) as raised:
            _billing.config_from_env(env)
        self.assertIn("was removed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()


from tests import test_password_signin as _pw  # noqa: E402


class _SeatPwBase(_pw._Base):
    """A buyer who is not in `principals` and holds an `active` seat."""

    def setUp(self):
        super().setUp()
        os.environ["EARWITNESS_PUBLIC_BASE_URL"] = _pw.BASE + "/"
        self.admission = {"status": "approved", "principals": [],
                          "removed_principals": [], "verified_emails": [],
                          "purchase_seats": {"enabled": True, "pools": POOLS}}
        self.write_admission()
        os.environ["EARWITNESS_GRANTS_DB"] = self.grants
        self.add_account()
        self.sub = "sub_buyer"
        con = self.control()
        try:
            _p.reserve(con, subscription_id=self.sub, offer="ew-a",
                       customer_id="cus", email=_pw.E, issuer=_pw.BASE,
                       workspace_id="pool-a1", run_key="r")
            for step in ("recorded", "bound", "active"):
                _p.transition(con, self.sub, step, run_key="r")
            con.commit()
        finally:
            con.close()

    def move(self, state):
        con = self.control()
        try:
            _p.transition(con, self.sub, state, run_key="r2")
            con.commit()
        finally:
            con.close()


class SeatPasswordSignInTest(_SeatPwBase):
    """Through the real POST /api/v1/session/password: a buyer is not in
    `principals`; only their `active` seat admits them (design 5.2)."""

    async def test_an_active_seat_holder_signs_in(self):
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 200, data)
        self.assertIn(_auth.SESSION_COOKIE, jar)
        self.assertEqual(len(self.sessions()), 1)

    async def test_an_ended_seat_holder_gets_the_admission_403(self):
        self.move("ended")
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], _adm.REFUSAL)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        self.assertEqual(self.sessions(), [])

    async def test_removed_principals_beats_an_active_seat_at_sign_in(self):
        self.admission["removed_principals"].append(_pw.P)
        self.write_admission()
        status, _raw, data, jar = await self.login()
        self.assertEqual(status, 403, data)
        self.assertNotIn(_auth.SESSION_COOKIE, jar)
        self.assertEqual(self.sessions(), [])

    async def test_a_disabled_block_admits_no_seat_at_sign_in(self):
        self.admission["purchase_seats"]["enabled"] = False
        self.write_admission()
        status, _raw, data, _jar = await self.login()
        self.assertEqual(status, 403, data)
        self.assertEqual(self.sessions(), [])


class SeatChangePasswordTest(_SeatPwBase):
    """Through the real POST /api/v1/session/password/change: the commit
    fence admits an active seat holder as sign-in does (code review r1 H1)."""

    signed_in = _pw.ChangePasswordTest.signed_in
    change = _pw.ChangePasswordTest.change
    stored_hash = _pw.ChangePasswordTest.stored_hash

    async def test_an_active_seat_holder_changes_their_password(self):
        cookie, csrf = await self.signed_in()
        status, _raw, data, _jar = await self.change(cookie, csrf)
        self.assertEqual(status, 200, data)
        self.assertEqual(data, {"ok": True})
        self.assertTrue(_pw.la.verify(self.stored_hash(), _pw.NEW_PW))

    async def test_an_ended_seat_holder_cannot_change_it(self):
        cookie, csrf = await self.signed_in()
        before = self.stored_hash()
        self.move("ended")
        status, _raw, data, _jar = await self.change(cookie, csrf)
        self.assertEqual(status, 401, data)
        self.assertEqual(self.stored_hash(), before)

    async def test_a_removed_seat_holder_cannot_change_it(self):
        cookie, csrf = await self.signed_in()
        before = self.stored_hash()
        self.admission["removed_principals"].append(_pw.P)
        self.write_admission()
        status, _raw, data, _jar = await self.change(cookie, csrf)
        self.assertEqual(status, 401, data)
        self.assertEqual(self.stored_hash(), before)

    async def test_the_commit_fence_refuses_a_seat_that_ends_mid_request(self):
        # The session lookup has passed; only the fence's own admission read
        # sees a seat that no longer admits (the request holds the write
        # lock, so the seat's end is simulated in that read), and refuses
        # with the admission 403.
        cookie, csrf = await self.signed_in()
        before = self.stored_hash()
        real = _adm.require_admitted

        def fence(*args, **kw):
            with mock.patch.object(_p, "ADMITTING_STATES", frozenset()):
                return real(*args, **kw)
        with mock.patch.object(_adm, "require_admitted", side_effect=fence) as seen:
            status, _raw, data, _jar = await self.change(cookie, csrf)
        self.assertEqual(seen.call_count, 1)
        self.assertEqual(status, 403, data)
        self.assertEqual(data["error"]["message"], _adm.REFUSAL)
        self.assertEqual(self.stored_hash(), before)
