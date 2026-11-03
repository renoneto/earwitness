"""L7: billing scaffolding with fixtures only (no provider, no network).

Entitlement gating of customer operations (off by default, one explicit
UNAPPROVED list) and the one-trial-per-history reservation mechanism.
The policy switches (release after abandon, how cancelled/expired trials
count) are UNAPPROVED and default to the strictest reading.
"""
from __future__ import annotations

import inspect
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from earwitness.app import access as _access
from earwitness.app import asgi as _asgi_mod
from earwitness.app import health as _health
from earwitness.app import billing as _billing
from earwitness.app import services as _services
from tests.test_backend_authority import HostedReaderHttpTest

NOW = 1_800_000_000


def _mem():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    _billing.ensure_billing_schema(con)
    return con


def _cfg(**over):
    base = {"secret_key": "sk_fixture", "webhook_secret": "whsec_fixture",
            "success_url": "/billing/success", "cancel_url": "/billing/cancel",
            "prices": {"price_pro": {"amount_cents": 100, "trial_days": 14}},
            "livemode_required": False,
            # The trial and checkout tests drive the in-app checkout route,
            # which is off by default (BY1, design 10.3).
            "inapp_checkout": True}
    base.update(over)
    return _billing.BillingConfig(**base)


def _resource(sub="sub_1", customer="cus_a", status="trialing", workspace="acme",
              trial_end=NOW + 14 * 86400, period=NOW + 14 * 86400, price="price_pro"):
    return {"id": sub, "customer": customer, "status": status,
            "price_id": price, "current_period_end": period,
            "trial_end": trial_end, "cancel_at_period_end": False,
            "metadata_workspace": workspace}


def _event(eid, kind="customer.subscription.updated", sub="sub_1", customer="cus_a"):
    return {"id": eid, "type": kind, "livemode": False,
            "data": {"object": {"id": sub, "customer": customer,
                                "subscription": sub}}}


def _apply(con, event, resource, *, workspace="acme", cfg=None, now=NOW):
    fix = _billing.FixtureSubscriptionProtocol(
        resources={event["data"]["object"]["subscription"]: resource})
    out = _billing.apply_event(con, workspace, event, now=now,
                               config=cfg or _cfg(), provider=fix)
    con.commit()
    return out


class ConfigTest(unittest.TestCase):
    def test_defaults_are_off_and_strict(self):
        cfg = _billing.BillingConfig()
        self.assertFalse(cfg.entitlement_required)
        self.assertFalse(cfg.trial_release_abandoned)
        self.assertTrue(cfg.trial_cancelled_counts)
        self.assertTrue(cfg.trial_expired_counts)
        self.assertTrue(cfg.prior_subscription_blocks_trial)
        self.assertEqual(cfg.ungated_operations, _billing.UNAPPROVED_UNGATED_OPERATIONS)
        self.assertEqual(_billing.config_from_env({}).entitlement_required, False)

    def test_env_switches(self):
        cfg = _billing.config_from_env({
            "EARWITNESS_BILLING_ENTITLEMENT_REQUIRED": "1",
            "EARWITNESS_BILLING_UNGATED_OPERATIONS": "customer_overview, billing_portal",
            "EARWITNESS_BILLING_TRIAL_RELEASE_ABANDONED": "1",
            "EARWITNESS_BILLING_TRIAL_CANCELLED_COUNTS": "0",
            "EARWITNESS_BILLING_TRIAL_EXPIRED_COUNTS": "false",
            "EARWITNESS_BILLING_PRIOR_SUBSCRIPTION_BLOCKS_TRIAL": "no"})
        self.assertTrue(cfg.entitlement_required)
        self.assertEqual(cfg.ungated_operations, ("customer_overview", "billing_portal"))
        self.assertTrue(cfg.trial_release_abandoned)
        self.assertFalse(cfg.trial_cancelled_counts)
        self.assertFalse(cfg.trial_expired_counts)
        self.assertFalse(cfg.prior_subscription_blocks_trial)

    def test_empty_exempt_list_env_falls_back_to_default_list(self):
        cfg = _billing.config_from_env({
            "EARWITNESS_BILLING_ENTITLEMENT_REQUIRED": "1",
            "EARWITNESS_BILLING_UNGATED_OPERATIONS": " , "})
        self.assertEqual(cfg.ungated_operations, _billing.UNAPPROVED_UNGATED_OPERATIONS)

    def test_unknown_exempt_name_is_refused_at_config_load_naming_it(self):
        with self.assertRaises(ValueError) as caught:
            _billing.config_from_env({
                "EARWITNESS_BILLING_UNGATED_OPERATIONS": "customer_overview, custmer_search"})
        self.assertIn("custmer_search", str(caught.exception))
        self.assertIn("EARWITNESS_BILLING_UNGATED_OPERATIONS", str(caught.exception))

    def test_removed_gated_list_env_is_refused_not_silently_ignored(self):
        with self.assertRaises(ValueError) as caught:
            _billing.config_from_env({
                "EARWITNESS_BILLING_GATED_OPERATIONS": "customer_search"})
        self.assertIn("EARWITNESS_BILLING_GATED_OPERATIONS", str(caught.exception))
        self.assertIn("EARWITNESS_BILLING_UNGATED_OPERATIONS", str(caught.exception))

    def test_default_exempt_list_is_billing_and_overview_only(self):
        self.assertEqual(set(_billing.UNAPPROVED_UNGATED_OPERATIONS), {
            "billing_checkout", "billing_portal", "customer_overview",
            "customer_reader_status"})
        self.assertLessEqual(set(_billing.UNAPPROVED_UNGATED_OPERATIONS),
                             set(_billing.KNOWN_OPERATIONS))

    def test_share_titles_can_never_be_exempted_and_always_check(self):
        # collab_share_titles is not a known operation on purpose (documented in
        # docs/hosting/billing.md): naming it is refused, and it is always gated.
        self.assertNotIn("collab_share_titles", _billing.KNOWN_OPERATIONS)
        with self.assertRaises(ValueError) as caught:
            _billing.config_from_env({
                "EARWITNESS_BILLING_UNGATED_OPERATIONS": "collab_share_titles"})
        self.assertIn("collab_share_titles", str(caught.exception))
        with self.assertRaises(_billing.EntitlementRequired):
            _billing.require_entitlement(
                _mem(), "acme", "collab_share_titles",
                _cfg(entitlement_required=True), now=NOW)

    def test_export_download_has_its_own_gated_operation_name(self):
        self.assertIn("customer_export_download", _billing.KNOWN_OPERATIONS)
        self.assertNotIn("customer_export_download", _billing.UNAPPROVED_UNGATED_OPERATIONS)
        seen = []
        with mock.patch.object(_services, "_require_member",
                               side_effect=lambda c, con, e, op: seen.append(op)), \
                mock.patch.object(_services._db, "transaction"), \
                mock.patch("earwitness.app.exports.load_export_bytes", return_value={}):
            _services.customer_export_bytes(mock.MagicMock(), None, {}, 1)
        self.assertEqual(seen, ["customer_export_download"])

    def test_agent_tools_and_mcp_are_known_operations(self):
        from earwitness import agents as _agents
        known = set(_billing.KNOWN_OPERATIONS)
        self.assertLessEqual({spec["name"] for spec in _agents.TOOL_DEFS}, known)
        self.assertIn("mcp", known)


class ServiceHelperOffPathTest(unittest.TestCase):
    def test_off_never_reads_the_entry_or_the_connection(self):
        with mock.patch.object(_services, "_billing_config", return_value=_cfg()):
            self.assertIsNone(_services._require_entitlement(None, None, "customer_search"))


