"""BY2: the purchase_seats table (purchase design 5.1).

A real control store (connections.GrantsStore on a temp file). The table
is created by the first write; the rules are the table's own (UNIQUE,
partial UNIQUE, CHECK) plus the transition list of 5.1.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from earwitness.app import connections as _conn
from earwitness.app import local_accounts as _la
from earwitness.app import purchases as _p

ISSUER = "https://app.example.test"


def reserve(con, sub, email, workspace, offer="ew-a"):
    return _p.reserve(con, subscription_id=sub, offer=offer,
                      customer_id="cus_" + sub, email=email, issuer=ISSUER,
                      workspace_id=workspace, run_key="run-1")


def refuse(con, sub, email, offer="ew-a"):
    return _p.refuse(con, subscription_id=sub, offer=offer,
                     customer_id="cus_" + sub, email=email, issuer=ISSUER,
                     run_key="run-1", reason="duplicate")


class _Store(unittest.TestCase):
    def setUp(self):
        # The local (password) issuer is the origin of this setting, as the
        # sign-in route and app_accounts read it.
        env = mock.patch.dict(os.environ, {"EARWITNESS_PUBLIC_BASE_URL": ISSUER + "/"})
        env.start()
        self.addCleanup(env.stop)
        self.dir = tempfile.mkdtemp(prefix="ew-by2-seats-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.store = _conn.GrantsStore(os.path.join(self.dir, "grants.sqlite3"))
        self.con = self.store.auth_connection()
        self.addCleanup(self.con.close)

    def walk(self, sub, *states):
        for state in states:
            _p.transition(self.con, sub, state, run_key="run-2")
        return _p.seat(self.con, sub)


class SchemaTest(_Store):
    def test_reads_before_the_first_write_are_no_seat(self):
        self.assertIsNone(_p.seat(self.con, "sub_1"))
        self.assertIsNone(_p.holding_seat(self.con, "oidc:x:y"))
        self.assertIsNone(_p.seat_for_workspace(self.con, "ws-a1"))

    def test_columns_are_the_design_columns(self):
        reserve(self.con, "sub_1", "Buyer@Example.com", "ws-a1")
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(purchase_seats)")]
        self.assertEqual(cols, ["subscription_id", "offer", "customer_id",
                                "principal", "email", "workspace_id", "state",
                                "run_key", "state_at", "last_error",
                                "credential_delivered_at"])

    def test_principal_is_the_password_sign_in_rule(self):
        row = reserve(self.con, "sub_1", "  Buyer@Example.com ", "ws-a1")
        self.assertEqual(row["email"], "buyer@example.com")
        self.assertEqual(row["principal"], _la.principal(ISSUER, "buyer@example.com"))
        self.assertEqual(row["principal"], _la.principal(ISSUER, "BUYER@example.COM"))
        self.assertEqual(row["state"], "reserved")

    def test_state_set_is_enforced_by_a_check(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE purchase_seats SET state='paid'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE purchase_seats SET offer='ew-c'")
        self.assertEqual(set(_p.STATES), {
            "reserved", "recorded", "bound", "active", "awaiting_account",
            "needs_refund", "refused", "ended", "retired"})

    def test_refused_holds_no_workspace_and_others_hold_one(self):
        refuse(self.con, "sub_r", "r@example.com")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE purchase_seats SET workspace_id='ws-a9'"
                             " WHERE subscription_id='sub_r'")
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute("UPDATE purchase_seats SET workspace_id=NULL"
                             " WHERE subscription_id='sub_1'")


class UniqueTest(_Store):
    def test_a_workspace_is_held_once_for_life_even_by_an_ended_row(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        self.walk("sub_1", "recorded", "bound", "active", "ended")
        with self.assertRaises(_p.SeatError):
            reserve(self.con, "sub_2", "b@example.com", "ws-a1")
        self.walk("sub_1", "retired")
        with self.assertRaises(_p.SeatError):
            reserve(self.con, "sub_3", "c@example.com", "ws-a1")
        self.assertIsNone(_p.seat(self.con, "sub_2"))
        self.assertEqual(self.con.execute(
            "SELECT COUNT(*) FROM purchase_seats").fetchone()[0], 1)

    def test_one_workspace_holding_seat_per_principal_for_life(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        self.walk("sub_1", "recorded", "bound", "active", "ended")
        with self.assertRaises(_p.SeatError):
            reserve(self.con, "sub_2", "A@EXAMPLE.com", "ws-b1", offer="ew-b")
        self.assertIsNone(_p.seat(self.con, "sub_2"))

    def test_the_same_principal_may_also_have_refused_rows(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        refuse(self.con, "sub_2", "a@example.com")
        refuse(self.con, "sub_3", "A@example.com", offer="ew-b")
        rows = self.con.execute("SELECT subscription_id, state, workspace_id"
                                " FROM purchase_seats ORDER BY 1").fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ("sub_1", "reserved", "ws-a1"), ("sub_2", "refused", None),
            ("sub_3", "refused", None)])
        self.assertEqual(_p.holding_seat(self.con, _la.principal(
            ISSUER, "a@example.com"))["subscription_id"], "sub_1")

    def test_a_refused_row_for_a_bad_email_has_no_principal(self):
        row = refuse(self.con, "sub_x", "not an email")
        self.assertEqual((row["state"], row["principal"], row["workspace_id"]),
                         ("refused", "", None))


class TransitionTest(_Store):
    def test_the_fulfilled_path(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        row = self.walk("sub_1", "recorded", "bound", "active", "ended", "retired")
        self.assertEqual((row["state"], row["workspace_id"], row["run_key"]),
                         ("retired", "ws-a1", "run-2"))

    def test_refusal_or_terminal_before_active_needs_a_refund(self):
        for n, upto in enumerate(([], ["recorded"], ["recorded", "bound"])):
            sub = f"sub_{n}"
            reserve(self.con, sub, f"b{n}@example.com", f"ws-a{n}")
            row = self.walk(sub, *upto, "needs_refund", "ended")
            self.assertEqual((row["state"], row["workspace_id"]), ("ended", f"ws-a{n}"))

    def test_a_fallback_seat_whose_subscription_ends_needs_a_refund(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        row = self.walk("sub_1", "recorded", "bound", "awaiting_account", "needs_refund",
                        "ended")
        self.assertEqual(row["state"], "ended")

    def test_the_store_creates_the_purchase_tables_up_front(self):
        names = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual({"purchase_seats", "purchase_cancel_requests",
                              "purchase_journal", "billing_deliveries"}, names)
        sql = self.con.execute("SELECT sql FROM sqlite_master WHERE name='purchase_seats'"
                               ).fetchone()[0]
        self.assertIn("credential_delivered_at TEXT,", sql)  # in the definition, not ALTERed

    def test_password_fallback_path(self):
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        row = self.walk("sub_1", "recorded", "bound", "awaiting_account", "active")
        self.assertEqual(row["state"], "active")

    def test_refused_reaches_ended_and_never_retired(self):
        refuse(self.con, "sub_r", "r@example.com")
        self.walk("sub_r", "ended")
        with self.assertRaises(_p.SeatError):
            _p.transition(self.con, "sub_r", "retired", run_key="run-3")

    def test_every_other_edge_is_refused(self):
        allowed = {(a, b) for a, nexts in _p.TRANSITIONS.items() for b in nexts}
        self.assertEqual(allowed, {
            ("reserved", "recorded"), ("reserved", "needs_refund"),
            ("recorded", "bound"), ("recorded", "needs_refund"),
            ("bound", "active"), ("bound", "awaiting_account"), ("bound", "needs_refund"),
            ("awaiting_account", "active"), ("awaiting_account", "needs_refund"),
            ("active", "ended"),
            ("needs_refund", "ended"), ("refused", "ended"), ("ended", "retired")})
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        for bad in ("bound", "active", "ended", "retired", "refused", "nonsense"):
            with self.subTest(bad):
                with self.assertRaises(_p.SeatError):
                    _p.transition(self.con, "sub_1", bad, run_key="run-9")
        self.assertEqual(_p.seat(self.con, "sub_1")["state"], "reserved")
        with self.assertRaises(_p.SeatError):
            _p.transition(self.con, "sub_missing", "recorded", run_key="run-9")


class LocalIssuerTest(_Store):
    """A seat is written only for the local password issuer (the origin of
    EARWITNESS_PUBLIC_BASE_URL, local_accounts.local_issuer), so no other
    issuer's identity can ever match a seat principal."""

    def count(self):
        try:
            return self.con.execute("SELECT COUNT(*) FROM purchase_seats").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    def test_another_issuer_is_refused_and_nothing_is_written(self):
        for issuer in ("https://accounts.google.com", ISSUER + "/",
                       "http://app.example.test", ""):
            with self.subTest(issuer=issuer):
                with self.assertRaises(_p.SeatError):
                    _p.reserve(self.con, subscription_id="sub_1", offer="ew-a",
                               customer_id="cus", email="a@example.com",
                               issuer=issuer, workspace_id="ws-a1", run_key="r")
                with self.assertRaises(_p.SeatError):
                    _p.refuse(self.con, subscription_id="sub_2", offer="ew-a",
                              customer_id="cus", email="a@example.com",
                              issuer=issuer, run_key="r", reason="x")
                self.assertEqual(self.count(), 0)

    def test_no_local_issuer_configured_refuses_every_write(self):
        for value in ("", "not a url"):
            with self.subTest(value=value):
                os.environ["EARWITNESS_PUBLIC_BASE_URL"] = value
                with self.assertRaises(_p.SeatError):
                    reserve(self.con, "sub_1", "a@example.com", "ws-a1")
                with self.assertRaises(_p.SeatError):
                    refuse(self.con, "sub_2", "a@example.com")
                self.assertEqual(self.count(), 0)

    def test_the_local_issuer_is_accepted(self):
        self.assertEqual(reserve(self.con, "sub_1", "a@example.com", "ws-a1")["state"],
                         "reserved")
        self.assertEqual(refuse(self.con, "sub_2", "b@example.com")["state"], "refused")


