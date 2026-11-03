"""Jev spend cap (design 2026-10-21 section 10): the hash-chained ledger, its
pin, reservations per HTTP request, LA periods, recovery mode, and the one
send path. Offline: fake openers only, no key, no network."""
from __future__ import annotations

import ast
import io
import json
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from earwitness.pipeline import jev_budget as jb
from earwitness.pipeline import jev_screen as js
from earwitness.pipeline import pod_naming

REPO = Path(__file__).resolve().parent.parent
LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> float:
    return datetime(*args, tzinfo=LA).timestamp()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def ok_response(tokens=100):
    return {"model": "jev-1.13.0", "answers": {},
            "usage": {"input_tokens": tokens, "output_tokens": 0}}


def script_opener(script, seen):
    def open_(req, timeout=None):
        seen.append(req)
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return FakeResponse(json.dumps(step).encode())
    return open_


BODY = {"model": "jev-1.13.0", "state": {"episode_id": "ep_1", "windows": {}},
        "questions": {}}
BODY_BYTES = len(json.dumps(BODY).encode())


class BudgetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jevbudget-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = self.tmp / "worker" / "jev-budget.sqlite3"
        self.pin = self.tmp / "config" / "earwitness-jev-budget.pin"
        self.now = [la(2026, 10, 21, 12, 0, 0)]

    def clock(self):
        return self.now[0]

    def init(self):
        jb.init(self.ledger, self.pin, first_activation=True, clock=self.clock)

    def budget(self, **kw):
        kw.setdefault("clock", self.clock)
        return jb.JevBudget(self.ledger, self.pin, **kw)

    def max_charge(self, nbytes=BODY_BYTES):
        return jb.max_charge_usd(nbytes, js.MAX_OUTPUT_TOKENS)

    def reserve(self, b, nbytes=BODY_BYTES, episode="ep_1"):
        return b.reserve(purpose="screen", episode=episode, body_bytes=nbytes,
                         output_tokens_max=js.MAX_OUTPUT_TOKENS)

    def transport(self, budget, script, seen, **kw):
        kw.setdefault("sleep", lambda s: None)
        return js.UrllibTransport("sk-test-secret", budget=budget,
                                  opener=script_opener(script, seen), **kw)


class MaxChargeTest(BudgetBase):
    def test_bytes_times_price_plus_output_plus_25_percent(self):
        got = jb.max_charge_usd(1_000_000, 0)
        self.assertAlmostEqual(got, 0.042 * 1.25, places=9)
        self.assertEqual(jb.max_charge_nusd(10, 0), 525)  # 10 x 42 n$ x 1.25, exact
        self.assertEqual(jb.max_charge_nusd(1, 0), 53)  # rounded up

    def test_no_output_bound_or_unknown_price_is_not_sendable(self):
        self.init()
        b = self.budget()
        with self.assertRaises(jb.NotSendable):
            b.reserve(purpose="screen", episode="e", body_bytes=10, output_tokens_max=None)
        b = self.budget(pricing=jb.Pricing(None, 0.0, "x", "2026-10-21"))
        with self.assertRaises(jb.NotSendable):
            self.reserve(b)