class RequireEntitlementTest(unittest.TestCase):
    def setUp(self):
        self.con = _mem()

    def tearDown(self):
        self.con.close()

    def _entitle(self, status="active", period=NOW + 3600, **cols):
        self.con.execute(
            "INSERT OR REPLACE INTO billing_entitlement (workspace_id, status,"
            " price_id, trial_end, current_period_end, past_due_since,"
            " updated_at) VALUES ('acme', ?, 'price_pro', ?, ?, ?, 'x')",
            (status, cols.get("trial_end", 0), period, cols.get("past_due_since", 0)))
        self.con.commit()

    def test_off_never_refuses(self):
        cfg = _cfg()
        self.assertIsNone(_billing.require_entitlement(
            self.con, "acme", "customer_search", cfg, now=NOW))

    def test_on_refuses_without_billing_record_with_named_refusal(self):
        cfg = _cfg(entitlement_required=True)
        with self.assertRaises(_billing.EntitlementRequired) as caught:
            _billing.require_entitlement(self.con, "acme", "customer_search",
                                         cfg, now=NOW)
        exc = caught.exception
        self.assertIsInstance(exc, _access.AuthorizationError)
        self.assertEqual(exc.status, 402)
        self.assertIn("entitlement required", str(exc))
        self.assertIn("customer_search", str(exc))

    def test_on_allows_active_and_grace_past_due(self):
        # BY1 (design 8.6): trialing is refused; see test_billing_entitlement_table.
        cfg = _cfg(entitlement_required=True)
        self._entitle("active")
        self.assertTrue(_billing.require_entitlement(
            self.con, "acme", "customer_search", cfg, now=NOW)["allowed"])
        self._entitle("past_due", past_due_since=NOW - 10)
        self.assertTrue(_billing.require_entitlement(
            self.con, "acme", "customer_search", cfg, now=NOW)["allowed"])

    def test_on_refuses_expired_trial_period_end_and_exhausted_grace(self):
        cfg = _cfg(entitlement_required=True)
        for status, kwargs in (
                ("trialing", {"trial_end": NOW - 1, "period": NOW + 99}),
                ("active", {"period": NOW - 1}),
                ("past_due", {"past_due_since": NOW - cfg.past_due_grace_s - 5}),
                ("canceled", {"period": NOW - 1}),
                ("canceled", {"period": NOW + 99}),
                ("unpaid", {"period": NOW + 99}),
                ("trialing", {"trial_end": NOW + 100, "period": NOW + 100}),
                ("unknown", {})):
            self._entitle(status, **kwargs)
            with self.assertRaises(_billing.EntitlementRequired, msg=status):
                _billing.require_entitlement(self.con, "acme", "customer_search",
                                             cfg, now=NOW)

    def test_exempt_operation_passes_even_without_entitlement(self):
        cfg = _cfg(entitlement_required=True, ungated_operations=("customer_search",))
        self.assertIsNone(_billing.require_entitlement(
            self.con, "acme", "customer_search", cfg, now=NOW))
        with self.assertRaises(_billing.EntitlementRequired):
            _billing.require_entitlement(self.con, "acme", "customer_summary",
                                         cfg, now=NOW)

    def test_unlisted_and_unknown_operations_are_gated_fail_closed(self):
        cfg = _cfg(entitlement_required=True)
        for name in ("customer_recording", "customer_brand_detail", "brand_new_operation"):
            with self.assertRaises(_billing.EntitlementRequired, msg=name):
                _billing.require_entitlement(self.con, "acme", name, cfg, now=NOW)
        for name in _billing.UNAPPROVED_UNGATED_OPERATIONS:
            self.assertIsNone(_billing.require_entitlement(
                self.con, "acme", name, cfg, now=NOW))

    def test_missing_billing_schema_fails_closed_when_on(self):
        bare = sqlite3.connect(":memory:")
        bare.row_factory = sqlite3.Row
        cfg = _cfg(entitlement_required=True)
        with self.assertRaises(_billing.EntitlementRequired):
            _billing.require_entitlement(bare, "acme", "customer_search", cfg, now=NOW)
        # off: the missing schema is not even read
        self.assertIsNone(_billing.require_entitlement(
            bare, "acme", "customer_search", _cfg(), now=NOW))
        bare.close()

    def test_other_workspace_entitlement_does_not_carry_over(self):
        self._entitle("active")
        cfg = _cfg(entitlement_required=True)
        with self.assertRaises(_billing.EntitlementRequired):
            _billing.require_entitlement(self.con, "other", "customer_search",
                                         cfg, now=NOW)


def _service_operations():
    """Every customer-facing service function and the operation names it
    passes to the shared membership and role helpers."""
    import ast
    tree = ast.parse(inspect.getsource(_services))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (
                node.name.startswith("customer_") or node.name.startswith("shared_")):
            found[node.name] = node
    return found


NOT_OPERATIONS = {"customer_view_context"}