class CallerTransactionTest(_Store):
    """Design 5.3: the window selects, checks and inserts in one caller-owned
    BEGIN IMMEDIATE transaction. reserve/refuse never commit or end it, on
    the real GrantsStore connection, whether the table already exists or is
    created by this first write."""

    def setUp(self):
        super().setUp()
        self.con.execute("CREATE TABLE caller_marker (note TEXT)")
        self.con.commit()

    def other(self):
        con = sqlite3.connect(self.store.path, timeout=0)
        self.addCleanup(con.close)
        return con

    def check(self, write, *, prepared):
        if prepared:
            _p.ensure_schema(self.con)
            self.con.commit()
        self.con.execute("BEGIN IMMEDIATE")
        self.con.execute("INSERT INTO caller_marker VALUES ('earlier write')")
        row = write(self.con)
        self.assertTrue(self.con.in_transaction, "the caller's transaction ended")
        self.assertIsNotNone(row)
        # Another connection sees none of the uncommitted work and cannot
        # take the write lock while the caller holds it.
        peer = self.other()
        self.assertEqual(peer.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0], 0)
        self.assertIsNone(_p.seat(peer, row["subscription_id"]))
        with self.assertRaises(sqlite3.OperationalError):
            peer.execute("BEGIN IMMEDIATE")
        try:
            raise RuntimeError("a later step of the window fails")
        except RuntimeError:
            self.con.rollback()
        fresh = self.other()
        self.assertEqual(fresh.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0], 0)
        self.assertIsNone(_p.seat(fresh, row["subscription_id"]))
        self.assertIsNone(_p.seat(self.con, row["subscription_id"]))

    def test_reserve_keeps_the_caller_transaction_with_a_prepared_table(self):
        self.check(lambda con: reserve(con, "sub_1", "a@example.com", "ws-a1"), prepared=True)

    def test_reserve_keeps_the_caller_transaction_on_the_first_write(self):
        self.check(lambda con: reserve(con, "sub_1", "a@example.com", "ws-a1"), prepared=False)

    def test_refuse_keeps_the_caller_transaction_with_a_prepared_table(self):
        self.check(lambda con: refuse(con, "sub_r", "r@example.com"), prepared=True)

    def test_refuse_keeps_the_caller_transaction_on_the_first_write(self):
        self.check(lambda con: refuse(con, "sub_r", "r@example.com"), prepared=False)

    def test_a_committed_reservation_is_seen_by_another_connection(self):
        self.con.execute("BEGIN IMMEDIATE")
        reserve(self.con, "sub_1", "a@example.com", "ws-a1")
        self.con.commit()
        self.assertEqual(_p.seat(self.other(), "sub_1")["state"], "reserved")


if __name__ == "__main__":
    unittest.main()