class ReserveTest(BudgetBase):
    def test_two_workers_at_the_boundary_only_one_fits(self):
        self.init()
        m = self.max_charge()
        cap = m * 1.5
        barrier = threading.Barrier(2)
        results = []

        def worker():
            b = self.budget(day_cap_usd=cap)
            barrier.wait()
            try:
                self.reserve(b)
                results.append("ok")
            except jb.BudgetExhausted:
                results.append("refused")
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertAlmostEqual(self.budget().status()["day"]["committed_usd"], m, places=9)

    def test_cap_is_inclusive_and_month_cap_applies(self):
        self.init()
        m = self.max_charge()
        b = self.budget(day_cap_usd=2 * m)
        self.reserve(b)
        self.reserve(b)                    # exactly at the cap: fits
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.code, "JEV_AT_CAP")
        b = self.budget(month_cap_usd=2 * m)
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(b)

    def test_settled_actual_replaces_the_maximum(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        b.settle(r, 0.000001)
        st = self.budget().status()
        self.assertAlmostEqual(st["day"]["committed_usd"], 0.000001, places=9)
        self.assertEqual(st["mode"], "normal")

    def test_updates_are_new_rows_and_the_table_refuses_update_and_delete(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        b.settle(r, 0.0)
        con = sqlite3.connect(self.ledger)
        rows = con.execute("SELECT reservation_id, state FROM entries ORDER BY seq").fetchall()
        self.assertEqual(rows, [(r.id, "reserved"), (r.id, "settled")])
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("UPDATE entries SET state='settled'")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM entries")
        con.close()
        pin = json.loads(self.pin.read_text())
        self.assertEqual(pin["rows"], 2)


class PeriodTest(BudgetBase):
    def test_la_day_rollover(self):
        self.init()
        m = self.max_charge()
        self.now[0] = la(2026, 10, 21, 23, 59, 59)   # 06:59:59 UTC on Oct 22
        b = self.budget(day_cap_usd=m)
        self.reserve(b)
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(b)
        self.assertEqual(b.status()["day"]["period"], "2026-10-21")
        self.now[0] = la(2026, 10, 22, 0, 0, 1)
        self.reserve(b)
        self.assertEqual(b.status()["day"]["period"], "2026-10-22")

    def test_la_month_rollover(self):
        self.init()
        m = self.max_charge()
        self.now[0] = la(2026, 10, 31, 23, 0, 0)
        b = self.budget(month_cap_usd=m)
        self.reserve(b)
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(b)
        self.now[0] = la(2026, 11, 1, 0, 0, 1)      # UTC is already Nov 1 07:00
        self.reserve(b)
        self.assertEqual(b.status()["month"]["period"], "2026-11")


class TransportBudgetTest(BudgetBase):
    def test_timeout_after_send_is_unknown_at_max(self):
        self.init()
        m = self.max_charge()
        seen = []
        t = self.transport(self.budget(), [socket.timeout("timed out")], seen, retries=0)
        with self.assertRaises(js.JevError):
            t(BODY)
        self.assertEqual(len(seen), 1)
        st = self.budget().status()
        self.assertAlmostEqual(st["day"]["committed_usd"], m, places=9)
        self.assertEqual(st["day"]["unknown"], 1)
        # a reservation that fits only if the unknown were not counted is refused
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget(day_cap_usd=1.5 * m))

    def test_malformed_response_is_unknown(self):
        self.init()
        seen = []

        def opener(req, timeout=None):
            seen.append(req)
            return FakeResponse(b"not json")
        t = js.UrllibTransport("k", budget=self.budget(), opener=opener, sleep=lambda s: None)
        with self.assertRaises(Exception):
            t(BODY)
        self.assertEqual(self.budget().status()["day"]["unknown"], 1)

    def test_retry_is_a_new_reservation(self):
        self.init()
        http = lambda code: urllib.error.HTTPError(js.API_URL, code, "err", {},
                                                   io.BytesIO(b"{}"))
        seen = []
        t = self.transport(self.budget(), [http(503), http(429), ok_response(100)], seen)
        self.assertEqual(t(BODY)["usage"]["input_tokens"], 100)
        con = sqlite3.connect(self.ledger)
        rows = con.execute("SELECT reservation_id, state FROM entries ORDER BY seq").fetchall()
        con.close()
        ids = list(dict.fromkeys(r[0] for r in rows))
        self.assertEqual(len(ids), 3)
        final = {rid: [s for i, s in rows if i == rid][-1] for rid in ids}
        self.assertEqual([final[i] for i in ids], ["unknown", "unknown", "settled"])
        st = self.budget().status()["day"]
        self.assertAlmostEqual(st["committed_usd"],
                               2 * self.max_charge() + js.cost_usd({"input_tokens": 100}),
                               places=9)

    def test_retry_stops_when_the_next_attempt_does_not_fit(self):
        self.init()
        m = self.max_charge()
        http = urllib.error.HTTPError(js.API_URL, 503, "err", {}, io.BytesIO(b"{}"))
        seen = []
        t = self.transport(self.budget(day_cap_usd=1.5 * m), [http, ok_response()], seen)
        with self.assertRaises(jb.BudgetExhausted):
            t(BODY)
        self.assertEqual(len(seen), 1)

    def test_no_opener_call_without_a_reservation(self):
        with self.assertRaises(TypeError):
            js.UrllibTransport("k", opener=lambda *a, **k: None)
        with self.assertRaises(TypeError):
            js.UrllibTransport("k", budget=None, opener=lambda *a, **k: None)
        seen = []          # no ledger at all: recovery mode, nothing sent
        t = self.transport(self.budget(), [ok_response()], seen)
        with self.assertRaises(jb.BudgetRecovery):
            t(BODY)
        self.assertEqual(seen, [])

    def test_key_and_body_never_in_status_or_ledger(self):
        self.init()
        seen = []
        t = self.transport(self.budget(), [ok_response()], seen)
        t(BODY)
        blob = self.ledger.read_bytes() + self.pin.read_bytes() + \
            json.dumps(self.budget().status()).encode()
        self.assertNotIn(b"sk-test-secret", blob)
        self.assertNotIn(b"answers", blob)

    def test_preflight_is_a_free_get_with_no_reservation(self):
        self.init()
        seen = []
        t = self.transport(self.budget(), [{"models": []}], seen)
        t.preflight()
        self.assertEqual([(r.get_method(), r.full_url) for r in seen], [("GET", js.MODELS_URL)])
        self.assertEqual(self.budget().status()["rows"], 0)


class OpenSendPathScanTest(unittest.TestCase):
    """Every opener / urlopen call in jev_screen sits in UrllibTransport._open,
    which only __call__ (after a reservation) and preflight (GET /models)
    reach; no other pipeline module talks to the Jev API."""

    def test_only_reserved_paths_reach_the_opener(self):
        src = (REPO / "earwitness/pipeline/jev_screen.py").read_text()
        tree = ast.parse(src)
        sites, callers = [], {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in ("opener", "urlopen", "build_opener"):
                    sites.append(fn.name)
                if name == "_open":
                    callers.setdefault(fn.name, 0)
                    callers[fn.name] += 1
        self.assertEqual(sorted(set(sites)), ["_open"])
        self.assertEqual(sorted(callers), ["__call__", "preflight"])
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                    and n.name == "__call__")
        body = ast.get_source_segment(src, call)
        self.assertLess(body.index("reserve("), body.index("self._open("))
        pre = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "preflight" and "self" in [a.arg for a in n.args.args])
        self.assertIn("models_url", ast.get_source_segment(src, pre))

    def test_no_other_module_reaches_the_jev_api(self):
        hits = []
        for p in list((REPO / "earwitness").rglob("*.py")) + list((REPO / "scripts").glob("*.py")):
            if p.name in ("jev_screen.py",):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if "api.typesafe.ai" in text or "jev_screen.API_URL" in text \
                    or "js.API_URL" in text or "._open(" in text and "jev" in p.name:
                hits.append(str(p.relative_to(REPO)))
        self.assertEqual(hits, [])