def _dispatch_customer_ast():
    import ast
    tree = ast.parse(inspect.getsource(_asgi_mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_customer":
            return node
    raise AssertionError("_dispatch_customer not found")


def _route_operations() -> set:
    """Operation names the customer routes in asgi.py pass to the gated
    membership helper themselves (routes that do not call a service)."""
    import ast
    ops = set()
    for node in ast.walk(_dispatch_customer_ast()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "_require_member":
            ops.add(node.args[3].value)
    return ops


_GATE_ATTRS = ("_require_member", "_require_entitlement")
_RELEASE_NAMES = ("ok", "send")


def _gate_call(call) -> bool:
    """A gate is a customer service function (not customer_view_context,
    which only describes the workspace and checks nothing) or the gated
    membership and entitlement helpers."""
    import ast
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "off_loop" and isinstance(func, ast.Name) and call.args:
        # PERF-HOL: off_loop(service, ...) awaits service(ctx, ro, entry,
        # ...) in the read pool; it gates as a direct call would.
        func = call.args[0]
        name = func.attr if isinstance(func, ast.Attribute) else ""
    return name in _GATE_ATTRS or (
        name.startswith("customer_") and name not in NOT_OPERATIONS
        and isinstance(func, ast.Attribute))


def _exempt_names(node) -> set:
    """ids of the `send`/`ok` names inside a plain not-found reply."""
    import ast
    out = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and any(
                isinstance(i, ast.Call) and getattr(i.func, "id", "") == "_not_found"
                for i in ast.walk(call)):
            out.update(id(n) for n in ast.walk(call) if isinstance(n, ast.Name))
    return out


def _ends_in_exit(handler) -> bool:
    import ast
    return bool(handler.body) and isinstance(handler.body[-1], (ast.Raise, ast.Return))


def _ungated_releases(source: str) -> list:
    """Line numbers of every release in _dispatch_customer that no gated
    call certainly precedes. Every ast.If is scanned, nested ones too.

    A gate counts only where it surely runs: not inside a for/while body,
    not in a try body whose handlers can fall through, not behind a
    short-circuit or in a conditional expression's branch, and never for a
    sibling branch. A release is any use of the names `send` or `ok` (a
    call, an alias, an argument to a helper) outside a plain not-found
    reply."""
    import ast
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dispatch_customer")
    exempt = _exempt_names(fn)
    bad = []

    def ev(node, gated):
        """Evaluate an expression; returns whether a gate surely ran."""
        if node is None or isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return gated
        if isinstance(node, ast.BoolOp):
            first = ev(node.values[0], gated)
            for rest in node.values[1:]:
                ev(rest, first)
            return first
        if isinstance(node, ast.IfExp):
            test = ev(node.test, gated)
            ev(node.body, test)
            ev(node.orelse, test)
            return test
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            ev_children(node, gated)  # may run zero times: no carry-over
            return gated
        if isinstance(node, ast.Name):
            if node.id in _RELEASE_NAMES and isinstance(node.ctx, ast.Load) \
                    and id(node) not in exempt and not gated:
                bad.append(node.lineno)
            return gated
        if isinstance(node, ast.Call):
            # Arguments run before the call itself: ok(customer_x(...)) is gated.
            for arg in [*node.args, *[k.value for k in node.keywords]]:
                gated = ev(arg, gated)
            gated = ev(node.func, gated)
            return True if _gate_call(node) else gated
        return ev_children(node, gated)

    def ev_children(node, gated):
        for child in ast.iter_child_nodes(node):
            gated = ev(child, gated)
        return gated

    def block(stmts, gated):
        for st in stmts:
            gated = stmt(st, gated)
        return gated

    def stmt(st, gated):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return gated
        if isinstance(st, ast.If):
            inner = ev(st.test, gated)
            block(st.body, inner)
            block(st.orelse, inner)
            return inner
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            inner = ev(st.iter if hasattr(st, "iter") else st.test, gated)
            block(st.body, inner)
            block(st.orelse, inner)
            return inner  # the body may never run
        if isinstance(st, ast.Try):
            after = block(st.body, gated)
            # A handler answers an error the gated call raised (or one raised
            # before it, which the gate's own errors are not): it counts as
            # gated only when the try body holds a gate call.
            attempted = after or any(
                isinstance(n, ast.Call) and _gate_call(n)
                for part in st.body for n in ast.walk(part))
            for handler in st.handlers:
                block(handler.body, attempted)
            after = block(st.orelse, after)
            if not all(_ends_in_exit(h) for h in st.handlers):
                after = gated
            return block(st.finalbody, after)
        if isinstance(st, (ast.With, ast.AsyncWith)):
            for item in st.items:
                gated = ev(item.context_expr, gated)
            return block(st.body, gated)
        return ev_children(st, gated)

    block(fn.body, False)
    return sorted(set(bad))


def _swallowed_gate_mutants(top: str, read: str) -> dict:
    """Shapes where a gate is written but does not run on every path, or the
    response leaves through something the scan did not look at. Each is
    inserted before the first route and reads export bytes."""
    gate = "_services.customer_overview(ctx, con, entry)"
    leak = '        if base == "/leak":\n'
    send_body = ('            await {s}({{"type": "http.response.start", "status": 200,'
                 ' "headers": []}})\n            return\n')

    shapes = {
        "gate inside try with a swallowing handler": leak + (
            "            try:\n                " + gate + "\n"
            "            except Exception:\n                pass\n") + read,
        "gate inside a for loop that never runs": leak + (
            "            for _ in ():\n                " + gate + "\n") + read,
        "short-circuited gate": leak + (
            "            False and " + gate + "\n") + read,
        "send aliased before the response": leak + (
            "            respond = send\n") + send_body.format(s="respond"),
        "response sent through a helper method": leak + (
            "            await self._leak_helper(send, con)\n            return\n"),
        "top-level swallowed gate makes later routes look gated": (
            "        try:\n            " + gate + "\n"
            "        except Exception:\n            pass\n") + leak + read,
        "gate inside a while loop that never runs": leak + (
            "            while False:\n                " + gate + "\n") + read,
        "gate in the dead branch of a conditional expression": leak + (
            "            _unused = 1 if True else " + gate + "\n") + read,
    }
    return {name: (top, code) for name, code in shapes.items()}


def _bypass_mutants(src: str) -> dict:
    """The current asgi source with one wrongly wired route each. Anchors
    are asserted, so a refactor that moves one fails loudly here."""
    def once(old, new, text=src):
        assert text.count(old) == 1, old
        return text.replace(old, new)

    top = '        if method == "GET" and base == "/overview":\n'
    read = ('            await ok({"data": _exports.load_export_bytes(con, 1)})\n'
            '            return\n')
    gate = '            _services._require_member(ctx, con, entry, "customer_report_html")\n'
    sent = ('            await send({"type": "http.response.body", "body": raw})\n'
            '            return\n'
            '        m = re.fullmatch(r"/campaigns/(\\d+)/reports/(\\d+)", base)\n')
    attach_else = ('            else:\n'
                   '                await self._send(send, *_not_found())\n'
                   '            return\n'
                   '        m = re.fullmatch(r"/campaigns/(\\d+)/reports", base)\n')
    moved = once(gate, "")
    return {
        "method in a tuple, direct export read": once(
            top, '        if method in ("GET",) and base == "/leak":\n' + read + top),
        "no method test at all": once(
            top, '        if base == "/leak":\n' + read + top),
        "only customer_view_context before the read": once(
            top, '        if base == "/leak":\n'
                 '            _services.customer_view_context(ctx, entry)\n' + read + top),
        "route nested inside the existing if m branch": once(
            attach_else, '            elif method == "GET":\n'
                         '                await ok({"data": _exports.load_export_bytes(con, 1)})\n'
                         + attach_else),
        **{name: once(anchor, code + anchor)
           for name, (anchor, code) in _swallowed_gate_mutants(top, read).items()},
        "report gate moved after the bytes are sent": once(
            sent, sent.replace("            return\n", gate + "            return\n", 1), moved),
    }


class CustomerRouteWiringTest(unittest.TestCase):
    """A customer route in asgi.py that reads through a module directly,
    without a gated service or the gated membership helper, fails here."""

    def test_no_route_uses_the_bare_membership_check(self):
        import ast
        bare = [n.lineno for n in ast.walk(_dispatch_customer_ast())
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "require_member"]
        self.assertEqual(bare, [], "ctx.require_member bypasses the entitlement gate")

    def test_every_route_branch_goes_through_a_gated_call(self):
        self.assertEqual(_ungated_releases(inspect.getsource(_asgi_mod)), [],
                         "a route releases data before any gated call")

    def test_the_scan_fails_each_known_bypass_shape(self):
        src = inspect.getsource(_asgi_mod)
        self.assertEqual(_ungated_releases(src), [])
        for name, mutated in _bypass_mutants(src).items():
            compile(mutated, name, "exec")  # a mutant is real code, not a typo
            with self.subTest(name):
                self.assertNotEqual(_ungated_releases(mutated), [], name)

    def test_route_operations_are_known(self):
        self.assertLessEqual(_route_operations(), set(_billing.KNOWN_OPERATIONS))
        self.assertLessEqual(
            {"customer_mistake_read", "customer_report_html", "customer_show_artwork",
             "collab_invite_send"}, set(_billing.KNOWN_OPERATIONS))


class ServiceGateWiringTest(unittest.TestCase):
    """The gate is inverted: every customer-facing service function runs
    the entitlement helper unless the exempt list names its operation."""

    def _run_all(self, stub, ctx_for=None):
        seen = {}
        for name in sorted(set(_service_operations()) - NOT_OPERATIONS):
            fn = getattr(_services, name)
            ctx = mock.MagicMock()
            ctx.authority_kind = "share" if name.startswith("shared_") else "member"
            params = list(inspect.signature(fn).parameters.values())
            # *args/**kwargs would hide options from the scan: extend it first
            self.assertFalse([p.name for p in params
                              if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)], name)
            args = [mock.MagicMock() for p in params
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            args[0] = ctx
            args[2] = {"is_owner": True, "row_namespace": "acme"}
            # each keyword-only option (e.g. customer_research_query's rank) also
            # runs flipped and as a mock, so no option value can skip the gate
            variants = [{}]
            for p in params:
                if p.kind is p.KEYWORD_ONLY:
                    flipped = (not p.default) if isinstance(p.default, bool) else None
                    variants += [{p.name: flipped}, {p.name: mock.MagicMock()}]
            seen[name] = (fn, args, variants)
        return seen

    def test_every_customer_function_checks_entitlement_exactly_through_the_helper(self):
        class Stop(Exception):
            pass
        calls = {}
        current = {}

        def stub(con, entry, operation):
            calls.setdefault(current["name"], []).append(operation)
            raise Stop()

        known = set(_billing.KNOWN_OPERATIONS)
        with mock.patch.object(_services, "_require_entitlement", stub), \
                mock.patch.object(_access, "require_customer_mutation", lambda c, o: None):
            for name, (fn, args, variants) in self._run_all(stub).items():
                current["name"] = name
                per_variant = []
                for kwargs in variants:
                    calls[name] = []
                    with self.assertRaises(Stop, msg=(name, kwargs)):
                        fn(*args, **kwargs)
                    self.assertEqual(len(calls[name]), 1, (name, kwargs))
                    self.assertIn(calls[name][0], known, (name, kwargs))
                    per_variant.append(calls[name][0])
                # every option value gates as the same operation
                self.assertEqual(len(set(per_variant)), 1, (name, per_variant))
        self.assertEqual(set(calls), set(_service_operations()) - NOT_OPERATIONS)
        # KNOWN_OPERATIONS stays equal to the code: no stale or missing names
        observed = {ops[0] for ops in calls.values()} | _route_operations()
        listed = {k for k in known if k.startswith(("customer_", "shared_"))}
        self.assertEqual(observed, listed)

    def test_gate_runs_after_membership_and_role_checks(self):
        class Stop(Exception):
            pass

        def stub(con, entry, operation):
            raise Stop()

        denied = _access.AuthorizationError("workspace permission required", 403)
        with mock.patch.object(_services, "_require_entitlement", stub), \
                mock.patch.object(_access, "require_customer_mutation", lambda c, o: None):
            # read: membership check fails first
            ctx = mock.MagicMock()
            ctx.require_member.side_effect = denied
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services.customer_brands(ctx, None, {"row_namespace": "acme"})
            self.assertEqual(caught.exception.status, 403)
            # mutation on a collaborative workspace: role check fails first
            ctx = mock.MagicMock()
            ctx.authority_kind = "member"
            with mock.patch.object(_services._collaboration, "check_permission",
                                   return_value=False):
                with self.assertRaises(_access.AuthorizationError) as caught:
                    _services.customer_save_campaign(
                        ctx, None, {"is_owner": False, "row_namespace": "acme"}, {})
            self.assertEqual(caught.exception.status, 403)
            # share guest: the share-authority check fails first
            ctx = mock.MagicMock()
            ctx.authority_kind = "member"
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services.shared_search(ctx, None, {"row_namespace": "acme"}, {})
            self.assertEqual(caught.exception.status, 403)

    def test_real_gate_refuses_share_guest_reads_when_on(self):
        con = _mem()
        ctx = mock.MagicMock()
        ctx.authority_kind = "share"
        entry = {"row_namespace": "acme"}
        with mock.patch.object(_services, "_billing_config",
                               return_value=_cfg(entitlement_required=True)):
            for fn, extra in ((_services.shared_search, ({},)),
                              (_services.shared_evidence, ("ev1",)),
                              (_services.shared_transcript, ("ep1",))):
                with self.assertRaises(_billing.EntitlementRequired, msg=fn.__name__) as caught:
                    fn(ctx, con, entry, *extra)
                self.assertEqual(caught.exception.status, 402)
        with mock.patch.object(_services, "_billing_config", return_value=_cfg()), \
                mock.patch.object(_services, "search_evidence", return_value={"items": []}):
            self.assertEqual(_services.shared_search(ctx, con, entry, {}), {"items": []})
        con.close()


class _HttpBase(unittest.TestCase):
    """Real HostedApp HTTP through the existing fixture, composed rather
    than inherited so its own tests are not collected twice."""

    def setUp(self):
        self.http = HostedReaderHttpTest()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.db_path = self.http.db_path

    def _call(self, *args, **kwargs):
        return self.http._call(*args, **kwargs)

    def _csrf(self):
        return self.http._csrf()


class GatedRoutesHttpTest(_HttpBase):
    """Real HostedApp HTTP with a fixture billing config. The workspace
    database holds the server-side payment state."""

    def _headers(self):
        return {"authorization": "Bearer " + self.http.reader_token}

    def _config(self, **over):
        return mock.patch.object(_services, "_billing_config",
                                 return_value=_cfg(**over))

    def _set_entitlement(self, status, **cols):
        con = sqlite3.connect(self.db_path)
        try:
            _billing.ensure_billing_schema(con)
            con.execute(
                "INSERT OR REPLACE INTO billing_entitlement (workspace_id, status,"
                " price_id, trial_end, current_period_end, past_due_since,"
                " updated_at) VALUES ('local', ?, 'price_pro', 0, ?, 0, 'x')",
                (status, cols.get("period", int(time.time()) + 3600)))
            con.commit()
        finally:
            con.close()

    QUERY = {"version": 2, "mode": "ads", "q": "", "brands": None, "shows": None,
             "categories": None, "statuses": None, "audio": None,
             "date_status": "all", "from": None, "to": None, "sort": "newest",
             "page": 1, "ids": None}

    def _search(self):
        return self._call("/api/v1/customer/search", method="GET",
                          headers=self._headers(),
                          query="query=" + urllib.parse.quote(json.dumps(self.QUERY)))

    def test_off_by_default_is_unchanged(self):
        with self._config():
            status, body = self._search()
        self.assertEqual(status, 200, body)

    def test_on_without_entitlement_is_refused_not_empty_success(self):
        with self._config(entitlement_required=True):
            status, body = self._search()
        self.assertEqual(status, 402, body)
        self.assertFalse(body.get("ok"))
        self.assertIn("entitlement required", body["error"]["message"])

    def test_on_with_active_entitlement_allows(self):
        self._set_entitlement("active")
        with self._config(entitlement_required=True):
            status, body = self._search()
        self.assertEqual(status, 200, body)

    def test_on_with_lapsed_entitlement_refuses(self):
        self._set_entitlement("active", period=int(time.time()) - 5)
        with self._config(entitlement_required=True):
            status, body = self._search()
        self.assertEqual(status, 402, body)

    def test_only_exempt_operations_are_open(self):
        with self._config(entitlement_required=True,
                          ungated_operations=("customer_search",)):
            self.assertEqual(self._search()[0], 200)
        with self._config(entitlement_required=True):
            self.assertEqual(self._search()[0], 402)

    def test_customer_reads_outside_the_old_list_are_gated(self):
        with self._config(entitlement_required=True):
            for path in ("/api/v1/customer/brands", "/api/v1/customer/shows",
                         "/api/v1/customer/campaigns", "/api/v1/customer/briefs",
                         "/api/v1/customer/alerts", "/api/v1/customer/saved-items"):
                status, body = self._call(path, method="GET", headers=self._headers())
                self.assertIn(status, (402, 404), (path, body))
                self.assertNotEqual(status, 200, path)
            status, body = self._call("/api/v1/customer/brands", method="GET",
                                      headers=self._headers())
            self.assertEqual(status, 402, body)
        with self._config():
            status, body = self._call("/api/v1/customer/brands", method="GET",
                                      headers=self._headers())
            self.assertEqual(status, 200, body)

    BYPASS_ROUTES = (
        "/api/v1/customer/exports/1",
        "/api/v1/customer/campaigns/1/reports/1/html",
        "/api/v1/customer/records/x/mistake",
        "/api/v1/customer/shows/x/artwork",
    )

    def test_direct_module_routes_are_gated_when_on_and_unchanged_when_off(self):
        with self._config():
            baseline = {p: self._call(p, method="GET", headers=self._headers())[0]
                        for p in self.BYPASS_ROUTES}
        for path, status in baseline.items():
            self.assertNotEqual(status, 402, path)
        with self._config(entitlement_required=True):
            for path in self.BYPASS_ROUTES:
                status, body = self._call(path, method="GET", headers=self._headers())
                self.assertEqual(status, 402, (path, body))
                self.assertIn("entitlement required", body["error"]["message"])
        self._set_entitlement("active")
        with self._config(entitlement_required=True):
            for path in self.BYPASS_ROUTES:
                self.assertEqual(
                    self._call(path, method="GET", headers=self._headers())[0],
                    baseline[path], path)

    AGENT_PROBES = (
        ("POST", "/api/v1/evidence/search", {"filter": {"query": ""}, "limit": 1}),
        ("POST", "/api/v1/evidence/summary", {"group_by": "brand", "limit": 5}),
        ("GET", "/api/v1/brands", None),
        ("GET", "/api/v1/shows", None),
        ("POST", "/api/v1/compat/search", {"query": "x"}),
    )

    def _agent(self, method, path, body):
        return self._call(path, method=method, body=body, headers=self._headers())

    def test_agent_api_is_gated_when_on_and_unchanged_when_off(self):
        with self._config():
            baseline = {p: self._agent(m, p, b)[0] for m, p, b in self.AGENT_PROBES}
        for path, status in baseline.items():
            self.assertNotEqual(status, 402, path)
        with self._config(entitlement_required=True):
            for method, path, body in self.AGENT_PROBES:
                status, resp = self._agent(method, path, body)
                self.assertEqual(status, 402, (path, resp))
                self.assertFalse(resp.get("ok"))
                self.assertIn("entitlement required", resp["error"]["message"])
        self._set_entitlement("active")
        with self._config(entitlement_required=True):
            for method, path, body in self.AGENT_PROBES:
                self.assertEqual(self._agent(method, path, body)[0], baseline[path], path)

    def test_every_agent_route_is_gated_or_denied_earlier_never_open(self):
        from earwitness.app import asgi as _asgi
        with self._config(entitlement_required=True):
            for method, route, _tool in _asgi.AGENT_ROUTES:
                path = route.replace("{evidence_id}", "x").replace(
                    "{campaign_id}", "1").replace("{source_version_id}", "x")
                status, resp = self._agent(method, path, {} if method == "POST" else None)
                self.assertIn(status, (402, 403), (path, status, resp))

    def test_mcp_endpoint_is_gated_when_on(self):
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        with self._config(entitlement_required=True):
            status, resp = self._call("/mcp", method="POST", body=rpc,
                                      headers=self._headers())
        self.assertEqual(status, 402, resp)
        with self._config():
            status, _ = self._call("/mcp", method="POST", body=rpc,
                                   headers=self._headers())
        self.assertNotEqual(status, 402)

    def test_billing_and_overview_routes_stay_reachable_when_on(self):
        with self._config(entitlement_required=True):
            status, body = self._call("/api/v1/billing/status", method="GET",
                                      headers=self._headers())
            self.assertEqual(status, 200, body)
            self.assertFalse(body["entitlement"]["allowed"])
            status, body = self._call("/api/v1/customer/overview", method="GET",
                                      headers=self._headers())
            self.assertEqual(status, 200, body)


class TrialReservationTest(unittest.TestCase):
    def setUp(self):
        self.con = _mem()
        self.cfg = _cfg()

    def tearDown(self):
        self.con.close()

    def _reserve(self, ws="acme", customer="", cfg=None, now=NOW):
        out = _billing.reserve_trial(self.con, ws, customer, cfg or self.cfg, now=now)
        self.con.commit()
        return out

    def test_first_reserved_second_refused_with_reason(self):
        first = self._reserve()
        self.assertTrue(first["granted"])
        self.assertTrue(first["reservation_id"])
        second = self._reserve()
        self.assertFalse(second["granted"])
        self.assertEqual(second["reservation_id"], "")
        self.assertIn("trial", second["reason"])
        self.assertFalse(_billing.trial_eligibility(
            self.con, "acme", "", self.cfg, now=NOW)["eligible"])

    def test_reservation_does_not_grant_entitlement(self):
        self._reserve()
        self.assertFalse(_billing.entitlement_status(
            self.con, "acme", self.cfg, now=NOW)["allowed"])

    def test_concurrent_checkouts_reserve_exactly_one_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ws.sqlite3")
            setup = sqlite3.connect(path)
            setup.row_factory = sqlite3.Row
            _billing.ensure_billing_schema(setup)
            setup.commit()
            setup.close()
            barrier = threading.Barrier(8)
            results, errors = [], []

            def worker(index):
                con = sqlite3.connect(path, timeout=30)
                con.row_factory = sqlite3.Row
                try:
                    barrier.wait()
                    out = _billing.reserve_trial(
                        con, "acme", f"cus_{index}", self.cfg, now=NOW)
                    con.commit()
                    results.append(out["granted"])
                except Exception as exc:  # pragma: no cover - reported below
                    errors.append(repr(exc))
                finally:
                    con.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False] * 7 + [True])

    def test_single_database_concurrent_workspaces_sharing_one_customer_reserve_one_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ws.sqlite3")
            setup = sqlite3.connect(path)
            _billing.ensure_billing_schema(setup)
            setup.commit()
            setup.close()
            barrier = threading.Barrier(4)
            results = []

            def worker(index):
                con = sqlite3.connect(path, timeout=30)
                con.row_factory = sqlite3.Row
                barrier.wait()
                out = _billing.reserve_trial(
                    con, f"ws{index}", "cus_shared", self.cfg, now=NOW)
                con.commit()
                results.append(out["granted"])
                con.close()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sorted(results), [False] * 3 + [True])

    def test_replacement_customer_ids_do_not_reset_history(self):
        self.assertTrue(self._reserve(customer="cus_a")["granted"])
        # abandoned retry creates a fresh Stripe customer for the same workspace
        self.assertFalse(self._reserve(customer="cus_b")["granted"])
        # operator rebinds the workspace to a replacement customer
        _billing.bind_billing_customer(self.con, "acme", "cus_b", operator="owner")
        self.con.commit()
        # a different workspace record presenting either customer is the same history
        self.assertFalse(self._reserve(ws="acme2", customer="cus_a")["granted"])
        self.assertFalse(self._reserve(ws="acme3", customer="cus_b")["granted"])
        # an unrelated history still gets its own trial
        self.assertTrue(self._reserve(ws="other", customer="cus_z")["granted"])

    def test_history_is_per_workspace_database_not_across_databases(self):
        """Hosted use is one database per workspace: the same customer in two
        databases is two histories. Documented limit (AA-02), not a goal."""
        other = _mem()
        try:
            self.assertTrue(self._reserve(ws="local", customer="cus_a")["granted"])
            granted = _billing.reserve_trial(other, "local", "cus_a", self.cfg, now=NOW)
            self.assertTrue(granted["granted"])
        finally:
            other.close()

    def test_rebound_customer_keeps_trial_taken_under_old_customer(self):
        _billing.bind_billing_customer(self.con, "acme", "cus_a", operator="owner")
        self.con.commit()
        self.assertTrue(self._reserve(customer="cus_a")["granted"])
        _billing.bind_billing_customer(self.con, "acme", "cus_new", operator="owner")
        self.con.commit()
        self.assertFalse(self._reserve(customer="cus_new")["granted"])
        self.assertFalse(self._reserve(ws="acme9", customer="cus_a")["granted"])

    def test_rebinding_keeps_the_replaced_customer_in_history(self):
        _billing.bind_billing_customer(self.con, "acme", "cus_old", operator="owner")
        _billing.bind_billing_customer(self.con, "acme", "cus_new", operator="owner")
        self.con.commit()
        _apply(self.con, _event("evt_1", customer="cus_new"),
               _resource(customer="cus_new"))
        self.assertFalse(self._reserve(ws="other", customer="cus_old")["granted"])

    def test_delayed_webhook_consumes_the_reservation(self):
        rid = self._reserve(customer="cus_a")["reservation_id"]
        late = NOW + 10 * 86400
        out = _apply(self.con, _event("evt_1"), _resource(), now=late)
        self.assertEqual(out["status"], "reconciled")
        row = self.con.execute("SELECT trial_state, trial_kind, trial_reservation_id"
                               " FROM billing_trials WHERE workspace_id='acme'").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("consumed", "trial", rid))
        self.assertFalse(self._reserve(customer="cus_a", now=late)["granted"])

    def test_trial_started_with_no_prior_reservation_still_counts(self):
        _apply(self.con, _event("evt_1"), _resource())
        self.assertFalse(self._reserve(customer="cus_a")["granted"])

    def test_duplicate_and_reordered_webhooks_count_the_trial_once(self):
        trialing = _resource(status="trialing")
        canceled = _resource(status="canceled", trial_end=NOW + 86400,
                             period=NOW + 86400)
        _apply(self.con, _event("evt_cancel", "customer.subscription.deleted"),
               canceled)
        _apply(self.con, _event("evt_create", "customer.subscription.created"),
               trialing)
        dup = _apply(self.con, _event("evt_create", "customer.subscription.created"),
                     trialing)
        self.assertTrue(dup["duplicate"])
        rows = self.con.execute("SELECT COUNT(*) FROM billing_entitlement"
                                " WHERE workspace_id='acme'").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertFalse(self._reserve(customer="cus_a")["granted"])
        self.assertFalse(_billing.trial_eligibility(
            self.con, "acme", "cus_a", self.cfg, now=NOW)["eligible"])

    def test_checkout_failure_keeps_reservation_by_default(self):
        self._reserve(customer="cus_a")
        later = NOW + 30 * 86400
        self.assertFalse(self._reserve(customer="cus_a", now=later)["granted"])

    def test_abandoned_checkout_expiry_event_does_not_release_by_default(self):
        rid = self._reserve(customer="cus_a")["reservation_id"]
        event = self._expired_event("evt_exp", rid)
        out = _billing.apply_event(self.con, "acme", event, now=NOW, config=self.cfg)
        self.con.commit()
        self.assertEqual(out["status"], "ignored")
        self.assertIn("UNAPPROVED", out["reason"])
        self.assertFalse(self._reserve(customer="cus_a")["granted"])

    def _expired_event(self, eid, rid, workspace="acme", session="cs_1"):
        return {"id": eid, "type": "checkout.session.expired", "livemode": False,
                "data": {"object": {"id": session, "customer": "",
                                    "client_reference_id": workspace,
                                    "metadata": {"workspace_id": workspace,
                                                 "trial_reservation": rid}}}}

    def test_abandoned_checkout_releases_when_switch_on(self):
        cfg = _cfg(trial_release_abandoned=True)
        rid = self._reserve(customer="cus_a", cfg=cfg)["reservation_id"]
        out = _billing.apply_event(self.con, "acme", self._expired_event("evt_exp", rid),
                                   now=NOW, config=cfg)
        self.con.commit()
        self.assertEqual(out["status"], "reconciled")
        again = self._reserve(customer="cus_a", cfg=cfg)
        self.assertTrue(again["granted"])
        self.assertNotEqual(again["reservation_id"], rid)

    def test_release_refuses_wrong_reservation_workspace_and_consumed(self):
        cfg = _cfg(trial_release_abandoned=True)
        rid = self._reserve(customer="cus_a", cfg=cfg)["reservation_id"]
        wrong = _billing.apply_event(
            self.con, "acme", self._expired_event("evt_w", "someone-else"),
            now=NOW, config=cfg)
        self.assertEqual(wrong["status"], "ignored")
        foreign = _billing.apply_event(
            self.con, "acme", self._expired_event("evt_f", rid, workspace="acme2"),
            now=NOW, config=cfg)
        self.assertEqual(foreign["status"], "ignored")
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg)["granted"])
        _apply(self.con, _event("evt_1"), _resource(), cfg=cfg)
        consumed = _billing.apply_event(
            self.con, "acme", self._expired_event("evt_c", rid), now=NOW, config=cfg)
        self.assertEqual(consumed["status"], "ignored")
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg)["granted"])

    def test_release_never_touches_a_consumed_trial_even_without_a_subscription_row(self):
        cfg = _cfg(trial_release_abandoned=True)
        rid = self._reserve(customer="cus_a", cfg=cfg)["reservation_id"]
        self.con.execute("UPDATE billing_trials SET trial_state='consumed'"
                         " WHERE workspace_id='acme'")
        self.con.commit()
        out = _billing.release_trial_reservation(self.con, "acme", rid, cfg)
        self.assertFalse(out["released"])
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg)["granted"])

    def test_stale_reservation_released_lazily_only_when_switch_on(self):
        later = NOW + self.cfg.trial_reservation_ttl_s + 1
        self._reserve(customer="cus_a")
        self.assertFalse(self._reserve(customer="cus_a", now=later)["granted"])
        cfg = _cfg(trial_release_abandoned=True)
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg, now=NOW + 60)["granted"])
        self.assertTrue(self._reserve(customer="cus_a", cfg=cfg, now=later)["granted"])

    def test_late_webhook_of_released_checkout_counts_the_trial_again(self):
        cfg = _cfg(trial_release_abandoned=True)
        later = NOW + cfg.trial_reservation_ttl_s + 1
        self._reserve(customer="cus_a", cfg=cfg)
        self.assertTrue(self._reserve(customer="cus_a", cfg=cfg, now=later)["granted"])
        _apply(self.con, _event("evt_late"), _resource(), cfg=cfg, now=later)
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg, now=later + 5)["granted"])

    def test_stale_reservation_with_verified_subscription_is_never_released(self):
        cfg = _cfg(trial_release_abandoned=True)
        self._reserve(customer="cus_a", cfg=cfg)
        _apply(self.con, _event("evt_1"), _resource(), cfg=cfg)
        later = NOW + cfg.trial_reservation_ttl_s * 5
        self.assertFalse(self._reserve(customer="cus_a", cfg=cfg, now=later)["granted"])

    def test_cancelled_trial_counts_by_default_and_by_switch(self):
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        _apply(self.con, _event("evt_2"), _resource(status="canceled"), now=NOW + 3600)
        self.assertFalse(self._reserve(customer="cus_a")["granted"])
        lenient = _cfg(trial_cancelled_counts=False)
        self.assertTrue(self._reserve(customer="cus_a", cfg=lenient)["granted"])

    def test_expired_trial_counts_by_default_and_by_switch(self):
        end = NOW + 14 * 86400
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        _apply(self.con, _event("evt_2"),
               _resource(status="canceled", trial_end=end, period=end),
               now=end + 3600)
        self.assertFalse(self._reserve(customer="cus_a", now=end + 7200)["granted"])
        # cancelled-during-trial is a different outcome: its own switch decides
        self.assertFalse(_billing.trial_eligibility(
            self.con, "acme", "cus_a", _cfg(trial_cancelled_counts=False),
            now=end + 7200)["eligible"])
        lenient = _cfg(trial_expired_counts=False)
        self.assertTrue(_billing.trial_eligibility(
            self.con, "acme", "cus_a", lenient, now=end + 7200)["eligible"])
        self.assertTrue(self._reserve(customer="cus_a", cfg=lenient,
                                      now=end + 7200)["granted"])

    def test_cancelled_vs_expired_comes_from_provider_times_not_processing_time(self):
        end = NOW + 14 * 86400
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        # cancelled a day before the trial ended; webhook processed a week after
        resource = _resource(status="canceled", trial_end=end, period=end)
        resource.update(canceled_at=end - 86400, ended_at=end - 86400)
        _apply(self.con, _event("evt_2"), resource, now=end + 7 * 86400)
        row = self.con.execute("SELECT trial_outcome FROM billing_trials"
                               " WHERE workspace_id='acme'").fetchone()
        self.assertEqual(row[0], "cancelled")
        self.assertTrue(_billing.trial_eligibility(
            self.con, "acme", "cus_a", _cfg(trial_cancelled_counts=False),
            now=end + 8 * 86400)["eligible"])

    def test_trial_that_ran_out_is_expired_even_if_processed_early(self):
        end = NOW + 14 * 86400
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        resource = _resource(status="canceled", trial_end=end, period=end)
        resource.update(canceled_at=end, ended_at=end)
        _apply(self.con, _event("evt_2"), resource, now=NOW + 3600)
        row = self.con.execute("SELECT trial_outcome FROM billing_trials"
                               " WHERE workspace_id='acme'").fetchone()
        self.assertEqual(row[0], "expired")

    def test_gone_event_keeps_the_provider_dated_outcome(self):
        end = NOW + 14 * 86400
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        resource = _resource(status="canceled", trial_end=end, period=end)
        resource.update(canceled_at=end - 5, ended_at=end - 5)
        _apply(self.con, _event("evt_2"), resource, now=end + 100)
        gone = _billing.FixtureSubscriptionProtocol(
            failures={"sub_1": _billing.ProviderNotFound("deleted")})
        _billing.apply_event(self.con, "acme", _event("evt_3", "customer.subscription.deleted"),
                             now=end + 9 * 86400, config=_cfg(), provider=gone)
        row = self.con.execute("SELECT trial_outcome FROM billing_trials"
                               " WHERE workspace_id='acme'").fetchone()
        self.assertEqual(row[0], "cancelled")

    def test_status_reason_after_reservation_is_clear(self):
        self._reserve(customer="cus_a")
        out = _billing.entitlement_status(self.con, "acme", self.cfg, now=NOW)
        self.assertFalse(out["allowed"])
        self.assertIn("trial reserved", out["reason"])
        self.assertIn("no verified subscription", out["reason"])
        _billing.bind_billing_customer(self.con, "other", "cus_x", operator="o")
        _billing.bind_billing_customer(self.con, "other", "cus_y", operator="o")
        out = _billing.entitlement_status(self.con, "other", self.cfg, now=NOW)
        self.assertEqual(out["reason"], "no billing record")

    def test_converted_trial_always_counts_even_after_cancel(self):
        end = NOW + 14 * 86400
        _apply(self.con, _event("evt_1"), _resource(status="trialing"))
        _apply(self.con, _event("evt_2"),
               _resource(status="active", trial_end=end, period=end + 30 * 86400),
               now=end + 60)
        _apply(self.con, _event("evt_3"),
               _resource(status="canceled", trial_end=end, period=end + 30 * 86400),
               now=end + 40 * 86400)
        lenient = _cfg(trial_cancelled_counts=False, trial_expired_counts=False)
        self.assertFalse(self._reserve(customer="cus_a", cfg=lenient,
                                       now=end + 50 * 86400)["granted"])

    def test_prior_paid_subscription_blocks_trial_unless_switched(self):
        _apply(self.con, _event("evt_1"),
               _resource(status="active", trial_end=0, period=NOW + 30 * 86400))
        self.assertFalse(self._reserve(customer="cus_a")["granted"])
        lenient = _cfg(prior_subscription_blocks_trial=False)
        self.assertTrue(self._reserve(customer="cus_a", cfg=lenient)["granted"])

    def test_legacy_subscription_history_without_reservation_counts(self):
        self.con.execute(
            "INSERT INTO billing_entitlement (workspace_id, customer_id, status,"
            " price_id, current_period_end, updated_at)"
            " VALUES ('acme', 'cus_a', 'canceled', 'price_pro', 5, 'x')")
        self.con.execute(
            "INSERT INTO billing_subscriptions (subscription_id, workspace_id,"
            " customer_id, verified_at) VALUES ('sub_old', 'acme', 'cus_a', 'x')")
        self.con.commit()
        self.assertFalse(self._reserve(customer="cus_a")["granted"])
        self.assertFalse(self._reserve(ws="acme2", customer="cus_a")["granted"])

    def test_ineligible_reserve_does_not_overwrite_history(self):
        first = self._reserve(customer="cus_a")
        self._reserve(customer="cus_b")
        row = self.con.execute("SELECT trial_reservation_id FROM billing_trials"
                               " WHERE workspace_id='acme'").fetchone()
        self.assertEqual(row[0], first["reservation_id"])

    def test_reserve_inside_caller_transaction_rolls_back_with_it(self):
        self.con.execute("BEGIN")
        _billing.reserve_trial(self.con, "acme", "cus_a", self.cfg, now=NOW)
        self.con.rollback()
        self.assertTrue(self._reserve(customer="cus_a")["granted"])