class OpenChecksTest(BudgetBase):
    def assertRecovery(self, why):
        seen = []
        t = self.transport(self.budget(), [ok_response()], seen)
        with self.assertRaises(jb.BudgetRecovery) as cm:
            t(BODY)
        self.assertEqual(cm.exception.code, "JEV_BUDGET_RECOVERY")
        self.assertEqual(cm.exception.reason, why)
        self.assertEqual(seen, [])
        self.assertEqual(self.budget().status()["mode"], "recovery")

    def test_missing_ledger(self):
        self.init()
        self.ledger.unlink()
        self.assertRecovery("LEDGER_MISSING")
        self.assertFalse(self.ledger.exists())     # opening never creates it

    def test_missing_or_unreadable_pin(self):
        self.init()
        self.pin.write_text("{not json")
        self.assertRecovery("PIN_UNREADABLE")
        self.pin.unlink()
        self.assertRecovery("PIN_MISSING")

    def test_valid_older_ledger_restored(self):
        self.init()
        b = self.budget()
        b.settle(self.reserve(b), 0.0)
        older = self.tmp / "older.sqlite3"
        shutil.copy(self.ledger, older)
        b.settle(self.reserve(b), 0.0)
        shutil.copy(older, self.ledger)             # a backup put back: chain is valid
        self.assertRecovery("LEDGER_BEHIND_PIN")

    def test_other_ledger_with_the_same_length(self):
        self.init()
        b = self.budget()
        self.reserve(b)
        other = self.tmp / "other"
        jb.init(other / "l.sqlite3", other / "p.pin", first_activation=True, clock=self.clock)
        b2 = jb.JevBudget(other / "l.sqlite3", other / "p.pin", clock=self.clock)
        self.reserve(b2, episode="ep_other")
        shutil.copy(other / "l.sqlite3", self.ledger)
        self.assertRecovery("PIN_MISMATCH")

    def test_broken_chain(self):
        self.init()
        b = self.budget()
        b.settle(self.reserve(b), 0.0)
        self.reserve(b)
        con = sqlite3.connect(self.ledger)
        con.execute("DROP TRIGGER entries_no_update")
        con.execute("UPDATE entries SET max_nusd=1 WHERE seq=1")
        con.commit()
        con.close()
        self.assertRecovery("CHAIN_BROKEN")

    def test_future_dated_row(self):
        self.init()
        self.now[0] = la(2026, 10, 22, 12, 0, 0)
        self.reserve(self.budget())
        self.now[0] = la(2026, 10, 22, 11, 0, 0)    # the clock went back
        self.assertRecovery("FUTURE_ROW")


class RecoverTest(BudgetBase):
    def test_init_refuses_when_a_ledger_or_pin_exists(self):
        self.init()
        with self.assertRaises(jb.BudgetError):
            self.init()
        self.ledger.unlink()
        with self.assertRaises(jb.BudgetError):     # the pin alone also blocks init
            self.init()

    def test_recover_marks_day_and_month_exhausted_until_month_end(self):
        self.init()
        b = self.budget()
        self.reserve(b)
        old_bytes = self.ledger.read_bytes()
        self.pin.unlink()                          # the pin was lost
        self.assertEqual(b.status()["mode"], "recovery")
        rep =jb.recover(self.ledger, self.pin, "restored from backup", clock=self.clock)
        moved = Path(rep["moved_aside"]["ledger"])
        self.assertTrue(moved.exists())
        self.assertEqual(moved.read_bytes(), old_bytes)  # never deleted
        st = self.budget().status()
        self.assertEqual((st["mode"], st["day"]["exhausted"], st["month"]["exhausted"]),
                         ("normal", True, True))
        for day in ((2026, 10, 21, 13), (2026, 10, 30, 9), (2026, 10, 31, 23, 59)):
            self.now[0] = la(*day)
            with self.assertRaises(jb.BudgetExhausted) as cm:
                self.reserve(self.budget())
            self.assertEqual(cm.exception.code, "JEV_AT_CAP")
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(self.budget())
        con = sqlite3.connect(self.ledger)
        first = con.execute("SELECT state, la_day, la_month, note FROM entries"
                            " ORDER BY seq LIMIT 2").fetchall()
        con.close()
        self.assertEqual([r[0] for r in first], ["exhausted", "exhausted"])
        self.assertIn("restored from backup", first[0][3])

    def test_recover_without_any_old_ledger(self):
        rep = jb.recover(self.ledger, self.pin, "first install lost", clock=self.clock)
        self.assertIsNone(rep["moved_aside"].get("ledger"))
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget())


class PodNamingBudgetTest(BudgetBase):
    def test_refused_reservation_stops_the_pod_pass(self):
        seen = []
        t = self.transport(self.budget(), [ok_response()], seen)  # no ledger: recovery
        ask = pod_naming.CachedAsk(self.tmp / "corpus", t)
        body = pod_naming.build_request("ep_1", [(0.0, 30.0)], ["text"], ["Acme"])
        with self.assertRaises(pod_naming.PodJevBudget) as cm:
            ask("ep_1", body)
        self.assertIn("JEV_BUDGET_RECOVERY", str(cm.exception))
        self.assertEqual(seen, [])