class CheckoutTrialHttpTest(_HttpBase):
    """The checkout route reserves before the provider call and sends the
    catalog trial only when the reservation is granted. No network: the
    provider transport is replaced."""

    def _checkout(self, body, sent):
        def fake(config, method, path, fields):
            sent.append(dict(fields))
            return {"id": f"cs_{len(sent)}", "url": "https://checkout.invalid/s",
                    "livemode": True}
        with mock.patch.object(_services, "_billing_config", return_value=_cfg()), \
                mock.patch.object(_billing, "_stripe_request", fake):
            return self._call("/api/v1/billing/checkout", body=body,
                              headers=self._csrf())

    def test_first_checkout_gets_the_trial_second_does_not(self):
        sent = []
        status, body = self._checkout({"price_id": "price_pro"}, sent)
        self.assertEqual(status, 200, body)
        self.assertEqual(sent[0].get("subscription_data[trial_period_days]"), "14")
        rid = sent[0].get("metadata[trial_reservation]")
        self.assertTrue(rid)
        self.assertEqual(sent[0].get("subscription_data[metadata][trial_reservation]"), rid)
        status, body = self._checkout({"price_id": "price_pro"}, sent)
        self.assertEqual(status, 200, body)
        self.assertNotIn("subscription_data[trial_period_days]", sent[1])
        self.assertNotIn("metadata[trial_reservation]", sent[1])

    def test_invalid_requests_do_not_burn_the_reservation(self):
        sent = []
        status, _ = self._checkout({"price_id": "price_pro", "trial_days": 99}, sent)
        self.assertEqual(status, 400)
        status, _ = self._checkout({"price_id": "price_missing"}, sent)
        self.assertEqual(status, 400)
        status, _ = self._checkout({"price_id": "price_pro",
                                    "success_url": "https://evil.example/x"}, sent)
        self.assertEqual(status, 400)
        self.assertEqual(sent, [])
        status, body = self._checkout({"price_id": "price_pro"}, sent)
        self.assertEqual(status, 200, body)
        self.assertEqual(sent[0].get("subscription_data[trial_period_days]"), "14")

    def test_price_without_catalog_trial_reserves_nothing(self):
        sent = []

        def fake(config, method, path, fields):
            sent.append(dict(fields))
            return {"id": "cs_x", "url": "https://checkout.invalid/s", "livemode": True}
        cfg = _cfg(prices={"price_plain": {"amount_cents": 100}})
        with mock.patch.object(_services, "_billing_config", return_value=cfg), \
                mock.patch.object(_billing, "_stripe_request", fake):
            status, body = self._call("/api/v1/billing/checkout",
                                      body={"price_id": "price_plain"},
                                      headers=self._csrf())
        self.assertEqual(status, 200, body)
        self.assertNotIn("subscription_data[trial_period_days]", sent[0])
        con = sqlite3.connect(self.db_path)
        try:
            state = con.execute("SELECT (SELECT COUNT(*) FROM billing_entitlement)"
                                " + (SELECT COUNT(*) FROM billing_trials)").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(state, 0)


class CreateCheckoutCompatTest(unittest.TestCase):
    def test_defaults_still_send_the_catalog_trial(self):
        sent = {}

        def fake(config, method, path, fields):
            sent.update(fields)
            return {"id": "cs", "url": "u", "livemode": True}
        with mock.patch.object(_billing, "_stripe_request", fake):
            _billing.create_checkout_session(_cfg(), price_id="price_pro",
                                             workspace_id="acme")
        self.assertEqual(sent["subscription_data[trial_period_days]"], "14")

    def test_allow_trial_false_sends_none(self):
        sent = {}

        def fake(config, method, path, fields):
            sent.update(fields)
            return {"id": "cs", "url": "u", "livemode": True}
        with mock.patch.object(_billing, "_stripe_request", fake):
            _billing.create_checkout_session(_cfg(), price_id="price_pro",
                                             workspace_id="acme", allow_trial=False)
        self.assertNotIn("subscription_data[trial_period_days]", sent)


class CollaborationGateTest(_HttpBase):
    """Collaboration (members, invites, shares) is exempt by design, with
    two exceptions: invite send, and share titles that reveal content."""

    def setUp(self):
        super().setUp()
        from tests import test_invites_send as _inv
        self.fake = _inv.FakeResend(os.path.join(self.http.tmp.name, "resend.json"))
        self.http.app.invite_mailer = _inv._mailer(self.fake)

    def _config(self, **over):
        return mock.patch.object(_services, "_billing_config",
                                 return_value=_cfg(**over))

    def test_collaboration_stays_open_with_the_gate_on(self):
        headers = self.http._csrf()
        with self._config(entitlement_required=True):
            for path in ("/api/v1/workspaces/acme/members",
                         "/api/v1/workspaces/acme/invites", "/api/v1/shares"):
                status, body = self._call(path, method="GET", headers=headers)
                self.assertEqual(status, 200, (path, body))
            status, body = self._call("/api/v1/workspaces/acme/invites",
                                      body={"member": "x@example.test", "role": "viewer"},
                                      headers=headers)
            self.assertEqual(status, 200, body)

    def test_invite_send_is_refused_when_on_and_sends_nothing(self):
        headers = self.http._csrf()
        with self._config():
            status, out = self._call("/api/v1/workspaces/acme/invites",
                                     body={"member": "invitee@example.test", "role": "viewer"},
                                     headers=headers)
        self.assertEqual(status, 200, out)
        path = f"/api/v1/workspaces/acme/invites/{out['invite_id']}/send"
        with self._config(entitlement_required=True):
            status, body = self._call(path, body={}, headers=headers)
            self.assertEqual(status, 402, body)
            self.assertEqual(self.fake.state()["requests"], [])
            # a caller without the right still gets 403, not a billing reason
            status, body = self._call(path, body={}, headers={
                "authorization": "Bearer " + self.http.reader_token})
            self.assertEqual(status, 403, body)
        with self._config():
            status, body = self._call(path, body={}, headers=headers)
            self.assertEqual(status, 200, body)
            self.assertEqual(len(self.fake.state()["accepted"]), 1)

    def test_invite_send_without_the_right_is_403_before_any_billing_reason(self):
        from earwitness.app import invites_send as _invites_send

        class Stop(Exception):
            pass

        def stub(con, entry, operation):
            raise Stop()

        denied = _access.AuthorizationError("owner manages membership", 403)
        ctx = mock.MagicMock()
        with mock.patch.object(_access, "require_customer_mutation", lambda c, o: None), \
                mock.patch.object(_services, "_require_entitlement", stub), \
                mock.patch.object(_services._collaboration, "check_permission",
                                  return_value=False), \
                mock.patch.object(_invites_send, "prepare_send", side_effect=denied):
            with self.assertRaises(_access.AuthorizationError) as caught:
                _services.collab_invite_send(ctx, None, {"row_namespace": "acme",
                                                         "workspace_id": "acme"}, "i1", None)
        self.assertEqual(caught.exception.status, 403)

    def _shares_fixture(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        _billing.ensure_billing_schema(con)
        con.execute("CREATE TABLE shares (id TEXT, workspace_id TEXT, object_type TEXT,"
                    " object_id TEXT, audience TEXT, expires_at TEXT, revoked INTEGER,"
                    " revision INTEGER)")
        con.execute("INSERT INTO shares VALUES ('s1','acme','campaign','1','a','', 0, 1)")
        return con

    def test_share_titles_are_dropped_when_the_workspace_is_not_entitled(self):
        con = self._shares_fixture()
        ctx = mock.MagicMock()
        # A resolved entry carries paid_use (connections.resolve_workspace, RB1).
        entry = {"row_namespace": "acme", "paid_use": True, "paid_use_reason": ""}
        titled = {("campaign", "1"): "Secret campaign"}
        with mock.patch.object(_services, "_share_object_titles", return_value=titled), \
                mock.patch.object(_services, "_visible_member_emails", return_value={}):
            with self._config():
                rows = _services.collab_shares(ctx, con, entry)["shares"]
                self.assertEqual(rows[0]["object_title"], "Secret campaign")
            with self._config(entitlement_required=True):
                rows = _services.collab_shares(ctx, con, entry)["shares"]
                self.assertEqual(rows[0]["object_title"], "")
                self.assertEqual(rows[0]["id"], "s1")
            con.execute("INSERT INTO billing_entitlement (workspace_id, status,"
                        " current_period_end, updated_at) VALUES ('acme','active',?,'x')",
                        (int(time.time()) + 3600,))
            with self._config(entitlement_required=True):
                rows = _services.collab_shares(ctx, con, entry)["shares"]
                self.assertEqual(rows[0]["object_title"], "Secret campaign")
        con.close()


class StartupConfigTest(_HttpBase):
    BAD = {"EARWITNESS_BILLING_UNGATED_OPERATIONS": "custmer_search"}

    def _config(self):
        return _asgi_mod.HostedConfig(
            mode="local-test", data_dir=self.http.data_dir,
            grants_db=os.path.join(self.http.tmp.name, "grants2.sqlite3"))

    def test_create_app_refuses_a_bad_billing_config_naming_it(self):
        with mock.patch.dict(os.environ, self.BAD):
            with self.assertRaises(_health.ConfigError) as caught:
                _asgi_mod.create_app(self._config())
        self.assertIn("custmer_search", str(caught.exception))
        self.assertIn("EARWITNESS_BILLING_UNGATED_OPERATIONS", str(caught.exception))
        with mock.patch.dict(os.environ, {"EARWITNESS_BILLING_GATED_OPERATIONS": "a"}):
            with self.assertRaises(_health.ConfigError):
                _asgi_mod.create_app(self._config())

    def test_create_app_starts_with_a_valid_or_absent_billing_config(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("EARWITNESS_BILLING")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNotNone(_asgi_mod.create_app(self._config()))
        with mock.patch.dict(os.environ, {
                "EARWITNESS_BILLING_UNGATED_OPERATIONS": "customer_overview,billing_checkout"}):
            self.assertIsNotNone(_asgi_mod.create_app(self._config()))

    def test_per_request_path_never_shows_the_setting_name(self):
        headers = {"authorization": "Bearer " + self.http.reader_token}
        with mock.patch.dict(os.environ, self.BAD):
            status, body = self._call("/api/v1/customer/overview", method="GET",
                                      headers=headers)
            self.assertEqual(status, 500, body)
            self.assertNotIn("EARWITNESS", json.dumps(body))
            self.assertNotIn("custmer_search", json.dumps(body))
            status, body = self._call("/mcp", method="POST", headers=headers,
                                      body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(status, 500, body)
            self.assertNotIn("EARWITNESS", json.dumps(body))

    def test_services_config_error_is_generic(self):
        with mock.patch.dict(os.environ, self.BAD):
            with self.assertRaises(_services.BillingConfigError) as caught:
                _services._billing_config()
        self.assertNotIn("EARWITNESS", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