class ReviewR1Test(BudgetBase):
    """Code review r1: clock read inside the lock, settle failures and
    bound overruns reported, init and recover guards."""

    def test_clock_moves_on_while_workers_contend(self):
        self.init()
        mu, tick = threading.Lock(), [la(2026, 10, 21, 12)]

        def clock():                 # every read is later than the one before
            with mu:
                tick[0] += 0.001
                return tick[0]
        errors, barrier = [], threading.Barrier(4)

        def worker():
            b = jb.JevBudget(self.ledger, self.pin, clock=clock)
            barrier.wait()
            for _ in range(10):
                try:
                    b.settle(self.reserve(b), 0.000001)
                except Exception as exc:     # FUTURE_ROW was the failure
                    errors.append(repr(exc))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        st = jb.JevBudget(self.ledger, self.pin, clock=clock).status()
        self.assertEqual((st["mode"], st["day"]["settled"], st["day"]["reserved"]),
                         ("normal", 40, 0))

    def test_settle_failure_is_reported_and_stays_at_max(self):
        self.init()
        alerts = []

        class Failing(jb.JevBudget):
            def settle(self, res, actual_usd):
                raise sqlite3.OperationalError("disk I/O error")
        b = Failing(self.ledger, self.pin, clock=self.clock)
        t = js.UrllibTransport("k", budget=b, opener=script_opener([ok_response()], []),
                               sleep=lambda s: None, alert=lambda c, m: alerts.append((c, m)))
        t(BODY)
        self.assertEqual([c for c, _ in alerts], ["JEV_BUDGET_SETTLE_FAILED"])
        st = self.budget().status()["day"]
        self.assertEqual(st["reserved"], 1)
        self.assertAlmostEqual(st["committed_usd"], self.max_charge(), places=9)

    def test_actual_over_the_bound_is_recorded_and_closes_the_day(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        self.assertEqual(b.settle(r, r.max_usd * 2), "JEV_BOUND_EXCEEDED")
        st = self.budget().status()
        self.assertAlmostEqual(st["day"]["committed_usd"], r.max_usd * 2, places=9)
        self.assertTrue(st["day"]["exhausted"] and st["month"]["exhausted"])
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget())
        self.now[0] = la(2026, 10, 25, 9)          # the rest of the month stays closed
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget())

    def test_bound_overrun_across_midnight_closes_the_reservations_day_and_month(self):
        self.init()
        self.now[0] = la(2026, 10, 31, 23, 59, 59)
        r = self.reserve(self.budget())
        self.now[0] = la(2026, 11, 1, 0, 0, 1)     # the response lands 2 s later
        self.assertEqual(self.budget().settle(r, r.max_usd * 2), "JEV_BOUND_EXCEEDED")
        con = sqlite3.connect(self.ledger)
        marks = con.execute("SELECT scope, la_day, la_month, note FROM entries"
                            " WHERE state='exhausted' ORDER BY scope").fetchall()
        con.close()
        self.assertEqual(sorted(m[:3] for m in marks), [
            ("day", "2026-10-31", "2026-10"), ("day", "2026-11-01", "2026-11"),
            ("month", "2026-10-31", "2026-10"), ("month", "2026-11-01", "2026-11")])
        self.assertTrue(all("JEV_BOUND_EXCEEDED" in m[3] for m in marks))
        st = self.budget().status()                 # the settle-time month closes too
        self.assertTrue(st["day"]["exhausted"] and st["month"]["exhausted"])
        self.now[0] = la(2026, 11, 20, 9)
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget())
        self.now[0] = la(2026, 12, 1, 0, 0, 1)
        self.reserve(self.budget())

    def test_same_period_overrun_writes_one_day_and_one_month_mark(self):
        self.init()
        r = self.reserve(self.budget())
        self.budget().settle(r, r.max_usd * 2)
        con = sqlite3.connect(self.ledger)
        n = con.execute("SELECT COUNT(*) FROM entries WHERE state='exhausted'").fetchone()[0]
        con.close()
        self.assertEqual(n, 2)

    def test_transport_alerts_when_a_response_exceeds_its_bound(self):
        self.init()
        alerts = []
        big = ok_response(tokens=BODY_BYTES * 100)  # more tokens than bytes
        t = js.UrllibTransport("k", budget=self.budget(), opener=script_opener([big], []),
                               sleep=lambda s: None, alert=lambda c, m: alerts.append((c, m)))
        t(BODY)
        self.assertEqual([c for c, _ in alerts], ["JEV_BOUND_EXCEEDED"])
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(self.budget())

    def test_init_needs_first_activation_and_no_replaced_copies(self):
        with self.assertRaises(jb.BudgetError):
            jb.init(self.ledger, self.pin, clock=self.clock)
        self.assertFalse(self.ledger.exists() or self.pin.exists())
        for side in (self.ledger, self.pin):
            side.parent.mkdir(parents=True, exist_ok=True)
            old = side.with_name(side.name + ".replaced-20261021T000000Z")
            old.write_text("x")
            with self.assertRaises(jb.BudgetError) as cm:
                jb.init(self.ledger, self.pin, first_activation=True, clock=self.clock)
            self.assertIn("replaced", str(cm.exception))
            old.unlink()
        jb.init(self.ledger, self.pin, first_activation=True, clock=self.clock)

    def test_recover_refuses_while_a_worker_holds_the_lock(self):
        import fcntl
        self.init()
        lock = self.pin.with_name(f".{self.pin.name}.lock")
        with open(lock, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            with self.assertRaises(jb.BudgetError) as cm:
                jb.recover(self.ledger, self.pin, "x", clock=self.clock, lock_timeout_s=0.2)
            self.assertIn("stop the jev workers", str(cm.exception))
        self.assertTrue(self.ledger.exists())       # nothing moved aside
        self.assertEqual(list(self.ledger.parent.glob("*.replaced-*")), [])

    def test_recover_reports_a_hot_journal_it_moves(self):
        self.init()
        journal = self.ledger.with_name(self.ledger.name + "-journal")
        journal.write_bytes(b"hot")
        rep = jb.recover(self.ledger, self.pin, "x", clock=self.clock)
        self.assertEqual(len(rep["journals_moved"]), 1)
        self.assertTrue(Path(rep["journals_moved"][0]).exists())


class CodePrefixTest(BudgetBase):
    """code_prefix renames every refusal code; classes stay the same."""

    def test_default_prefix_keeps_the_jev_codes(self):
        self.init()
        b = self.budget(day_cap_usd=self.max_charge())
        self.reserve(b)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.code, "JEV_AT_CAP")
        self.assertTrue(str(cm.exception).startswith("JEV_AT_CAP"))
        self.assertIsNone(cm.exception.reason)   # corpus.py prints `reason or 'spend cap reached'`

    def test_meta_prefix_on_the_cap_refusal(self):
        self.init()
        b = self.budget(day_cap_usd=self.max_charge(), code_prefix="META")
        self.reserve(b)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertIsInstance(cm.exception, jb.JevBudgetRefused)
        self.assertEqual(cm.exception.code, "META_AT_CAP")
        self.assertTrue(str(cm.exception).startswith("META_AT_CAP"))

    def test_meta_prefix_on_recovery(self):
        b = self.budget(code_prefix="META")          # no ledger: recovery mode
        with self.assertRaises(jb.BudgetRecovery) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.code, "META_BUDGET_RECOVERY")
        self.assertTrue(str(cm.exception).startswith("META_BUDGET_RECOVERY"))
        self.assertEqual(b.status()["code"], "META_BUDGET_RECOVERY")
        self.assertEqual(self.budget().status()["code"], "JEV_BUDGET_RECOVERY")

    def test_meta_prefix_on_the_bound_overrun(self):
        self.init()
        b = self.budget(code_prefix="META")
        r = self.reserve(b)
        self.assertEqual(b.settle(r, r.max_usd * 2), "META_BOUND_EXCEEDED")

    def test_meta_prefix_on_an_unpriced_request(self):
        self.init()
        b = self.budget(code_prefix="META", pricing=jb.Pricing(None, 0.0, "x", "2026-10-21"))
        with self.assertRaises(jb.NotSendable) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.code, "META_UNPRICED")
        self.assertTrue(str(cm.exception).startswith("META_UNPRICED"))

    def test_jev_still_appends_a_second_close(self):
        # The close-once rule is Ask's (AskLedger); Jev's ledger is unchanged.
        self.init()
        b = self.budget()
        r = self.reserve(b)
        b.mark_unknown(r)
        b.settle(r, 0.0)
        con = sqlite3.connect(self.ledger)
        states = [x[0] for x in con.execute("SELECT state FROM entries ORDER BY seq")]
        con.close()
        self.assertEqual(states, ["reserved", "unknown", "settled"])

    def test_default_prefix_on_the_bound_overrun(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        self.assertEqual(b.settle(r, r.max_usd * 2), "JEV_BOUND_EXCEEDED")


class RefusalReasonTest(BudgetBase):
    """BudgetExhausted.cap says which cap refused (reason stays None, as Jev
    prints it); the order is
    MONTH_CAP, TOTAL_CAP, DAY_CAP."""

    def refusal(self, b):
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        return cm.exception.cap

    def test_day_cap(self):
        self.init()
        b = self.budget(day_cap_usd=self.max_charge())
        self.reserve(b)
        self.assertEqual(self.refusal(b), "DAY_CAP")

    def test_month_cap(self):
        self.init()
        b = self.budget(month_cap_usd=self.max_charge())
        self.reserve(b)
        self.assertEqual(self.refusal(b), "MONTH_CAP")

    def test_total_cap(self):
        self.init()
        b = self.budget(total_cap_usd=self.max_charge())
        self.reserve(b)
        self.assertEqual(self.refusal(b), "TOTAL_CAP")

    def test_month_beats_total_and_day(self):
        self.init()
        m = self.max_charge()
        b = self.budget(day_cap_usd=m, month_cap_usd=m, total_cap_usd=m)
        self.reserve(b)
        self.assertEqual(self.refusal(b), "MONTH_CAP")

    def test_month_beats_total(self):
        self.init()
        m = self.max_charge()
        b = self.budget(month_cap_usd=m, total_cap_usd=m)
        self.reserve(b)
        self.assertEqual(self.refusal(b), "MONTH_CAP")

    def test_total_beats_day(self):
        self.init()
        m = self.max_charge()
        b = self.budget(day_cap_usd=m, total_cap_usd=m)
        self.reserve(b)
        self.assertEqual(self.refusal(b), "TOTAL_CAP")

    def test_exhausted_marks_give_day_and_month_reasons(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        b.settle(r, r.max_usd * 2)                  # marks day and month
        self.assertEqual(self.refusal(self.budget()), "MONTH_CAP")


class TotalCapTest(BudgetBase):
    def test_lifetime_cap_is_inclusive(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=2 * m)
        self.reserve(b)
        self.reserve(b)                              # exactly at the cap: fits
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")
        self.assertEqual(cm.exception.code, "JEV_AT_CAP")

    def test_previous_months_count(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=2 * m)
        self.reserve(b)                              # October, left open
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(b)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")

    def test_open_reservations_of_an_earlier_month_count_in_a_later_month(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=2 * m)
        self.reserve(b)
        self.reserve(b)
        self.now[0] = la(2026, 12, 3, 9)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")

    def test_unknown_rows_count_at_their_maximum(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=2 * m)
        b.mark_unknown(self.reserve(b))
        self.reserve(b)
        self.now[0] = la(2026, 11, 2, 9)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(b)
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")

    def test_a_settled_actual_replaces_the_maximum_in_the_lifetime_sum(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=2 * m)
        b.settle(self.reserve(b), 0.0)
        self.reserve(b)
        self.reserve(b)                              # 0 + m + m: at the cap
        with self.assertRaises(jb.BudgetExhausted):
            self.reserve(b)

    def test_a_settled_actual_counts_in_the_lifetime_sum(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=m + 0.0000005)
        b.settle(self.reserve(b), 0.000001)
        with self.assertRaises(jb.BudgetExhausted) as cm:    # 1e-6 + m > cap
            self.reserve(b)
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")

    def test_status_has_a_total_block_when_a_cap_is_set(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=100.0)
        b.settle(self.reserve(b), 0.000001)
        b.mark_unknown(self.reserve(b))
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(b)
        st = b.status()
        self.assertEqual(st["caps_usd"]["total"], 100.0)
        total = st["total"]
        self.assertEqual((total["settled"], total["reserved"], total["unknown"],
                          total["exhausted"]), (1, 1, 1, False))
        self.assertAlmostEqual(total["committed_usd"], 0.000001 + 2 * m, places=9)

    def test_status_without_a_cap_has_no_total(self):
        self.init()
        st = self.budget().status()
        self.assertNotIn("total", st)
        self.assertEqual(st["caps_usd"], {"day": 1.0, "month": 15.0})

    def test_overrun_marks_the_lifetime_cap_reached(self):
        self.init()
        b = self.budget(total_cap_usd=100.0)
        r = self.reserve(b)
        self.assertEqual(b.settle(r, r.max_usd * 2), "JEV_BOUND_EXCEEDED")
        self.assertTrue(b.status()["total"]["exhausted"])
        self.now[0] = la(2026, 10, 25, 9)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(self.budget(total_cap_usd=100.0))
        self.assertEqual(cm.exception.cap, "MONTH_CAP")
        for when in (la(2026, 11, 1, 0, 0, 1), la(2026, 12, 1, 0, 0, 1), la(2027, 3, 1, 9)):
            self.now[0] = when
            with self.assertRaises(jb.BudgetExhausted) as cm:
                self.reserve(self.budget(total_cap_usd=100.0))
            self.assertEqual(cm.exception.cap, "TOTAL_CAP")

    def test_overrun_without_a_total_cap_reserves_again_next_month(self):
        self.init()
        b = self.budget()
        r = self.reserve(b)
        b.settle(r, r.max_usd * 2)
        self.assertNotIn("total", b.status())
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(self.budget())

    def test_a_budget_without_a_total_cap_ignores_the_mark(self):
        self.init()
        b = self.budget(total_cap_usd=100.0)
        r = self.reserve(b)
        b.settle(r, r.max_usd * 2)
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(self.budget())


class RecoverMarkTotalTest(BudgetBase):
    """recover always writes the lifetime mark, so the generic recover
    command cannot reopen a ledger with a lifetime cap (review r1 item 1)."""

    def test_recover_closes_every_later_month(self):
        jb.recover(self.ledger, self.pin, "restored", clock=self.clock)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(self.budget(total_cap_usd=100.0))
        self.assertEqual(cm.exception.cap, "MONTH_CAP")
        for when in (la(2026, 11, 1, 0, 0, 1), la(2026, 12, 15, 9), la(2027, 6, 1, 9)):
            self.now[0] = when
            with self.assertRaises(jb.BudgetExhausted) as cm:
                self.reserve(self.budget(total_cap_usd=100.0))
            self.assertEqual(cm.exception.cap, "TOTAL_CAP")
        self.assertTrue(self.budget(total_cap_usd=100.0).status()["total"]["exhausted"])

    def test_a_budget_without_a_total_cap_ignores_the_mark(self):
        jb.recover(self.ledger, self.pin, "restored", clock=self.clock)
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        self.reserve(self.budget())

    def test_recover_over_a_ledger_at_its_lifetime_cap_stays_closed(self):
        self.init()
        m = self.max_charge()
        b = self.budget(total_cap_usd=m)
        self.reserve(b)
        self.pin.unlink()
        jb.recover(self.ledger, self.pin, "restored", clock=self.clock)
        self.now[0] = la(2026, 11, 1, 0, 0, 1)
        with self.assertRaises(jb.BudgetExhausted) as cm:
            self.reserve(self.budget(total_cap_usd=m))
        self.assertEqual(cm.exception.cap, "TOTAL_CAP")


if __name__ == "__main__":
    unittest.main()
