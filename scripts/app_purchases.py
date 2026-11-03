#!/usr/bin/env python3
"""The purchase step of the nightly window (purchase design 5.3, BY3).

  pending      --run-key K
  window       --window-lock-fd 3 --run-key K --budget-s N
  repair       --window-lock-fd 3 --run-key K
  finish       --subscription S --window-lock-fd 3 [--run-key K]
  delivered    --subscription S
  cancel-request --subscription S
  status       --subscription S
  retire       --subscription S --window-lock-fd 3 [--run-key K]
  send-links   --run-key K
  morning-check [--records-dir D] [--date YYYY-MM-DD]

Exit codes follow the pilot_sync contract (ws-fresh/pilot-sync-cli-contract.md):
0 done; 4 deferred (a preserve-ok completion record exists); 3 refused
before any write; 5 failed after writes began (the site stays stopped);
2 any other failure. One JSON line comes last on stdout; diagnostics go to
stderr. No line ever holds an email, a password, a token or a key.

BY3 builds the password fallback only (design 9.5): with
EARWITNESS_PURCHASE_SETUP_LINK other than `off`, `window`, `finish` and
`send-links` refuse (`refused:SETUP_LINK_NOT_BUILT`) before any write.

The commands read the app's own settings (asgi.config_from_env:
EARWITNESS_GRANTS_DB, EARWITNESS_DATA_DIR, EARWITNESS_ADMISSION_FILE,
EARWITNESS_PUBLIC_BASE_URL), the billing settings (billing.config_from_env),
the layout file named by EARWITNESS_PILOT_LAYOUT, and the window Stripe key
(STRIPE_SECRET_KEY). See docs/hosting/purchases.md.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import window_lock  # noqa: E402

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_DEFERRED, EXIT_AFTER_WRITES = 0, 2, 3, 4, 5
RESULT_FORMAT = "earwitness-purchases-result/1"
RELEASE_FORMAT = "earwitness-purchase-release/1"
DEFAULT_RELEASE_DIR = "/var/lib/earwitness/purchase-release"
DEFAULT_RECORDS_DIR = "/var/lib/earwitness/sync/runs"
RUN_KEY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SUB_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")
BUYER_RESERVE_S = 30          # design 5.3 budget: under 30 s left, defer
ESTIMATE_BASE_S, ESTIMATE_PER_BUYER_S = 60, 30
SYNC_DUE_S = 20 * 3600        # pending: daily reconciliation due
SYNC_ALERT_S = 26 * 3600      # morning check
DEFAULT_CLOSE_BEFORE_END_S = 31 * 86400   # design 7.1 / D3 when no close date is set
LINKS_MISSING = "purchase_links_missing"
BILLING_TABLES = ["billing_bindings", "billing_entitlement", "billing_events",
                  "billing_subscriptions", "billing_trials"]
SEAT_STEPS = frozenset({"discover", "record", "bind", "ended", "accounts", "refund_ended",
                        "refund_needed", "error", "finish", "retire"})
REQUEST_STEPS = frozenset({"request"})
WORKSPACE_INTENTS = {"bind_intent": BILLING_TABLES, "sync_intent": BILLING_TABLES,
                     "finish_intent": BILLING_TABLES + ["memberships"]}
PAID_TERMINAL = frozenset({"canceled", "incomplete_expired"})   # never written again
SETUP_STOP = frozenset({"canceled", "incomplete_expired", "unpaid"})  # bind: needs_refund
LA = None
_release_dir = None


class Refused(Exception):
    """Refused before any write: exit 3."""

    def __init__(self, code: str, detail: str = "", **extra):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.extra = extra


class Failed(Exception):
    """A failure: exit 2 before writes began, 5 after."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ------------------------------------------------------------------ helpers

def _la():
    global LA
    if LA is None:
        from zoneinfo import ZoneInfo
        LA = ZoneInfo("America/Los_Angeles")
    return LA


def _production(env) -> bool:
    return str(env.get("EARWITNESS_MODE") or "") == "production"


def set_test_release_dir(path) -> None:
    """Tests only: write release files under `path` (None resets). Refused
    in production, where the folder is always DEFAULT_RELEASE_DIR."""
    global _release_dir
    if path is not None and _production(os.environ):
        raise RuntimeError("a scratch release folder is refused in production")
    _release_dir = path


def release_dir() -> str:
    if _release_dir is not None and _production(os.environ):
        raise RuntimeError("a scratch release folder is refused in production")
    return _release_dir or DEFAULT_RELEASE_DIR


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def _parse_iso(text) -> float | None:
    try:
        value = str(text)
        moment = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def _natural(value: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", value)]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_dir(folder: str) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _read_json(path: str):
    with open(path, "rb") as handle:
        return json.loads(handle.read())


def _connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    con = sqlite3.connect("file:" + quote(os.path.abspath(path), safe="/") + f"?mode={mode}",
                          uri=True, isolation_level=None, timeout=15.0)
    con.row_factory = sqlite3.Row
    return con


@contextlib.contextmanager
def _immediate(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def _idem(*parts) -> str:
    """Idempotency-Key derived from the subscription and the target (7.2)."""
    return "ew-" + _sha256_bytes("|".join(str(p) for p in parts).encode())[:40]


# Provider failures are named by this fixed vocabulary only. No Stripe
# message text is ever logged, journaled or written to a result (R1-09).
PROVIDER_CODES = (("ProviderEvidenceError", "provider_evidence_invalid"),
                  ("ProviderNotFound", "not_found"), ("ProviderTimeout", "timeout"),
                  ("ProviderTooLarge", "too_large"), ("ProviderError", "provider_error"))


def _safe(exc) -> str:
    """A provider failure as one fixed code; any other exception by type."""
    from earwitness.app import billing as _billing
    for name, code in PROVIDER_CODES:
        if isinstance(exc, getattr(_billing, name)):
            return code
    return type(exc).__name__


def _read_alert(exc) -> str:
    """The alert code of a failed subscription read."""
    from earwitness.app import billing as _billing
    if isinstance(exc, _billing.ProviderNotFound):
        return "stripe_subscription_not_found"
    return "stripe_read_failed"


def _describe(exc) -> str:
    """Diagnostic text for an unexpected exception: provider failures by
    code only; our own refusals keep their (safe) text."""
    from earwitness.app import billing as _billing
    if isinstance(exc, _billing.ProviderError):
        return _safe(exc)
    if isinstance(exc, (Refused, Failed, _AfterWrites)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _mini_point(name: str) -> None:
    """A named step inside finish and retire (tests interrupt here)."""


# ------------------------------------------------------------------ context

class Ctx:
    """Settings, clocks and the Stripe provider of one command."""

    def __init__(self, env=None, provider=None, now=None, monotonic=None):
        self.env = os.environ if env is None else env
        self._provider = provider
        self._now = now
        self._monotonic = monotonic or time.monotonic
        self._cache = {}

    def now(self) -> float:
        if self._now is None:
            return time.time()
        return self._now() if callable(self._now) else float(self._now)

    def monotonic(self) -> float:
        return self._monotonic()

    def _cached(self, name, build):
        if name not in self._cache:
            self._cache[name] = build()
        return self._cache[name]

    @property
    def hosted(self):
        from earwitness.app import asgi as _asgi
        return self._cached("hosted", lambda: _asgi.config_from_env(self.env))

    @property
    def billing(self):
        from earwitness.app import billing as _billing

        def build():
            try:
                return _billing.config_from_env(self.env)
            except ValueError as exc:
                raise Refused("CONFIG", f"billing settings: {exc}") from None
        return self._cached("billing", build)

    @property
    def grants_db(self) -> str:
        path = str(self.hosted.grants_db or "")
        if not path or not os.path.isabs(path) or not os.path.isfile(path):
            raise Refused("CONFIG", "EARWITNESS_GRANTS_DB must name the control store")
        return path

    @property
    def data_dir(self) -> str:
        return str(self.hosted.data_dir or "")

    @property
    def policy(self):
        from earwitness.app import admission as _admission

        def build():
            path = str(self.hosted.admission_file or "").strip()
            if not path:
                return None
            try:
                return _admission.load_file(path, warn=False,
                                            production=_production(self.env))
            except _admission.AdmissionError as exc:
                raise Refused("CONFIG", f"admission file: {exc}") from None
        return self._cached("policy", build)

    @property
    def layout(self):
        from scripts import pilot_inventory as inv

        def build():
            path = str(self.env.get("EARWITNESS_PILOT_LAYOUT") or "")
            if not path:
                raise Refused("CONFIG", "EARWITNESS_PILOT_LAYOUT is not set")
            try:
                return inv.load_layout(path)
            except inv.UsageError as exc:
                raise Refused("CONFIG", str(exc)) from None
        return self._cached("layout", build)

    @property
    def issuer(self) -> str:
        from earwitness.app import local_accounts as _la_mod

        def build():
            try:
                return _la_mod.local_issuer(self.hosted.public_base_url)
            except ValueError as exc:
                raise Refused("CONFIG", str(exc)) from None
        return self._cached("issuer", build)

    @property
    def pilot_end(self) -> int:
        from earwitness.app import rights_admission as _rights

        def build():
            try:
                return int(_rights.parse_deployed_end(self.layout["pilot_end"]).timestamp())
            except ValueError as exc:
                raise Refused("CONFIG", f"layout pilot_end: {exc}") from None
        return self._cached("pilot_end", build)

    @property
    def close_at(self) -> int:
        from earwitness.app import rights_admission as _rights

        def build():
            raw = str(self.env.get("EARWITNESS_PURCHASE_CLOSE_AT") or "").strip()
            if not raw:
                return self.pilot_end - DEFAULT_CLOSE_BEFORE_END_S
            try:
                return int(_rights.parse_deployed_end(raw).timestamp())
            except ValueError:
                raise Refused("CONFIG", "EARWITNESS_PURCHASE_CLOSE_AT is not an instant") from None
        return self._cached("close_at", build)

    @property
    def links(self) -> dict:
        def build():
            raw = str(self.env.get("EARWITNESS_PURCHASE_LINKS") or "").strip()
            if not raw:
                return {}
            try:
                loaded = json.loads(raw)
            except ValueError:
                loaded = None
            from earwitness.app.purchases import OFFERS
            if not isinstance(loaded, dict) or set(loaded) - set(OFFERS) or not all(
                    isinstance(v, str) and SUB_RE.match(v) for v in loaded.values()):
                raise Refused("CONFIG", "EARWITNESS_PURCHASE_LINKS must map ew-a and ew-b"
                              " to Payment Link IDs")
            return dict(loaded)
        return self._cached("links", build)

    def offer_prices(self) -> dict:
        from earwitness.app import billing as _billing
        from earwitness.app.purchases import OFFERS
        return {o: sorted(_billing.offer_prices(self.billing, o)) for o in OFFERS}

    @property
    def setup_link(self) -> str:
        return str(self.env.get("EARWITNESS_PURCHASE_SETUP_LINK") or "").strip().lower()

    def require_fallback(self) -> None:
        if self.setup_link != "off":
            raise Refused("SETUP_LINK_NOT_BUILT",
                          "the set-password link is BY5; set EARWITNESS_PURCHASE_SETUP_LINK=off")

    def configured(self) -> tuple:
        """(True, '') when purchase billing is configured for the window."""
        if not str(self.env.get("STRIPE_SECRET_KEY") or ""):
            return False, "no STRIPE_SECRET_KEY"
        if self.policy is None or self.policy.pools is None:
            return False, "no purchase_seats block"
        if not any(self.offer_prices().values()):
            return False, "no price tagged with an offer"
        if self.missing_links() and self.now() < self.close_at:
            # Code r9 MED-2: while sales are open, every sold offer needs its
            # Payment Link in EARWITNESS_PURCHASE_LINKS, or the close could
            # never stop it. Refused before any purchase work.
            return False, LINKS_MISSING
        return True, ""

    def missing_links(self) -> list:
        """Offers with a non-empty pool and no link in EARWITNESS_PURCHASE_LINKS
        while purchase_seats is enabled."""
        policy = self.policy
        if policy is None or not policy.purchase_enabled or not policy.pools:
            return []
        return [o for o, ids in sorted(policy.pools.items()) if ids and o not in self.links]

    def require_configured(self) -> None:
        ok, why = self.configured()
        if why == LINKS_MISSING:
            raise Refused("PURCHASE_LINKS", "EARWITNESS_PURCHASE_LINKS has no Payment Link"
                          f" for {', '.join(self.missing_links())}")
        if not ok:
            raise Refused("NOT_CONFIGURED", why)

    @property
    def stripe(self):
        if self._provider is None:
            from earwitness.app import billing as _billing
            self._provider = _billing.StripeWindowProtocol(
                str(self.env.get("STRIPE_SECRET_KEY") or ""))
        return self._provider

    def control(self, *, readonly: bool = False) -> sqlite3.Connection:
        return _connect(self.grants_db, readonly=readonly)


# ----------------------------------------------------------------- registry

def registry_entry(con, workspace_id: str) -> dict | None:
    row = con.execute("SELECT workspace_id, db_path, row_namespace, db_identity"
                      " FROM workspace_registry WHERE workspace_id=?", (workspace_id,)).fetchone()
    return dict(row) if row else None


def workspace_path(ctx, entry: dict) -> str | None:
    from earwitness.app import connections as _conn
    if not entry:
        return None
    return _conn._contained_db_path(ctx.data_dir, entry["db_path"])


def billing_identity(entry: dict) -> str:
    from earwitness.app import billing as _billing
    return _billing.workspace_billing_identity(entry["workspace_id"], entry["db_identity"])


def layout_db_path(ctx, workspace_id: str) -> str:
    return os.path.join(ctx.layout["data_dir"], workspace_id + ".sqlite3")


def check_targets(ctx) -> dict:
    """R1-04: the databases this command may write are the layout's own
    inventory targets. The app's control store, data folder and admission
    file must be the layout's, and every registered pool or seat workspace
    (not retired) must sit at <layout data_dir>/<id>.sqlite3 with the
    registry's database identity. Refused before any write otherwise.
    Returns {"control": path, "workspaces": {id: path}}."""
    from scripts import pilot_inventory as inv
    from earwitness.app import purchases as _p
    cached = ctx._cache.get("targets")
    if cached is not None:
        return cached
    lay = ctx.layout

    def same(a, b) -> bool:
        return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)
    if not same(ctx.grants_db, lay["control_db"]):
        raise Refused("TARGET_MISMATCH", "EARWITNESS_GRANTS_DB is not the layout's control_db")
    if not same(ctx.data_dir, lay["data_dir"]):
        raise Refused("TARGET_MISMATCH", "EARWITNESS_DATA_DIR is not the layout's data_dir")
    admission = str(ctx.hosted.admission_file or "")
    if admission and not same(admission, lay["admission_file"]):
        raise Refused("TARGET_MISMATCH", "EARWITNESS_ADMISSION_FILE is not the layout's")
    policy = ctx.policy
    con = ctx.control(readonly=True)
    try:
        seats = _p.all_seats(con)
        retired = {s["workspace_id"] for s in seats if s["state"] == "retired"}
        wanted = set((policy.pool_workspaces() if policy else ()))
        wanted |= {s["workspace_id"] for s in seats if s["workspace_id"]}
        workspaces = {}
        for w in sorted(wanted - retired):
            entry = registry_entry(con, w)
            if entry is None:
                continue  # never written; the 5.2 checks refuse an unregistered pool id
            target = os.path.join(lay["data_dir"], w + ".sqlite3")
            if not same(workspace_path(ctx, entry), target):
                raise Refused("TARGET_MISMATCH", f"{w}: its registered database is not the"
                              " layout's target")
            if os.path.isfile(target) and not inv.journal_unresolved(target):
                wcon = inv.open_ro(target)
                try:
                    row = wcon.execute("SELECT value FROM meta WHERE key='workspace_id'"
                                       ).fetchone()
                finally:
                    wcon.close()
                if row is None or row[0] != entry["db_identity"]:
                    raise Refused("TARGET_MISMATCH", f"{w}: the database identity is not"
                                  " the registry's")
            workspaces[w] = target
    finally:
        con.close()
    out = {"control": os.path.normpath(lay["control_db"]), "workspaces": workspaces}
    ctx._cache["targets"] = out
    return out


def seat_resource_problem(ctx, seat: dict, sub: dict, identity: str | None = None, *,
                          allow_unbound: bool = False) -> str | None:
    """R1-03: the one per-seat check of a verified Stripe resource, used by
    bind, sync, finish and the morning check, before any write and on the
    read-back. None when the resource is this seat's; else what differs."""
    if sub.get("id") != seat["subscription_id"]:
        return "subscription_id"
    if sub.get("customer") != seat["customer_id"]:
        return "customer"
    if sub.get("offer") != seat["offer"]:
        return "offer"
    if sub.get("price_id") not in ctx.offer_prices().get(seat["offer"], ()):
        return "price"
    if sub.get("quantity") != 1 or sub.get("item_count") != 1:
        return "quantity"
    if identity is not None:
        bound = sub.get("metadata_workspace") or ""
        if bound != identity and not (allow_unbound and not bound):
            return "identity"
    return None


# BY3-r18: the window adapter (billing.StripeWindowProtocol) validates every
# read for the request that produced it and raises ProviderEvidenceError
# otherwise. Each caller names the customer it expects and keeps the seat's
# prior state on that error, with the `provider_evidence_invalid` alert.

END_DATE_FIELDS = frozenset({"cancel_at", "current_period_start", "current_period_end",
                             "item.current_period_start", "item.current_period_end"})


def paid_latest_invoice(ctx, sub: dict, customer: str) -> bool:
    """Admission's payment evidence (Astra r20 MED-2): True only when the
    subscription's own latest invoice, read as `customer`'s, is paid. An
    absent or malformed latest invoice is never paid. The adapter checks the
    invoice's id and customer; they are compared again here, so a substituted
    read cannot answer for another invoice either."""
    from earwitness.app import billing as _billing
    invoice_id = sub.get("latest_invoice") or ""
    if not invoice_id:
        return False
    invoice = ctx.stripe.get_invoice(invoice_id, customer=customer)
    if invoice.get("id") != invoice_id:
        raise _billing.ProviderEvidenceError("invoice", "id", mismatch=True)
    if invoice.get("customer") != customer:
        raise _billing.ProviderEvidenceError("invoice", "customer", mismatch=True)
    return invoice.get("status") == "paid"


def buyer_email(ctx, customer_id: str) -> str:
    """The email of the subscription's own, not deleted, customer (Astra r20
    MED-2); checked by the adapter and again here with the same validator."""
    from earwitness.app import billing as _billing
    return _billing.check_customer(ctx.stripe.get_customer(customer_id), customer_id)["email"]


def _refund_reason(exc):
    """A refund-step read the adapter refused, in the refund runbook's terms:
    another object or customer is a mismatch, a malformed field incomplete."""
    if not exc.mismatch:
        return RefundEvidenceIncomplete()
    names = {("subscription", "id"): "subscription_id", ("subscription", "customer"): "customer"}
    return RefundEvidenceMismatch(names.get((exc.kind, exc.field), f"{exc.kind}_{exc.field}"))


class RefundEvidenceMismatch(Exception):
    """A refund-step resource that is not this seat's (Astra r14 MED-2);
    `field` names what differs, from a fixed vocabulary."""

    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


class RefundEvidenceIncomplete(Exception):
    """The refund evidence is not complete: the latest invoice's payment list
    is truncated or has a malformed entry, an unknown status or an
    unresolvable paid payment, or a field the step reads (subscription,
    invoice, payment intent, charge) does not have its exact JSON type
    (Astra r16 MED-1). A lasting block, told apart from a provider outage."""


def refund_evidence(stripe, seat: dict, sub: dict) -> list:
    """The charges behind the paid payments of a terminal subscription's
    latest invoice, read and checked as this seat's (design 5.3 step 5).
    Every object's id is the one requested; the subscription's and the
    invoice's customer are the seat's; the intent's and the charge's customer,
    and the charge's payment intent, match when present. Incomplete or
    malformed evidence (checked before identity) raises
    RefundEvidenceIncomplete; an intent with no charge
    raises ProviderError; a contradiction raises RefundEvidenceMismatch. The caller
    then keeps the row (never `ended` on missing evidence)."""
    from earwitness.app import billing as _billing
    customer = seat["customer_id"]

    def same(field, got, want, *, optional=False):
        if not want or (got != want and not (optional and got == "")):
            raise RefundEvidenceMismatch(field)

    def well_formed(obj, key="well_formed"):
        if obj.get(key) is not True:
            raise RefundEvidenceIncomplete()

    well_formed(sub)
    same("subscription_id", sub.get("id"), seat["subscription_id"])
    same("customer", sub.get("customer"), customer)
    if not sub.get("latest_invoice"):
        return []

    def read(fn, ident):
        try:
            return fn(ident, customer=customer)
        except _billing.ProviderEvidenceError as exc:
            raise _refund_reason(exc) from exc
    invoice = read(stripe.get_invoice, sub["latest_invoice"])
    well_formed(invoice, "complete")
    same("invoice_id", invoice.get("id"), sub["latest_invoice"])
    same("invoice_customer", invoice.get("customer"), customer)
    wanted = [(c, "") for c in invoice["charges"]]
    for intent_id in invoice["payment_intents"]:
        intent = read(stripe.get_payment_intent, intent_id)
        well_formed(intent)
        same("payment_intent_id", intent.get("id"), intent_id)
        same("payment_intent_customer", intent.get("customer"), customer, optional=True)
        # A payment-intent payment's charge is its latest_charge; none is
        # missing evidence, never a refund.
        if not intent.get("latest_charge"):
            raise _billing.ProviderError("payment_intent_without_charge")
        wanted.append((intent["latest_charge"], intent_id))
    charges = []
    for charge_id, intent_id in wanted:
        charge = read(stripe.get_charge, charge_id)
        well_formed(charge)
        same("charge_id", charge.get("id"), charge_id)
        same("charge_customer", charge.get("customer"), customer, optional=True)
        if intent_id:
            same("charge_payment_intent", charge.get("payment_intent"), intent_id,
                 optional=True)
        charges.append(charge)
    return charges


# -------------------------------------------------------------- rights (6)

AUDIENCE_USES = frozenset({"member_view", "member_invite", "share_view", "agent_read", "export"})
PLAN_USES = {
    "ew-a": frozenset({("member_view", "metadata"), ("member_view", "excerpt"),
                       ("export", "metadata"), ("export", "excerpt")}),
    "ew-b": frozenset({("member_view", "metadata"), ("member_view", "excerpt"),
                       ("member_view", "transcript"),
                       ("export", "metadata"), ("export", "excerpt")}),
}


def rights_problem(ctx, con, offer: str, entry: dict) -> str | None:
    """None when the workspace's signed rights record matches the plan
    (design 6): its digest recomputes, it names this database, and its
    audience uses are exactly the plan's, plus `agent_read` for viewed
    categories only when agent access is on. Ingest uses are not judged."""
    from earwitness.app import rights_admission as _rights
    row = con.execute("SELECT * FROM pilot_rights_admissions WHERE workspace_identity=?",
                      (entry["db_identity"],)).fetchone()
    if row is None:
        return "no rights record"
    if not _rights.row_digest_ok(row):
        return "rights record digest does not recompute"
    uses = {tuple(u) for u in json.loads(row["uses"]) if u[0] in AUDIENCE_USES}
    agent = {u for u in uses if u[0] == "agent_read"}
    plan = PLAN_USES[offer]
    features = _rights.parse_features(ctx.env.get("EARWITNESS_PILOT_FEATURES"))
    agent_on = features is not None and "agent_access" in features
    viewed = {c for (use, c) in plan if use == "member_view"}
    if agent and (not agent_on or any(c not in viewed for _, c in agent)):
        return "rights record allows agent reads the plan does not"
    if uses - agent != plan:
        return "rights record uses do not match the plan"
    return None


def paid_use_problem(con, entry: dict) -> str:
    """'' when a registered workspace's rights record allows paid use
    (rights_admission.paid_use), else the reason's code: `evaluation_only`,
    `record_tampered` or `rights_unreadable` (BY3-r20)."""
    from earwitness.app import rights_admission as _rights
    allowed, reason = _rights.paid_use(con, entry["db_identity"])
    return "" if allowed else (reason.split(":", 1)[0] or "rights_unreadable")


def pool_paid_problems(ctx, con) -> list:
    """(workspace, reason) for each registered, non-retired pool workspace
    whose rights record does not allow paid use (BY3-r20): the window's
    start-of-run and the morning check report each one once."""
    policy = ctx.policy
    if policy is None or not policy.pools:
        return []
    out = []
    for workspace in sorted(policy.pool_workspaces(), key=_natural):
        if workspace in policy.retired_workspace_ids:
            continue
        try:
            entry = registry_entry(con, workspace)
        except sqlite3.Error:
            out.append((workspace, "rights_unreadable"))
            continue
        if entry is not None:
            problem = paid_use_problem(con, entry)
            if problem:
                out.append((workspace, problem))
    return out


# purchases.reserve's paid-use refusal (the backstop): SeatError's text.
RESERVE_NOT_PAID_CAPABLE = "workspace not paid-capable"


# ----------------------------------------------------------- the register

def _register_requests(ctx):
    """(request_id, request.json doc) for every request folder."""
    folder = ctx.layout["register_dir"]
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return
    from scripts import pilot_inventory as inv
    for name in names:
        if not inv.REQ_RE.match(name):
            continue
        try:
            doc = _read_json(os.path.join(folder, name, "request.json"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            yield name, doc


def register_lines(ctx, req: str) -> list:
    path = os.path.join(ctx.layout["register_dir"], req, "register.log")
    try:
        with open(path, "rb") as handle:
            return [json.loads(line) for line in handle.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def _new_request_id(ctx, run_key: str) -> str:
    day = run_key[:10].replace("-", "")
    used = []
    for name in os.listdir(ctx.layout["register_dir"]):
        m = re.match(rf"^REQ-{day}-([0-9]+)$", name)
        if m:
            used.append(int(m.group(1)))
    return f"REQ-{day}-{max(used, default=0) + 1}"


def create_request(ctx, run_key: str, doc: dict) -> str:
    req = _new_request_id(ctx, run_key)
    folder = os.path.join(ctx.layout["register_dir"], req)
    os.makedirs(folder)
    doc = dict(doc, request_id=req)
    _atomic_bytes(os.path.join(folder, "request.json"), _json_bytes(doc))
    return req


def pilot_record(ctx, req: str, step: str, outcome: str, evidence=(), values=None) -> None:
    """pilot_sql.py record, in-process; its summary line goes to stderr so
    this command's one JSON line stays the only stdout line."""
    from scripts import pilot_sql as psql
    with contextlib.redirect_stdout(sys.stderr):
        try:
            code = psql.record(ctx.layout, req, step, outcome, list(evidence),
                               {k: str(v) for k, v in (values or {}).items()})
        except psql.Refused as exc:
            raise Failed("RECORD", str(exc)) from None
    if code != 0:
        raise Failed("RECORD", f"pilot_sql record exited {code}")


def buyer_request(ctx, subscription_id: str):
    """(request_id, doc) of the seat record request for a subscription."""
    for req, doc in _register_requests(ctx):
        purchase = doc.get("purchase") or {}
        if doc.get("kind") == "admission_change" and \
                purchase.get("subscription_id") == subscription_id:
            return req, doc
    return None, None


def line_protected(ctx, req: str, line: dict) -> bool:
    """The protected copy holds this exact line, the same request.json and
    every evidence file with its recorded hash (BY3 r3, R1-01)."""
    from scripts import pilot_sql as psql
    return psql.line_protected(ctx.layout, req, line)


def verify_line(ctx, req: str, line: dict) -> None:
    """Resume an interrupted record: mirror again (never append) until the
    protected copy holds `line`; Failed otherwise."""
    from scripts import pilot_sql as psql
    if line_protected(ctx, req, line):
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):
            psql.remirror(ctx.layout, req)
    except (OSError, psql.Refused) as exc:
        raise Failed("RECORD", f"{req}: protected copy failed ({type(exc).__name__})") from None
    if not line_protected(ctx, req, line):
        raise Failed("RECORD", f"{req}: the protected copy does not match the register")


def record_line(ctx, req: str, step: str, outcome: str, evidence=(), values=None,
                existing=None) -> dict:
    """Append one register line (or resume `existing`), then verify its
    protected copy. Returns the verified line."""
    if existing is None:
        pilot_record(ctx, req, step, outcome, evidence, values)
        existing = register_lines(ctx, req)[-1]
    verify_line(ctx, req, existing)
    return existing


def _record_lines(ctx, req: str) -> list:
    return [r for r in register_lines(ctx, req)
            if r.get("step") == "record" and r.get("outcome") == "ok"]


def buyer_recorded(ctx, req: str) -> bool:
    """True only for a record line whose protected copy is verified."""
    return any(line_protected(ctx, req, r) for r in _record_lines(ctx, req))


def recorded_tuple(ctx, doc: dict, req: str) -> dict | None:
    """(P, E, S, customer, W, D, offer) of a verified buyer record, or None
    when the record is not verified or its fields do not form one tuple."""
    if not buyer_recorded(ctx, req):
        return None
    from earwitness.app import local_accounts as _la_mod
    principals = doc.get("principals") or []
    purchase = doc.get("purchase") or {}
    workspace = doc.get("workspace") or {}
    if len(principals) != 1 or not isinstance(principals[0], dict):
        return None
    p, e = principals[0].get("principal"), principals[0].get("email")
    try:
        if not e or _la_mod.principal(ctx.issuer, e) != p or \
                _la_mod.canonical(e)[0] != e:
            return None
    except _la_mod.EmailError:
        return None
    out = {"principal": p, "email": e, "subscription_id": purchase.get("subscription_id"),
           "customer_id": purchase.get("customer_id"), "offer": purchase.get("offer"),
           "workspace_id": workspace.get("workspace_id"),
           "db_identity": workspace.get("db_identity")}
    return out if all(out.values()) else None


def request_matches(doc: dict, seat: dict, entry: dict) -> bool:
    """An existing request may serve as this reserved seat's record only
    when every field names this seat."""
    purchase = doc.get("purchase") or {}
    return (doc.get("kind") == "admission_change"
            and doc.get("principals") == [{"principal": seat["principal"],
                                           "email": seat["email"]}]
            and doc.get("workspace") == {"workspace_id": seat["workspace_id"],
                                         "db_identity": entry["db_identity"]}
            and purchase.get("subscription_id") == seat["subscription_id"]
            and purchase.get("offer") == seat["offer"]
            and purchase.get("customer_id") == seat["customer_id"])


# ------------------------------------------------------------------- runs

def _completion_lines(lines) -> list:
    return [line for line in lines
            if line.get("step") == "purchases" and line.get("outcome") == "ok"
            and (line.get("values") or {}).get("preserve") == "ok"
            and (line.get("values") or {}).get("checks_ok") == "true"]


def _completion(ctx, req: str, lines) -> dict | None:
    """The run's completion record: a purchases/ok line with checks and
    preserve, whose protected copy is verified."""
    for line in reversed(_completion_lines(lines)):
        if line_protected(ctx, req, line):
            return line
    return None


# A local marker that a run's completion record was verified in the
# protected copy (R2-03). Written only after that verification, so the
# daytime commands and the morning check can tell a complete run from an
# open one while the protected volume is unmounted. A local purchases/ok
# line without it (an interrupted mirror) stays open.
VERIFIED_NAME = "purchases-verified.json"


def _line_sha(line: dict) -> str:
    return _sha256_bytes(json.dumps(line, sort_keys=True).encode("utf-8"))


def publish_verified(ctx, req: str, line: dict) -> None:
    """Call only after line_protected(ctx, req, line) is True."""
    values = line.get("values") or {}
    name = next((e["name"] for e in line.get("evidence") or []
                 if e.get("sha256") == values.get("result_sha256")), "")
    path = os.path.join(ctx.layout["register_dir"], req, VERIFIED_NAME)
    _atomic_bytes(path, _json_bytes({"request_id": req, "line_sha256": _line_sha(line),
                                     "result_name": name,
                                     "result_sha256": values.get("result_sha256")}))


def local_completion(ctx, req: str, lines) -> dict | None:
    """The completion line the local marker vouches for, or None."""
    try:
        marker = _read_json(os.path.join(ctx.layout["register_dir"], req, VERIFIED_NAME))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("request_id") != req:
        return None
    for line in reversed(_completion_lines(lines)):
        if _line_sha(line) == marker.get("line_sha256"):
            return line
    return None


def open_runs_local(ctx) -> list:
    """The daytime selector: a baseline line with no locally marked,
    protected-verified completion. Reads only the local register."""
    return sorted((r for r in find_runs(ctx)
                   if _baseline_line(r["lines"]) and not local_completion(ctx, r["req"],
                                                                         r["lines"])),
                  key=lambda r: (r["run_key"], r["req"]))


def _baseline_line(lines) -> dict | None:
    for line in lines:
        if line.get("step") == "baseline" and line.get("outcome") == "ok":
            return line
    return None


def find_runs(ctx) -> list:
    """Every purchase run request: dicts with run_key, req, doc, lines."""
    out = []
    for req, doc in _register_requests(ctx):
        run = doc.get("purchase_run")
        if not isinstance(run, dict) or not run.get("run_key"):
            continue
        out.append({"req": req, "doc": doc, "run_key": run["run_key"],
                    "command": run.get("command", "window"),
                    "lines": register_lines(ctx, req)})
    return out


def incomplete_runs(ctx) -> list:
    """The repair selector: every baseline line (writes may have begun) with
    no verified completion record."""
    return sorted((r for r in find_runs(ctx)
                   if _baseline_line(r["lines"]) and not _completion(ctx, r["req"], r["lines"])),
                  key=lambda r: (r["run_key"], r["req"]))


def run_state(ctx, run_key: str) -> tuple:
    """('new'|'open'|'incomplete'|'complete', run or None)."""
    for run in find_runs(ctx):
        if run["run_key"] == run_key:
            if _completion(ctx, run["req"], run["lines"]):
                return "complete", run
            if _baseline_line(run["lines"]):
                return "incomplete", run
            return "open", run
    return "new", None


def made_active(rows: list) -> list:
    """The seats this run made active (finish), from its journal."""
    return sorted({r["subscription_id"] for r in rows if r["step"] == "finish"})


def checks_for(ctx, command: str, rows: list, fresh: dict | None = None) -> dict:
    """The run's step-7 obligations, rebuilt the same way for the normal run
    and for repair: every check, plus `made_active` from the journal."""
    made = made_active(rows)
    checks = run_checks(ctx, fresh, made_active=made)
    if command == "finish":
        checks["complete"] = [s for s in checks["complete"] if s in made]
    elif command != "window":
        checks["complete"] = []
    return checks


class Run:
    """One purchase run: its register request, persisted baseline, writes
    and completion record (design 5.3 restart safety)."""

    def __init__(self, ctx, run_key: str, command: str):
        self.ctx = ctx
        self.run_key = run_key
        self.command = command
        self.req = None
        self.began = False
        self.baseline = None
        self.notes = {"alerts": [], "deferred": [], "sales_close": "not_due",
                      "sync_ok": False, "lapsed": []}

    def folder(self) -> str:
        return os.path.join(self.ctx.layout["register_dir"], self.req)

    def begin(self) -> None:
        """Take and persist the baseline: from here on, writes have begun."""
        from scripts import pilot_inventory as inv
        ctx = self.ctx
        report = inv.build_inventory(ctx.layout, None)
        blocking = _blocking(report)
        if blocking:
            raise Refused("BASELINE_UNRESOLVED", ", ".join(blocking), unresolved=blocking)
        state, run = run_state(ctx, self.run_key)
        if state in ("complete", "incomplete"):
            raise Failed("RUN_EXISTS", f"{self.run_key} is {state}")
        if run is None:
            self.req = create_request(ctx, self.run_key, {
                "kind": "admission_change", "origin": "purchase_window",
                "received_at": _iso(ctx.now()), "principals": [], "workspace": None,
                "purchase_run": {"run_key": self.run_key, "command": self.command}})
        else:
            self.req = run["req"]
        path = os.path.join(self.folder(), "purchase-baseline.json")
        _atomic_bytes(path, _json_bytes(report))
        self.baseline = report
        self.began = True
        record_line(ctx, self.req, "baseline", "ok", [path], {"run_key": self.run_key})

    @classmethod
    def resume(cls, ctx, run: dict) -> "Run":
        self = cls(ctx, run["run_key"], run["command"])
        self.req = run["req"]
        self.began = True
        line = _baseline_line(run["lines"])
        path = os.path.join(self.folder(), "purchase-baseline.json")
        want = {e["name"]: e["sha256"] for e in line.get("evidence", [])}
        from scripts import pilot_inventory as inv
        if not os.path.isfile(path) or want.get("purchase-baseline.json") != inv.sha256_file(path):
            raise Failed("BASELINE_MISSING", f"{run['req']}: the persisted baseline is not"
                         " the recorded one")
        verify_line(ctx, self.req, line)
        self.baseline = _read_json(path)
        if self.command == "window" and ctx.now() >= ctx.close_at:
            # Astra r10 MED-1, r11 MED-1: repair never saw this run's sales
            # close. If the close is due now (the run may have crossed the
            # instant after it started), it is owed, not "not_due"; a
            # same-key retry performs it.
            self.notes["sales_close"] = "unverified"
        return self

    # -- evidence of this run's writes
    def journal(self) -> list:
        from earwitness.app import purchases as _p
        con = self.ctx.control(readonly=True)
        try:
            return _p.journal_rows(con, self.run_key)
        finally:
            con.close()

    def writes(self, rows: list) -> dict:
        """Tables per database this run may have written, from its journal
        (workspace writes are journaled as intents before they happen),
        keyed by the validated inventory targets (R1-04)."""
        from earwitness.app import purchases as _p
        ctx = self.ctx
        targets = check_targets(ctx)
        out = {}
        if rows:
            tables = {"purchase_journal"}
            if any(r["step"] in SEAT_STEPS for r in rows):
                tables.add("purchase_seats")
            if any(r["step"] in REQUEST_STEPS for r in rows):
                tables.add("purchase_cancel_requests")
            out[targets["control"]] = sorted(tables)
        con = ctx.control(readonly=True)
        try:
            for r in rows:
                if r["step"] in WORKSPACE_INTENTS:
                    seat = _p.seat(con, r["subscription_id"])
                    if seat and seat["workspace_id"]:
                        path = targets["workspaces"].get(seat["workspace_id"])
                        if path is None:
                            raise Failed("TARGET_MISMATCH", f"{seat['workspace_id']}: no"
                                         " validated inventory target")
                        out[path] = sorted(set(out.get(path, [])) | set(WORKSPACE_INTENTS[r["step"]]))
        finally:
            con.close()
        return out

    def complete(self, checks: dict, *, rebuilt: bool = False) -> bool:
        """Result, preserve against the persisted baseline, then (both ok)
        the completion record, verified in the protected copy, then the
        release file. True when complete."""
        from scripts import pilot_inventory as inv
        from earwitness.app import purchases as _p
        ctx = self.ctx
        rows = self.journal()
        con = ctx.control(readonly=True)
        try:
            seats = _p.all_seats(con)
        finally:
            con.close()
        result = {
            "format": RESULT_FORMAT, "tool": "app_purchases", "cmd": self.command,
            "run_key": self.run_key, "request_id": self.req, "mode": "commit",
            "outcome": "committed", "rebuilt": rebuilt, "generated_at": _iso(ctx.now()),
            "writes": self.writes(rows),
            "journal": [{k: r[k] for k in ("subscription_id", "step", "outcome", "committed_at")}
                        for r in rows],
            "seats": [{"subscription_id": s["subscription_id"], "offer": s["offer"],
                       "workspace_id": s["workspace_id"], "state": s["state"],
                       "credential_delivered": bool(s.get(_p.CREDENTIAL_COLUMN))}
                      for s in seats],
            "counts": _counts(rows), "checks_ok": bool(checks["ok"]),
            "checks": {"problems": checks["problems"]},
            "made_active": made_active(rows),
            "complete": checks["complete"], "lapsed": checks["lapsed"],
            "deferred": self.notes["deferred"], "alerts": self.notes["alerts"],
            "sync_ok": self.notes["sync_ok"], "sales_close": self.notes["sales_close"]}
        attempt = 1 + sum(1 for n in os.listdir(self.folder())
                          if n.startswith("purchases-result-a"))
        result_path = os.path.join(self.folder(), f"purchases-result-a{attempt}.json")
        result_bytes = _json_bytes(result)
        _atomic_bytes(result_path, result_bytes)
        report = inv.preserve(ctx.layout, self.baseline, [result_path], None, [])
        keep_path = os.path.join(self.folder(), f"purchases-preserve-a{attempt}.json")
        _atomic_bytes(keep_path, _json_bytes(report))
        preserve_ok = _preserved(self.baseline, report)
        values = {"run_key": self.run_key, "checks_ok": "true" if checks["ok"] else "false",
                  "preserve": "ok" if preserve_ok else "failed",
                  "result_sha256": _sha256_bytes(result_bytes)}
        if not (checks["ok"] and preserve_ok):
            if not preserve_ok:
                self.notes["preserve_differences"] = report.get("preserve", {}).get(
                    "differences", []) or report.get("unresolved", [])
            pilot_record(ctx, self.req, "purchases", "failed", [result_path, keep_path], values)
            return False
        line = record_line(ctx, self.req, "purchases", "ok", [result_path, keep_path], values)
        publish_verified(ctx, self.req, line)
        write_release(self.run_key, self.req, line, os.path.basename(result_path), result_bytes)
        return True


def _recorded_result(ctx, req: str, line: dict) -> tuple:
    """(name, exact bytes) of the result a verified completion line names,
    read from the protected copy and checked against the recorded hash."""
    want = (line.get("values") or {}).get("result_sha256")
    for entry in line.get("evidence") or []:
        if entry["name"].startswith("purchases-result-") and entry["sha256"] == want:
            path = os.path.join(ctx.layout["protected_dir"], "register", req, entry["name"])
            with open(path, "rb") as handle:
                data = handle.read()
            if _sha256_bytes(data) == want:
                return entry["name"], data
    raise Failed("RESULT_MISSING", f"{req}: the recorded result is not in the protected copy")


def markers_needed(ctx) -> list:
    """Protected-verified completions whose local marker is missing or
    invalid (read-only)."""
    out = []
    for run in find_runs(ctx):
        if local_completion(ctx, run["req"], run["lines"]) is not None:
            continue
        line = _completion(ctx, run["req"], run["lines"])
        if line is not None:
            out.append((run, line))
    return out


def recover_markers(ctx) -> list:
    """R3-01: under the window lock, rewrite each missing or invalid local
    marker, only from a completion line the protected copy verifies."""
    done = []
    for run, line in markers_needed(ctx):
        publish_verified(ctx, run["req"], line)
        done.append(run["run_key"])
    return done


def release_needed(ctx) -> list:
    """Verified completed runs whose release file is missing or does not
    carry the recorded result (read-only)."""
    out = []
    for run in find_runs(ctx):
        line = _completion(ctx, run["req"], run["lines"])
        if line is None:
            continue
        path = os.path.join(release_dir(), f"{run['run_key']}.json")
        doc = read_release(path) if os.path.exists(path) else None
        if doc is None or doc.get("result_sha256") != line["values"].get("result_sha256") \
                or doc.get("request_id") != run["req"]:
            out.append((run, line))
    return out


def recover_releases(ctx) -> list:
    """Under the window lock: rewrite each missing or invalid release file
    from the exact recorded result bytes. Never marks one eligible."""
    done = []
    for run, line in release_needed(ctx):
        name, data = _recorded_result(ctx, run["req"], line)
        write_release(run["run_key"], run["req"], line, name, data)
        done.append(run["run_key"])
    return done


# Inventory flags that describe what a host holds, not a change: media
# locators in a workspace (media_present) and an owner workspace's registry
# row (owner_db). They block deletion requests (L5p), not the purchase step:
# a baseline may carry them, and preserve passes only when the same flags
# are present after the run and no file or table differs. A clip bound to
# its import input raises no flag. Every other flag (journal_unresolved,
# schema_unpinned, undeclared, stray_copy, registry_mismatch, clip_unbound,
# clip_name_invalid, ...) refuses the baseline by name (exit 3): the
# operator re-imports or removes the clip (pilot_clips.py remove), then reruns.
TOLERATED_FLAGS = frozenset({"media_present", "owner_db"})


def _flags(report: dict) -> set:
    return {(u["code"], u["path"], u.get("detail", "")) for u in report.get("unresolved", [])}


def _blocking(report: dict) -> list:
    return sorted({u["code"] for u in report.get("unresolved", [])} - TOLERATED_FLAGS)


def _preserved(baseline: dict, report: dict) -> bool:
    """preserve passed: no difference, and no flag beyond the baseline's
    own tolerated flags (unchanged, same paths and details)."""
    if report.get("preserve", {}).get("differences"):
        return False
    if report["result"] == "clean":
        return True
    return report["result"] == "unresolved" and not _blocking(report) and \
        _flags(report) == _flags(baseline)


def write_release(run_key: str, req: str, line: dict, name: str, result_bytes: bytes) -> str:
    """Root-only release file outside the bundle (design 5.3 step 9)."""
    folder = release_dir()
    os.makedirs(folder, mode=0o700, exist_ok=True)
    doc = {"format": RELEASE_FORMAT, "run_key": run_key, "request_id": req,
           "register_at": line["at"], "register_step": line["step"],
           "checks_ok": True, "preserve": "ok", "result_name": name,
           "result_sha256": _sha256_bytes(result_bytes),
           "result_text": result_bytes.decode("utf-8")}
    path = os.path.join(folder, f"{run_key}.json")
    _atomic_bytes(path, _json_bytes(doc))
    return path


def read_release(path: str) -> dict | None:
    """A release file that parses, whose bytes match its hash and that
    carries checks_ok and preserve ok; else None."""
    try:
        doc = _read_json(path)
        text = doc["result_text"]
        if doc.get("format") != RELEASE_FORMAT or doc.get("checks_ok") is not True or \
                doc.get("preserve") != "ok" or \
                _sha256_bytes(text.encode("utf-8")) != doc.get("result_sha256"):
            return None
        result = json.loads(text)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not isinstance(result, dict) or result.get("checks_ok") is not True:
        return None
    doc["result"] = result
    return doc


def release_files(run_key: str | None = None) -> list:
    """Release file paths, for one window run key (its own file and the
    finish or retire runs of that night) or all."""
    folder = release_dir()
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json") or ".tmp-" in name:
            continue
        key = name[:-len(".json")]
        if run_key is None or key == run_key or key.startswith(run_key + "+"):
            out.append(os.path.join(folder, name))
    return out


def eligible(path: str, doc: dict) -> bool:
    try:
        marker = _read_json(path + ".eligible")
    except (OSError, ValueError):
        return False
    return isinstance(marker, dict) and marker.get("result_sha256") == doc.get("result_sha256")


# ------------------------------------------------------------ the 7.2 rule

def end_target(ctx, sub: dict, request: dict | None) -> tuple:
    """(target cancel_at, request state) under 7.2: the earliest of the
    current value, the pilot end and a queued request's stored target_end.
    A request whose target_end is not after the current period start is
    late: it adds nothing. A null cancel_at counts as later."""
    candidates = [ctx.pilot_end]
    if sub.get("cancel_at"):
        candidates.append(int(sub["cancel_at"]))
    state = None
    if request and request.get("done_at") is None:
        if int(request["target_end"]) <= int(sub.get("current_period_start") or 0):
            state = "late"
        else:
            state = "apply"
            candidates.append(int(request["target_end"]))
    return min(candidates), state


def enforce_end(ctx, seat: dict, sub: dict, target: int, identity: str = "") -> tuple:
    """Write cancel_at (and the binding) when needed; read back.
    ('ok'|'terminal'|'failed'|'mismatch'|'evidence', resource, reason, field
    or the ProviderEvidenceError).
    Never writes a terminal subscription; an ambiguous write is checked by
    a fresh read, never blindly retried. Both the read before the write and
    the read-back must be this seat's subscription and customer before
    their status or end date counts (Astra r19 MED-1); a mismatch returns
    the field. An end date is accepted only as exact-type evidence
    (`end_well_formed`, Astra r18 MED-1), never the normalizer's coerced
    integer: not before the write, not on the read-back. The workspace
    binding is checked only when `identity` is given (a refund row's
    conflicting binding still gets its end date)."""
    from earwitness.app import billing as _billing
    problem = seat_resource_problem(ctx, seat, sub)
    if problem:
        return "mismatch", problem
    s = sub["id"]
    if sub["status"] in PAID_TERMINAL:
        return "terminal", sub
    if sub.get("end_well_formed") is not True:
        return "failed", "end_date_malformed"
    need_meta = bool(identity) and sub.get("metadata_workspace") != identity
    need_end = not sub.get("cancel_at") or int(sub["cancel_at"]) > target
    if need_meta or need_end:
        try:
            ctx.stripe.update_subscription(
                s, workspace_identity=identity if need_meta else "",
                cancel_at=target if need_end else 0,
                idempotency_key=_idem(s, identity if need_meta else "", target if need_end else 0),
                customer=seat["customer_id"])
        except _billing.ProviderError as exc:
            print(f"app_purchases: write for {s} was ambiguous ({_safe(exc)}); reading back",
                  file=sys.stderr)
    try:
        back = ctx.stripe.get_subscription(s, customer=seat["customer_id"])
    except _billing.ProviderEvidenceError as exc:
        return "evidence", exc
    except _billing.ProviderError as exc:
        return "failed", f"readback_{_safe(exc)}"

    def accepted(outcome):
        # BY3-r18: a read-back counts only as valid evidence for this seat
        # (the adapter checked; checked again for a substituted read).
        try:
            _billing.check_window_subscription(back, s, customer=seat["customer_id"])
        except _billing.ProviderEvidenceError as exc:
            return "evidence", exc
        return outcome, back
    problem = seat_resource_problem(ctx, seat, back)
    if problem:
        return "mismatch", problem
    if back["status"] in PAID_TERMINAL:
        return accepted("terminal")
    if back.get("end_well_formed") is not True:
        return "failed", "readback_malformed"
    if identity and back.get("metadata_workspace") != identity:
        return "failed", "readback_identity"
    if not back.get("cancel_at") or int(back["cancel_at"]) > target:
        return "failed", "readback_end_later"
    return accepted("ok")


# ------------------------------------------------------------- reconcile

def reconcile(ctx, run_key: str, seat: dict, kind: str, resource, tag: str) -> str:
    """Apply a verified resource to the seat's workspace through the
    strict-tenant reconcile (billing.apply_event); '' on success, else why."""
    from earwitness.app import billing as _billing
    from earwitness.app import db as _db
    if kind == "ok":
        # BY3-r18 (Astra r20 MED-1): only valid evidence for this seat ever
        # reaches the workspace; its last verified state stays otherwise.
        try:
            _billing.check_window_subscription(resource or {}, seat["subscription_id"],
                                               customer=seat["customer_id"])
        except _billing.ProviderEvidenceError as exc:
            return f"provider_evidence_invalid:{exc.kind}:{exc.field}"
    con_c = ctx.control(readonly=True)
    try:
        entry = registry_entry(con_c, seat["workspace_id"])
    finally:
        con_c.close()
    path = workspace_path(ctx, entry)
    if not path or not os.path.isfile(path):
        return "workspace database missing"
    identity = billing_identity(entry)
    customer = (resource or {}).get("customer") if kind == "ok" else seat["customer_id"]
    s = seat["subscription_id"]
    event_id = f"ew-window:{run_key}:{s}:{tag}"
    try:
        con = _db.open_existing(path, readonly=False)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        return f"workspace open failed: {exc}"
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        if row is None or row[0] != entry["db_identity"]:
            return "workspace identity differs from the registry"
        if tag == "repair":
            # R3-03: a repair applies the exact resource it read. Its event is
            # keyed on that resource, so a later repair that reads another
            # resource applies it; the same resource, already the latest one
            # applied, is not applied again. Webhook deduplication is unchanged.
            base = event_id + ":" + _sha256_bytes(json.dumps(
                [kind, resource or None], sort_keys=True).encode())[:16]
            last = con.execute("SELECT event_id, status FROM billing_events"
                               " WHERE subscription_id=? ORDER BY rowid DESC LIMIT 1",
                               (s,)).fetchone()
            if last is not None and str(last[0]).startswith(base) and last[1] == "reconciled":
                return ""
            n = con.execute("SELECT COUNT(*) FROM billing_events WHERE event_id=?"
                            " OR event_id LIKE ?", (base, base + ":%")).fetchone()[0]
            event_id = base if n == 0 else f"{base}:{n}"
        event = {"id": event_id, "type": "customer.subscription.updated",
                 "data": {"object": {"id": s, "customer": customer,
                                     "metadata": {"workspace_id": identity}}}}
        with _db.transaction(con, write=True):
            out = _billing.apply_event(con, entry["row_namespace"], event, config=ctx.billing,
                                       tenant=identity, fetched=(kind, resource),
                                       now=ctx.now())
    except _billing.TenantMismatch as exc:
        return f"tenant mismatch: {exc}"
    finally:
        con.close()
    if out.get("status") != "reconciled":
        return f"reconcile {out.get('status')}: {out.get('reason', '')}"[:200]
    return ""


# ----------------------------------------------------------------- checks

def _open_workspace_ro(ctx, workspace_id: str):
    """(con, entry) read-only and immutable (no sidecar is created), or
    (None, reason)."""
    from scripts import pilot_inventory as inv
    con_c = ctx.control(readonly=True)
    try:
        entry = registry_entry(con_c, workspace_id)
    finally:
        con_c.close()
    path = workspace_path(ctx, entry)
    if not path or not os.path.isfile(path):
        return None, "workspace database missing"
    if inv.journal_unresolved(path):
        return None, "workspace journal unresolved"
    con = inv.open_ro(path)
    row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    if row is None or row[0] != entry["db_identity"]:
        con.close()
        return None, "workspace identity differs from the registry"
    return con, entry


def _predicted(status: str, period_end: int, cancel_at: int, past_due_since: int,
               now: int, grace: int) -> bool:
    if cancel_at and cancel_at <= now:
        return False
    if status == "active":
        return period_end > now
    if status == "past_due":
        return period_end > 0 and now - (past_due_since or now) <= grace
    return False


def run_checks(ctx, fresh: dict | None = None, *, made_active=()) -> dict:
    """Step 7. `fresh` maps subscription IDs to this run's verified
    resources; without one the stored billing state is the prediction."""
    from earwitness.app import admission as _admission
    from earwitness.app import billing as _billing
    from earwitness.app import purchases as _p
    fresh = fresh or {}
    problems, lapsed, complete = [], [], []
    policy = ctx.policy
    if policy is None or policy.pools is None:
        problems.append("no purchase_seats block")
    else:
        problems.extend(_admission.purchase_problems(policy, ctx.env))
    comped = set(ctx.billing.comped_workspaces)
    con = ctx.control(readonly=True)
    try:
        seats = _p.all_seats(con)
    finally:
        con.close()
    now = int(ctx.now())
    grace = ctx.billing.past_due_grace_s
    for seat in seats:
        w, s, state = seat["workspace_id"], seat["subscription_id"], seat["state"]
        if not w or state == "retired":
            continue
        if w in comped:
            problems.append(f"{s}: seat holds a comped workspace")
            continue
        if state not in ("active", "ended", "needs_refund"):
            continue
        opened, entry = _open_workspace_ro(ctx, w)
        if opened is None:
            problems.append(f"{s}: {entry}")
            continue
        try:
            ent = _billing.entitlement_status(opened, entry["row_namespace"], ctx.billing,
                                              now=now, tenant=billing_identity(entry))
            stored = opened.execute("SELECT * FROM billing_entitlement WHERE workspace_id=?",
                                    (entry["row_namespace"],)).fetchone()
        except (ValueError, sqlite3.Error) as exc:
            problems.append(f"{s}: billing state unreadable: {exc}")
            continue
        finally:
            opened.close()
        stored = dict(stored) if stored else {}
        if state != "active":
            if state in ("ended", "needs_refund") and ent.get("allowed"):
                problems.append(f"{s}: a {state} seat's workspace is still allowed")
            continue
        # BY3-r21 (Astra r23 MED-1): an active seat's workspace must also be
        # paid-capable (the allocation's predicate). A rights denial keeps
        # the run incomplete; it is never an ordinary lapse.
        con = ctx.control(readonly=True)
        try:
            paid_problem = paid_use_problem(con, entry)
        finally:
            con.close()
        if paid_problem:
            problems.append(f"{s}: the workspace is not paid-capable ({paid_problem})")
            continue
        res = fresh.get(s)
        if res is not None:
            predicted = _predicted(res["status"], int(res.get("current_period_end") or 0),
                                   int(res.get("cancel_at") or 0),
                                   int(stored.get("past_due_since") or 0), now, grace)
        else:
            predicted = _predicted(stored.get("status") or "", int(stored.get(
                "current_period_end") or 0), _billing.stored_cancel_at(stored),
                int(stored.get("past_due_since") or 0), now, grace)
        allowed = bool(ent.get("allowed"))
        if allowed != predicted:
            problems.append(f"{s}: the gate says {'allowed' if allowed else 'refused'},"
                            f" the verified status predicts the other")
            continue
        if s in made_active and not allowed:
            problems.append(f"{s}: a seat made active in this run is refused")
            continue
        if not allowed:
            lapsed.append(s)
        elif not seat.get(_p.CREDENTIAL_COLUMN):
            complete.append(s)
    return {"ok": not problems, "problems": problems, "lapsed": lapsed, "complete": complete}


# ------------------------------------------------------------ the window

class Window:
    """Steps 2-9 of design 5.3, fallback credential mode (9.5)."""

    def __init__(self, ctx, run_key: str, budget_s: int):
        self.ctx = ctx
        self.run = Run(ctx, run_key, "window")
        self.started = ctx.monotonic()
        self.budget_s = budget_s
        self.deferring = False
        self.fresh = {}

    def _point(self, name: str) -> None:
        """A named step boundary (tests interrupt here)."""

    def time_left(self) -> float:
        return self.budget_s - (self.ctx.monotonic() - self.started)

    def next_buyer(self, subscription_id: str) -> bool:
        """Budget rule: under 30 s left, the rest waits for the next night."""
        if not self.deferring and self.time_left() < BUYER_RESERVE_S:
            self.deferring = True
        if self.deferring:
            if subscription_id not in self.run.notes["deferred"]:
                self.run.notes["deferred"].append(subscription_id)
            return False
        return True

    def alert(self, code: str, subscription_id: str = "", **extra) -> None:
        item = {"code": code}
        if subscription_id:
            item["subscription_id"] = subscription_id
        item.update(extra)
        self.run.notes["alerts"].append(item)
        print(json.dumps({"alert": True, **item}), file=sys.stderr)

    def evidence(self, subscription_id: str, exc, **extra) -> None:
        """BY3-r18: a read the adapter refused: the one fixed alert."""
        self.alert("provider_evidence_invalid", subscription_id, kind=exc.kind,
                   field=exc.field, **extra)

    def refund_kept(self, subscription_id: str, reason, evidence=None) -> None:
        """A refund row kept on incomplete or mismatched evidence."""
        if evidence is not None:
            self.evidence(subscription_id, evidence)
        if isinstance(reason, RefundEvidenceMismatch):
            self.alert("refund_evidence_mismatch", subscription_id, field=reason.field)
        else:
            self.alert("refund_read_failed", subscription_id, detail="incomplete_payments")

    def write(self, fn, *args):
        """One control-store transaction (BEGIN IMMEDIATE)."""
        con = self.ctx.control()
        try:
            with _immediate(con):
                return fn(con, *args)
        finally:
            con.close()

    def move(self, con, subscription_id: str, to: str, step: str, outcome: str = "",
             last_error: str = "", *, terminal: bool = False) -> None:
        from earwitness.app import purchases as _p
        _p.transition(con, subscription_id, to, run_key=self.run.run_key,
                      last_error=last_error, now=_iso(self.ctx.now()))
        _p.journal(con, self.run.run_key, subscription_id, step, outcome or to,
                   now=_iso(self.ctx.now()))
        if terminal and to in ("needs_refund", "ended"):
            # LOW-1: a queued cancel request ends with its terminal subscription.
            # A nonterminal needs_refund keeps it (R2-04): end_live applies it.
            request = _p.cancel_request(con, subscription_id)
            if request and request.get("done_at") is None:
                self._close_request(con, subscription_id, "terminal")

    def note_error(self, subscription_id: str, why: str) -> None:
        from earwitness.app import purchases as _p

        def body(con):
            _p.set_last_error(con, subscription_id, why, run_key=self.run.run_key,
                              now=_iso(self.ctx.now()))
            _p.journal(con, self.run.run_key, subscription_id, "error", why[:120],
                       now=_iso(self.ctx.now()))
        self.write(body)

    def intent(self, subscription_id: str, step: str, workspace_id: str) -> None:
        from earwitness.app import purchases as _p
        self.write(lambda con: _p.journal(con, self.run.run_key, subscription_id, step,
                                          workspace_id, now=_iso(self.ctx.now())))

    def seats(self, *states) -> list:
        from earwitness.app import purchases as _p
        con = self.ctx.control(readonly=True)
        try:
            rows = _p.all_seats(con)
        finally:
            con.close()
        return [r for r in rows if not states or r["state"] in states]

    # -- step 2 read part (before the baseline: nothing is written)
    def list_subscriptions(self) -> list | None:
        """Every subscription for the configured prices; shape errors stop
        the step (Failed); a Stripe failure returns None (discovery waits)."""
        from earwitness.app import billing as _billing
        found = {}
        try:
            for offer, prices in self.ctx.offer_prices().items():
                for price in prices:
                    for sub in self.ctx.stripe.list_subscriptions(price):
                        if sub["item_count"] != 1 or sub["quantity"] != 1 or \
                                sub["price_id"] != price or sub["offer"] != offer:
                            raise Failed("SUBSCRIPTION_SHAPE",
                                         f"{sub['id']}: offer, price or quantity is wrong")
                        found[sub["id"]] = dict(sub, _offer=offer)
        except _billing.ProviderEvidenceError as exc:
            # Discovery pauses; the alert names the listed subscription the
            # error is about, when there is one (code review r21 NOTE-1).
            self.alert("provider_evidence_invalid", exc.subscription_id, kind=exc.kind,
                       field=exc.field)
            return None
        except _billing.ProviderError as exc:
            self.alert("stripe_list_failed", detail=_safe(exc))
            return None
        return sorted(found.values(), key=lambda s: (s.get("created", 0), s["id"]))

    # -- step 2
    def discover(self, subs: list) -> None:
        from earwitness.app import billing as _billing
        known = {r["subscription_id"] for r in self.seats()}
        for sub in subs:
            s = sub["id"]
            if s in known or sub["status"] != "active" or not sub.get("latest_invoice"):
                continue
            if not self.next_buyer(s):
                continue
            req, doc = buyer_request(self.ctx, s)
            try:
                # Astra r20 MED-2: the subscription's own latest invoice, of
                # its own customer, and that customer's own email.
                if not paid_latest_invoice(self.ctx, sub, sub["customer"]):
                    continue
                # A recorded buyer is rebuilt from the record; Stripe's current
                # email is never read for it (5.7 item 5).
                email = "" if req else buyer_email(self.ctx, sub["customer"])
            except _billing.ProviderEvidenceError as exc:
                self.evidence(s, exc)  # no buyer record from it
                continue
            except _billing.ProviderError as exc:
                self.alert("stripe_read_failed", s, detail=_safe(exc))
                continue
            self.write(self._discover_one, sub, email, req, doc)
            self._point(f"discover:{s}")

    def _discover_one(self, con, sub: dict, email: str, req, doc) -> None:
        from earwitness.app import local_accounts as _la_mod
        from earwitness.app import purchases as _p
        ctx, s, offer = self.ctx, sub["id"], sub["_offer"]
        if _p.seat(con, s) is not None:
            return
        policy = ctx.policy
        pool = list(policy.pools.get(offer, ()))
        recorded = None
        if req is not None:
            # R1-02: the protected tuple is the only authority for a seat
            # that was recorded before (a seat lost in a restore).
            recorded = recorded_tuple(ctx, doc, req)
            if recorded is None:
                self.alert("record_unverified", s)
            else:
                email = recorded["email"]
        common = dict(subscription_id=s, offer=offer, customer_id=sub["customer"],
                      email=email, issuer=ctx.issuer, run_key=self.run.run_key,
                      now=_iso(ctx.now()), public_base_url=ctx.hosted.public_base_url)

        def refuse(reason: str) -> None:
            _p.refuse(con, reason=reason, **common)
            _p.journal(con, self.run.run_key, s, "discover", f"refused:{reason}",
                       now=_iso(ctx.now()))

        if req is not None and recorded is None:
            return refuse("record_conflict")
        try:
            principal = _la_mod.principal(ctx.issuer, email)
        except _la_mod.EmailError:
            return refuse("email_rule")
        if principal in policy.removed_principals:
            return refuse("removed_principal")
        if principal in policy.principals:
            return refuse("admitted_principal")
        if _p.holding_seat(con, principal) is not None:
            return refuse("duplicate")
        if recorded is not None:
            workspace = recorded["workspace_id"]
            entry = registry_entry(con, workspace)
            bound = sub.get("metadata_workspace") or ""
            identity = f"{workspace}:{recorded['db_identity']}"
            if recorded["subscription_id"] != s or recorded["principal"] != principal \
                    or recorded["customer_id"] != sub["customer"] or recorded["offer"] != offer \
                    or workspace not in pool or entry is None \
                    or entry["db_identity"] != recorded["db_identity"] \
                    or _p.seat_for_workspace(con, workspace) is not None \
                    or (bound and bound != identity):
                self.alert("record_conflict", s)
                return refuse("record_conflict")
        elif sub.get("metadata_workspace"):
            # A binding with no protected record cannot be trusted (5.7 item 4).
            self.alert("bound_elsewhere", s)
            return refuse("bound_elsewhere")
        else:
            workspace = self._allocate(con, offer, pool)
            if workspace is None:
                return refuse("oversell")
        try:
            _p.reserve(con, workspace_id=workspace, **common)
        except _p.SeatError as exc:
            # The backstop: reserve refuses a workspace that is not
            # paid-capable and writes nothing. An allocation refusal, as an
            # oversell is: no workspace held, the run goes on (BY3-r20).
            if not str(exc).startswith(RESERVE_NOT_PAID_CAPABLE):
                raise
            self.alert("reserve_refused", s, workspace=workspace)
            return refuse("not_paid_capable")
        _p.journal(con, self.run.run_key, s, "discover", f"reserved:{workspace}",
                   now=_iso(ctx.now()))

    def _allocate(self, con, offer: str, pool: list) -> str | None:
        from earwitness.app import purchases as _p
        ctx = self.ctx
        retired = ctx.policy.retired_workspace_ids
        comped = set(ctx.billing.comped_workspaces)
        for workspace in sorted(pool, key=_natural):
            if workspace in retired or workspace in comped or \
                    _p.seat_for_workspace(con, workspace) is not None:
                continue
            entry = registry_entry(con, workspace)
            if entry is None:
                continue
            # BY3-r20: only a paid-capable record serves a buyer; an
            # unreadable or edited one is not paid-capable.
            reason = paid_use_problem(con, entry)
            if reason:
                self.alert("workspace_not_paid_capable", workspace=workspace, reason=reason)
                continue
            problem = rights_problem(ctx, con, offer, entry)
            if problem:
                self.alert("rights_mismatch", workspace=workspace, detail=problem)
                continue
            return workspace
        return None

    # -- step 3
    def record(self) -> None:
        ctx = self.ctx
        for seat in self.seats("reserved"):
            s = seat["subscription_id"]
            if not self.next_buyer(s):
                continue
            con = ctx.control(readonly=True)
            try:
                entry = registry_entry(con, seat["workspace_id"])
            finally:
                con.close()
            if entry is None:
                self.note_error(s, "workspace_unregistered")
                continue
            req, doc = buyer_request(ctx, s)
            if req is not None and not request_matches(doc, seat, entry):
                # R1-02: never reuse a request that names anything else.
                self.alert("record_conflict", s)
                self.note_error(s, "record_conflict")
                continue
            try:
                if req is None:
                    req = create_request(ctx, self.run.run_key, {
                        "kind": "admission_change", "origin": "purchase",
                        "received_at": _iso(ctx.now()),
                        "authentication": {"basis": "stripe_payment", "message_reference": s,
                                           "claimed_email": None, "operator": "app_purchases"},
                        "workspace": {"workspace_id": seat["workspace_id"],
                                      "db_identity": entry["db_identity"]},
                        "principals": [{"principal": seat["principal"], "email": seat["email"]}],
                        "purchase": {"subscription_id": s, "offer": seat["offer"],
                                     "customer_id": seat["customer_id"],
                                     "run_key": self.run.run_key}})
                self._point(f"record_request:{s}")
                lines = _record_lines(ctx, req)
                # R1-01: an interrupted record is resumed (mirrored and
                # verified), never appended a second time.
                record_line(ctx, req, "record", "ok", [], {
                    "status": "recorded", "subscription_id": s,
                    "workspace_id": seat["workspace_id"],
                    "db_identity": entry["db_identity"], "offer": seat["offer"],
                    "run_key": self.run.run_key}, existing=lines[-1] if lines else None)
                self._point(f"record_logged:{s}")
            except (Failed, OSError) as exc:
                self.alert("record_failed", s, detail=_describe(exc)[:120])
                continue
            self.write(lambda con: self.move(con, s, "recorded", "record", f"recorded:{req}"))
            self._point(f"record:{s}")

    # -- step 4
    def bind(self) -> None:
        from earwitness.app import billing as _billing
        from earwitness.app import purchases as _p
        ctx = self.ctx
        for seat in self.seats("recorded"):
            s = seat["subscription_id"]
            if not self.next_buyer(s):
                continue
            try:
                sub = ctx.stripe.get_subscription(s, customer=seat["customer_id"])
                paid = paid_latest_invoice(ctx, sub, seat["customer_id"])
            except _billing.ProviderEvidenceError as exc:
                self.evidence(s, exc)
                self.note_error(s, f"provider_evidence_invalid:{exc.kind}:{exc.field}")
                continue
            except _billing.ProviderError as exc:
                self.note_error(s, f"stripe_read_failed:{_safe(exc)}")
                continue
            con = ctx.control(readonly=True)
            try:
                entry = registry_entry(con, seat["workspace_id"])
                request = _p.cancel_request(con, s)
            finally:
                con.close()
            identity = billing_identity(entry)
            # Astra r19 MED-1: no status or binding counts before the
            # resource is this seat's subscription and customer.
            problem = seat_resource_problem(ctx, seat, sub)
            if problem:
                self.mismatch(s, problem)
                continue
            if sub["status"] in SETUP_STOP:
                self.refund_move(seat, sub, identity, f"needs_refund:{sub['status']}",
                                 terminal=sub["status"] in PAID_TERMINAL)
                continue
            if sub.get("metadata_workspace") and sub["metadata_workspace"] != identity:
                self.alert("metadata_conflict", s)
                self.refund_move(seat, sub, identity, "needs_refund:metadata_conflict")
                continue
            problem = seat_resource_problem(ctx, seat, sub, identity, allow_unbound=True)
            if problem:
                self.mismatch(s, problem)
                continue
            if sub["status"] != "active" or not paid:
                self.note_error(s, "not_active_paid")
                continue
            target, req_state = end_target(ctx, sub, request)
            outcome, back = enforce_end(ctx, seat, sub, target, identity)
            self._point(f"bind_stripe:{s}")
            if outcome == "mismatch":
                self.mismatch(s, back)
                continue
            if outcome == "evidence":
                self.evidence(s, back)
                self.note_error(s, f"provider_evidence_invalid:{back.kind}:{back.field}")
                continue
            if outcome == "terminal":
                self.refund_move(seat, back, identity, "needs_refund:terminal", terminal=True)
                continue
            if outcome != "ok":
                self.note_error(s, back)
                continue
            problem = seat_resource_problem(ctx, seat, back, identity)
            if problem:
                self.mismatch(s, problem)
                continue
            if req_state == "late":  # only on accepted evidence (code r19 MED-1)
                self.alert("cancel_request_late", s)
            self.fresh[s] = back
            self.intent(s, "bind_intent", seat["workspace_id"])
            why = reconcile(ctx, self.run.run_key, seat, "ok", back, "bind")
            self._point(f"bind_reconcile:{s}")
            if why:
                self.alert("reconcile_failed", s, detail=why)
                self.note_error(s, why)
                continue

            def bound(con):
                self.move(con, s, "bound", "bind", "bound")
                self._close_request(con, s, req_state)
            self.write(bound)
            self._point(f"bind:{s}")

    def refund_move(self, seat: dict, sub: dict, identity: str, outcome: str, *,
                    terminal: bool = False) -> None:
        """Bind's moves to needs_refund (code r9 MED-1). If this seat's
        workspace may already hold a paid state (Stripe names its workspace,
        or an earlier bind journaled an intent for it), the workspace first
        takes the resource just read, with the intent journaled before the
        write, so a needs_refund workspace is never left allowed. No Stripe
        write. If that reconcile fails, the seat is not moved and alerts."""
        s = seat["subscription_id"]
        if sub.get("metadata_workspace") == identity or _bind_touched(self.ctx, s):
            self.intent(s, "bind_intent", seat["workspace_id"])
            why = reconcile(self.ctx, self.run.run_key, seat, "ok", sub, "bind")
            if why:
                self.alert("reconcile_failed", s, detail=why)
                self.note_error(s, why)
                return
        self.write(lambda con: self.move(con, s, "needs_refund", "bind", outcome,
                                         terminal=terminal))

    def mismatch(self, s: str, field: str) -> None:
        """R1-03: a resource that is not this seat's: alert, change nothing."""
        self.alert("seat_resource_mismatch", s, field=field)
        self.note_error(s, f"resource_mismatch:{field}")

    def _close_request(self, con, s: str, req_state: str | None) -> None:
        from earwitness.app import purchases as _p
        outcome = {"apply": "applied", "late": "late", "terminal": "terminal"}.get(req_state)
        if outcome:
            _p.close_cancel_request(con, s, outcome, now=_iso(self.ctx.now()))
            _p.journal(con, self.run.run_key, s, "request", outcome, now=_iso(self.ctx.now()))

    # -- step 5
    def sync(self) -> bool:
        from earwitness.app import billing as _billing
        from earwitness.app import purchases as _p
        ctx = self.ctx
        reads_ok = True
        for seat in self.seats("bound", "awaiting_account", "active"):
            s = seat["subscription_id"]
            if not self.next_buyer(s):
                reads_ok = False
                continue
            con = ctx.control(readonly=True)
            try:
                request = _p.cancel_request(con, s)
                entry = registry_entry(con, seat["workspace_id"])
            finally:
                con.close()
            identity = billing_identity(entry) if entry else ""
            try:
                sub = ctx.stripe.get_subscription(s, customer=seat["customer_id"])
                kind = "ok"
            except _billing.ProviderEvidenceError as exc:
                # BY3-r18 (Astra r20 MED-1): the seat and its workspace keep
                # their last verified state; a queued request stays queued.
                reads_ok = False
                self.evidence(s, exc)
                continue
            except _billing.ProviderError as exc:
                # Code r7 MED-1: "not found" proves nothing about the buyer
                # (Stripe keeps canceled subscriptions; the usual cause is a
                # window key of the wrong mode or account). Like any
                # unreadable read: the seat and its workspace stay as they are.
                reads_ok = False
                self.alert(_read_alert(exc), s, detail=_safe(exc))
                continue
            if kind == "ok":
                problem = seat_resource_problem(ctx, seat, sub, identity)
                if problem:
                    reads_ok = False
                    self.mismatch(s, problem)
                    continue
            terminal = sub["status"] in PAID_TERMINAL  # a verified status only
            req_state = None
            if not terminal:
                target, req_state = end_target(ctx, sub, request)
                outcome, back = enforce_end(ctx, seat, sub, target)
                self._point(f"sync_stripe:{s}")
                if outcome == "mismatch":
                    reads_ok = False
                    self.mismatch(s, back)
                    continue
                if outcome == "evidence":
                    reads_ok = False
                    self.evidence(s, back)
                    continue
                if outcome == "ok":
                    problem = seat_resource_problem(ctx, seat, back, identity)
                    if problem:
                        reads_ok = False
                        self.mismatch(s, problem)
                        continue
                    sub = back
                elif outcome == "terminal":
                    sub, terminal = back, True
                    req_state = None
                else:
                    reads_ok = False
                    self.alert("end_date_write_failed", s, detail=back)
                    # Code r19 MED-1: a late verdict stands only on exact-type
                    # evidence; a malformed period start leaves it queued.
                    req_state = "late" if req_state == "late" and \
                        sub.get("end_well_formed") is True else None
            # BY3-r18 (Astra r20 MED-1): no late verdict, gate prediction or
            # reconcile from a resource that is not valid evidence for this
            # seat (the adapter checked; checked again for a substituted read).
            try:
                _billing.check_window_subscription(sub, s, customer=seat["customer_id"])
            except _billing.ProviderEvidenceError as exc:
                reads_ok = False
                self.evidence(s, exc)
                continue
            if req_state == "late":
                self.alert("cancel_request_late", s)
            if terminal and request and request.get("done_at") is None:
                req_state = "terminal"
            if sub is not None:
                self.fresh[s] = sub
            self.intent(s, "sync_intent", seat["workspace_id"])
            why = reconcile(ctx, self.run.run_key, seat, kind, sub if kind == "ok" else "",
                            "sync")
            self._point(f"sync_reconcile:{s}")
            if why:
                reads_ok = False
                self.alert("reconcile_failed", s, detail=why)

            def after(con, seat=seat, s=s, terminal=terminal, req_state=req_state):
                self._close_request(con, s, req_state)
                if terminal and seat["state"] == "active":
                    self.move(con, s, "ended", "ended", "ended", terminal=True)
                elif terminal and seat["state"] in ("bound", "awaiting_account"):
                    # Not admitted yet (5.1): refunded like the first three.
                    self.move(con, s, "needs_refund", "refund_needed", "needs_refund:terminal",
                              terminal=True)
            if req_state or terminal:
                self.write(after)
            self._point(f"sync:{s}")
        # Refund-tracked rows: a control-only read, no workspace is opened.
        for seat in self.seats("needs_refund", "refused"):
            s = seat["subscription_id"]
            if not self.next_buyer(s):
                reads_ok = False
                continue
            try:
                sub = ctx.stripe.get_subscription(s, customer=seat["customer_id"])
            except _billing.ProviderEvidenceError as exc:
                reads_ok = False
                self.refund_kept(s, _refund_reason(exc), exc)
                continue
            except _billing.ProviderError as exc:
                reads_ok = False
                self.alert("refund_read_failed" if not isinstance(exc, _billing.ProviderNotFound)
                           else _read_alert(exc), s, detail=_safe(exc))
                continue
            if sub["status"] not in PAID_TERMINAL:
                # The end date is gated only on what it reads (code review
                # r17 NOTE-1): a malformed id, status or date is no evidence
                # either way (Astra r16 MED-1). A malformed refund-only field
                # (latest invoice) never skips the write and still alerts,
                # after it. An identity field that is not the seat's (a
                # malformed or foreign customer) stops the write in end_live
                # with seat_resource_mismatch (Astra r18 LOW-1).
                if sub.get("end_well_formed") is not True:
                    reads_ok = False
                    self.alert("refund_read_failed", s, detail="incomplete_payments")
                    continue
                if seat["state"] == "needs_refund" and not self.end_live(seat, sub):
                    reads_ok = False
                if sub.get("well_formed") is not True:
                    reads_ok = False
                    self.alert("refund_read_failed", s, detail="incomplete_payments")
                continue
            try:
                charges = refund_evidence(ctx.stripe, seat, sub)
            except (RefundEvidenceMismatch, RefundEvidenceIncomplete) as exc:
                reads_ok = False
                cause = exc.__cause__
                self.refund_kept(s, exc, cause if isinstance(
                    cause, _billing.ProviderEvidenceError) else None)
                continue
            except _billing.ProviderError as exc:
                # Any refund-step read, a 404 included, is a refund read
                # failure (code review r14 NOTE-1); the row stays.
                reads_ok = False
                self.alert("refund_read_failed", s, detail=_safe(exc))
                continue
            if charges and all(_billing.charge_fully_refunded(c) for c in charges):
                self.write(lambda con, s=s: self.move(con, s, "ended", "refund_ended", "ended",
                                                      terminal=True))
                self._point(f"refund:{s}")
        # R4-02: a request queued for a seat that has already ended, retired
        # or was refused can never apply; it closes terminal, journaled.
        con = ctx.control(readonly=True)
        try:
            states = {r["subscription_id"]: r["state"] for r in _p.all_seats(con)}
            stranded = [r["subscription_id"] for r in _p.queued_cancel_requests(con)
                        if states.get(r["subscription_id"]) in ("ended", "retired", "refused")]
        finally:
            con.close()
        for s in stranded:
            self.write(lambda con, s=s: self._close_request(con, s, "terminal"))
        if reads_ok and not self.deferring:
            self.write(lambda con: _p.journal(con, self.run.run_key, "", "sync", "ok",
                                              now=_iso(self.ctx.now())))
            self.run.notes["sync_ok"] = True
        return reads_ok

    def end_live(self, seat: dict, sub: dict) -> bool:
        """LOW-3 and R2-04: a needs_refund seat whose subscription still lives
        gets the 7.2 end date from the stored request (a Stripe write only;
        no workspace is opened). A request wrongly closed as terminal while
        the subscription lives is taken up again and closed with its real
        outcome (applied or late)."""
        from earwitness.app import purchases as _p
        s = seat["subscription_id"]
        problem = seat_resource_problem(self.ctx, seat, sub)
        if problem:
            self.mismatch(s, problem)
            return False
        con = self.ctx.control(readonly=True)
        try:
            request = _p.cancel_request(con, s)
        finally:
            con.close()
        reopened = bool(request and request.get("outcome") == "terminal")
        if reopened:
            request = dict(request, done_at=None, outcome="")
        target, req_state = end_target(self.ctx, sub, request)
        # No workspace identity: a refund row's conflicting binding still
        # gets its end date; the read-back must still be this seat's.
        outcome, back = enforce_end(self.ctx, seat, sub, target)
        if outcome == "mismatch":
            self.mismatch(s, back)
            return False
        if outcome == "evidence":
            self.evidence(s, back)
            return False
        if outcome == "failed":
            self.alert("end_date_write_failed", s, detail=back)
            return False
        if outcome == "ok" and req_state:
            final = "applied" if req_state == "apply" else "late"
            if final == "late":
                self.alert("cancel_request_late", s)

            def close(con):
                if reopened:
                    _p.set_cancel_outcome(con, s, final, now=_iso(self.ctx.now()))
                    _p.journal(con, self.run.run_key, s, "request", final,
                               now=_iso(self.ctx.now()))
                else:
                    self._close_request(con, s, req_state)
            self.write(close)
        return True

    # -- step 6 (fallback, 9.5 step 1)
    def accounts(self) -> None:
        for seat in self.seats("bound"):
            s = seat["subscription_id"]
            if not self.next_buyer(s):
                continue
            self.write(lambda con, s=s: self.move(con, s, "awaiting_account", "accounts",
                                                  "awaiting_account"))
            self._point(f"accounts:{s}")

    # -- step 8
    def sales_close(self) -> None:
        """Every configured link is attempted and read back. Any link left
        active or unreadable makes the step a failed outcome (R1-08)."""
        from earwitness.app import billing as _billing
        ctx = self.ctx
        if ctx.now() < ctx.close_at:
            self.run.notes["sales_close"] = "not_due"
            return
        failed = []
        for offer in ctx.missing_links():
            # Code r9 MED-2: a sold offer with no link cannot be closed here.
            failed.append(offer)
            self.alert("sales_close_unconfigured", offer=offer)
        for offer, link in sorted(ctx.links.items()):
            try:
                if ctx.stripe.get_payment_link(link)["active"]:
                    try:
                        ctx.stripe.update_payment_link(link, active=False,
                                                       idempotency_key=_idem(link, "inactive"))
                    except _billing.ProviderError as exc:
                        print(f"app_purchases: closing {link} was ambiguous ({_safe(exc)})",
                              file=sys.stderr)
                    if ctx.stripe.get_payment_link(link)["active"]:
                        failed.append(offer)
                        self.alert("sales_close_failed", link=link, detail="still_active")
            except _billing.ProviderEvidenceError as exc:
                failed.append(offer)
                self.alert("provider_evidence_invalid", link=link, kind=exc.kind, field=exc.field)
            except _billing.ProviderError as exc:
                failed.append(offer)
                self.alert("sales_close_failed", link=link, detail=_safe(exc))
        self.run.notes["sales_close"] = "failed" if failed else "closed"

    def pool_check(self) -> None:
        """BY3-r20: one alert per pool workspace that is not paid-capable."""
        con = self.ctx.control(readonly=True)
        try:
            found = pool_paid_problems(self.ctx, con)
        finally:
            con.close()
        for workspace, reason in found:
            self.alert("pool_workspace_not_paid_capable", workspace=workspace, reason=reason)

    def run_steps(self, subs) -> int:
        self._point("begun")
        self.pool_check()
        if subs is not None:
            self.discover(subs)
        self._point("discovered")
        self.record()
        self._point("recorded")
        self.bind()
        self._point("bound")
        self.sync()
        self._point("synced")
        self.accounts()
        self._point("accounts")
        checks = checks_for(self.ctx, "window", self.run.journal(), self.fresh)
        self.run.notes["lapsed"] = checks["lapsed"]
        self._point("checked")
        self.sales_close()
        self._point("sales_closed")
        if not self.run.complete(checks):
            raise Failed("CHECKS" if not checks["ok"] else "PRESERVE",
                         "; ".join(checks["problems"])[:300] if not checks["ok"]
                         else json.dumps(self.run.notes.get("preserve_differences"))[:300])
        self._point("completed")
        if self.run.notes["sales_close"] == "failed":
            return EXIT_FAILED  # evidence complete; the mandatory close is not
        return EXIT_DEFERRED if self.deferring else EXIT_OK


# ---------------------------------------------------------------- commands

def _lock(args) -> None:
    try:
        window_lock.require(args.window_lock_fd)
    except window_lock.NoWindowLock as exc:
        raise Refused(window_lock.NO_WINDOW_LOCK, str(exc)) from None


@contextlib.contextmanager
def _web_lock(ctx):
    from earwitness.app import health as _health
    try:
        handle = _health.acquire_single_process_lock(ctx.grants_db)
    except _health.ConfigError:
        raise Refused("WEB_RUNNING", "the web process holds the control store") from None
    try:
        yield
    finally:
        handle.close()


def _check_schema(ctx) -> None:
    """The control store has the purchase tables (connections.GrantsStore
    creates them) and its schema equals the layout's pin. The window never
    creates or alters a table: a store from before the tables (an upgrade
    whose app has not started yet) or a stale pin is one refusal, before
    any write. The operator re-pins as DEPLOY (pilot-lifecycle.md 3.5.1)."""
    from scripts import pilot_inventory as inv
    from earwitness.app import purchases as _p
    con = ctx.control(readonly=True)
    try:
        missing = _p.missing_tables(con)
        digest = inv.schema_digest(con)
    finally:
        con.close()
    if missing:
        raise Refused("SCHEMA_UNPINNED", "the control store lacks the purchase tables; start"
                      " this release's app once (it creates them), then re-pin the layout as"
                      " DEPLOY 3.5.1", missing_tables=missing, schema_digest=digest)
    if digest != ctx.layout["schema_pins"]["control"]:
        raise Refused("SCHEMA_UNPINNED", "the control schema differs from the layout pin;"
                      " re-pin the layout from MANIFEST.json as DEPLOY 3.5.1",
                      schema_digest=digest)


def repair_all(ctx) -> list:
    """Result repair of every incomplete run, then release-file recovery for
    every verified completed run. Returns the run keys still incomplete."""
    failed = []
    for found in incomplete_runs(ctx):
        pending = _completion_lines(found["lines"])
        if pending:
            # An interrupted completion record (R1-01): resume its protected
            # copy; never append a second completion.
            try:
                verify_line(ctx, found["req"], pending[-1])
                publish_verified(ctx, found["req"], pending[-1])
            except Failed:
                failed.append(found["run_key"])
            continue
        run = Run.resume(ctx, found)
        try:
            # R2-01: the prediction is Stripe's verified state again, never
            # the workspace row it checks; the run's failed reconcile is redone.
            fresh = reverify(ctx, run, run.journal())
        except Failed as exc:
            print(f"app_purchases: repair of {run.run_key} cannot verify: {exc}",
                  file=sys.stderr)
            failed.append(found["run_key"])
            continue
        # R1-06: the same obligations as the normal run, made_active included.
        checks = checks_for(ctx, run.command, run.journal(), fresh)
        if not run.complete(checks, rebuilt=True):
            failed.append(found["run_key"])
    recover_markers(ctx)
    recover_releases(ctx)
    return failed


def reverify(ctx, run, rows: list) -> dict:
    """Repair's verified prediction (R2-01), per seat the way the window
    does it (code r3 HIGH-1, owner decision):
    - a seat this run touched (a workspace intent in its journal), whatever
      its state now (R3-02), is read from Stripe, checked and reconciled
      again; any failure keeps the run incomplete (fail closed);
    - an untouched `active` seat that reads and checks cleanly gets a
      `sync_intent` journaled first, then the same reconcile;
    - an untouched seat Stripe cannot read, or that does not check, keeps
      its stored state with an alert, as the window does.
    Retired seats are never opened; no Stripe write is ever made here.
    Returns the fresh resources of the `active` seats it verified."""
    from earwitness.app import billing as _billing
    from earwitness.app import purchases as _p
    touched = {r["subscription_id"] for r in rows if r["step"] in WORKSPACE_INTENTS}
    con = ctx.control(readonly=True)
    try:
        seats = [s for s in _p.all_seats(con) if s["workspace_id"] and s["state"] != "retired"
                 and (s["subscription_id"] in touched or s["state"] == "active")]
        entries = {s["workspace_id"]: registry_entry(con, s["workspace_id"]) for s in seats}
    finally:
        con.close()

    def alert(code, s, **extra):
        item = dict({"code": code, "subscription_id": s}, **extra)
        run.notes["alerts"].append(item)
        print(json.dumps({"alert": True, **item}), file=sys.stderr)

    fresh = {}
    for seat in seats:
        s = seat["subscription_id"]
        strict = s in touched
        entry = entries.get(seat["workspace_id"])
        if entry is None:
            if strict:
                raise Failed("TARGET_MISMATCH", f"{s}: workspace not registered")
            alert("workspace_unregistered", s)
            continue
        try:
            sub, kind = ctx.stripe.get_subscription(s, customer=seat["customer_id"]), "ok"
            # BY3-r18 (Astra r20 MED-1): checked again (a substituted read)
            # before anything is journaled or reconciled.
            _billing.check_window_subscription(sub, s, customer=seat["customer_id"])
        except _billing.ProviderEvidenceError as exc:
            # A touched seat stays incomplete until the evidence is valid; an
            # untouched one keeps its state with the alert, as the window does.
            if strict:
                raise Failed("PROVIDER_EVIDENCE_INVALID", f"{s}: {exc.kind}:{exc.field}") from None
            alert("provider_evidence_invalid", s, kind=exc.kind, field=exc.field)
            continue
        except _billing.ProviderError as exc:
            # "not found" is unreadable here too (code r7 MED-1): a touched
            # seat fails closed, an untouched one keeps its state and alerts.
            if strict:
                raise Failed("STRIPE_UNREADABLE", f"{s}: {_safe(exc)}") from None
            alert(_read_alert(exc), s, detail=_safe(exc))
            continue
        if kind == "ok":
            problem = seat_resource_problem(ctx, seat, sub, billing_identity(entry))
            if problem:
                if strict:
                    raise Failed("RESOURCE_MISMATCH", f"{s}: {problem}")
                alert("seat_resource_mismatch", s, field=problem)
                continue
        _mini_point("repair:after_read")
        if not strict:
            wcon = ctx.control()
            try:
                with _immediate(wcon):
                    _p.journal(wcon, run.run_key, s, "sync_intent", seat["workspace_id"],
                               now=_iso(ctx.now()))
            finally:
                wcon.close()
        why = reconcile(ctx, run.run_key, seat, kind, sub if kind == "ok" else "", "repair")
        if why:
            raise Failed("RECONCILE", f"{s}: reconcile failed")
        if sub["status"] in PAID_TERMINAL:  # a verified status only
            # R4-01: the workspace now denies it; its control state follows,
            # in one transaction with its journal entry, under this run.
            seat = _resolve_terminal(ctx, run.run_key, seat)
        if seat["state"] == "active":
            fresh[s] = sub if kind == "ok" else {"status": "canceled", "current_period_end": 0,
                                                  "cancel_at": 0}
    return fresh


def _bind_touched(ctx, subscription_id: str) -> bool:
    """An earlier bind journaled a workspace intent for this seat (any run)."""
    con = ctx.control(readonly=True)
    try:
        return con.execute("SELECT 1 FROM purchase_journal WHERE subscription_id=?"
                           " AND step='bind_intent' LIMIT 1",
                           (subscription_id,)).fetchone() is not None
    finally:
        con.close()


def _resolve_terminal(ctx, run_key: str, seat: dict) -> dict:
    """A verified terminal subscription during repair: an `active` seat
    ends, a `bound` or `awaiting_account` one needs a refund, exactly as the
    window's sync moves them, and a queued cancel request closes terminal.
    No Stripe write. Returns the seat as it is now."""
    from earwitness.app import purchases as _p
    s = seat["subscription_id"]
    moves = {"active": ("ended", "ended", "ended"),
             "bound": ("needs_refund", "refund_needed", "needs_refund:terminal"),
             "awaiting_account": ("needs_refund", "refund_needed", "needs_refund:terminal")}
    con = ctx.control()
    try:
        with _immediate(con):
            current = _p.seat(con, s)
            if current is None or current["state"] not in moves:
                return current or seat
            to, step, outcome = moves[current["state"]]
            now = _iso(ctx.now())
            _p.transition(con, s, to, run_key=run_key, now=now)
            _p.journal(con, run_key, s, step, outcome, now=now)
            request = _p.cancel_request(con, s)
            if request and request.get("done_at") is None:
                _p.close_cancel_request(con, s, "terminal", now=now)
                _p.journal(con, run_key, s, "request", "terminal", now=now)
            return _p.seat(con, s)
    finally:
        con.close()


def _repair_first(ctx, out: dict) -> None:
    """Result repair before any new work: every incomplete run must reach
    its completion record, or the command stops with exit 5."""
    try:
        still = repair_all(ctx)
    except Exception as exc:  # noqa: BLE001 - an incomplete run keeps the site down
        raise _AfterWrites("REPAIR", _describe(exc), out) from None
    if still:
        raise _AfterWrites("REPAIR", f"incomplete runs remain: {', '.join(still)}", out)


def _begin(ctx, run, out: dict) -> None:
    """Persist the baseline. A failure after the baseline line was written
    is a failure after writes (the run is now incomplete)."""
    try:
        run.begin()
    except (Failed, OSError) as exc:
        state, _found = run_state(ctx, run.run_key)
        if state == "incomplete":
            raise _AfterWrites("BASELINE", _describe(exc), out) from None
        raise


def _run_key(value: str) -> str:
    if not RUN_KEY_RE.match(value or ""):
        raise Refused("USAGE", "--run-key must be YYYY-MM-DD")
    return value


def _la_date(ctx) -> str:
    return dt.datetime.fromtimestamp(ctx.now(), dt.timezone.utc).astimezone(_la()).date().isoformat()


def cmd_pending(ctx, args) -> tuple:
    from earwitness.app import billing as _billing
    from earwitness.app import purchases as _p
    out = {"cmd": "pending", "run_key": args.run_key, "code": "ok", "pending": False,
           "reasons": [], "estimate_s": 0}
    ok, why = ctx.configured()
    if why == LINKS_MISSING:
        # Not "nothing to do": the window must run so it refuses and alerts.
        out.update(pending=True, reasons=[LINKS_MISSING], estimate_s=ESTIMATE_BASE_S)
        return EXIT_OK, out
    if not ok:
        out["reasons"] = ["not_configured"]
        return EXIT_OK, out
    try:
        con = ctx.control(readonly=True)
        try:
            seats = _p.all_seats(con)
            queued = _p.queued_cancel_requests(con)
            last_sync = _p.last_sync_ok(con)
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise Failed("CONTROL_UNREADABLE", str(exc)) from None
    reasons = set()
    known = {s["subscription_id"] for s in seats}
    to_set_up = sum(1 for s in seats if s["state"] in ("reserved", "recorded", "bound"))
    try:
        for offer, prices in ctx.offer_prices().items():
            for price in prices:
                for sub in ctx.stripe.list_subscriptions(price):
                    if sub["id"] in known or sub["status"] != "active" or \
                            not sub.get("latest_invoice"):
                        continue
                    if paid_latest_invoice(ctx, sub, sub["customer"]):
                        reasons.add("unseated_paid")
                        known.add(sub["id"])
                        to_set_up += 1
    except _billing.ProviderError:
        reasons.add("stripe_unreadable")
    for s in seats:
        if s["state"] in ("reserved", "recorded", "bound", "awaiting_account",
                          "needs_refund", "refused"):
            reasons.add("seat_unfinished")
        elif s["state"] == "active" and not s.get(_p.CREDENTIAL_COLUMN):
            reasons.add("credential_undelivered")
    if queued:
        reasons.add("cancel_request")
    if ctx.now() >= ctx.close_at and ctx.missing_links():
        reasons.add("sales_close_unconfigured")
    if ctx.now() >= ctx.close_at and ctx.links:
        try:
            if any(ctx.stripe.get_payment_link(link)["active"] for link in ctx.links.values()):
                reasons.add("sales_close_due")
        except _billing.ProviderError:
            reasons.add("stripe_unreadable")
    if any(s["state"] == "active" for s in seats):
        at = _parse_iso(last_sync) if last_sync else None
        if at is None or ctx.now() - at > SYNC_DUE_S:
            reasons.add("sync_due")
    out["reasons"] = sorted(reasons)
    out["pending"] = bool(reasons)
    out["estimate_s"] = ESTIMATE_BASE_S + ESTIMATE_PER_BUYER_S * to_set_up if reasons else 0
    return EXIT_OK, out


def _window_out(cmd: str, run_key: str) -> dict:
    return {"cmd": cmd, "run_key": run_key, "code": "ok", "counts": {}, "deferred": []}


def cmd_window(ctx, args) -> tuple:
    out = _window_out("window", args.run_key)
    _lock(args)
    _run_key(args.run_key)
    ctx.require_configured()
    ctx.require_fallback()
    check_targets(ctx)  # R1-04: before any write, repair included
    with _web_lock(ctx):
        window = Window(ctx, args.run_key, args.budget_s)
        # Result repair before new work (5.3 restart safety).
        _repair_first(ctx, out)
        state, found = run_state(ctx, args.run_key)
        if state == "complete":
            out["already_complete"] = True
            return _retry_sales_close(ctx, window, found, out)
        _check_schema(ctx)
        subs = window.list_subscriptions()
        _begin(ctx, window.run, out)
        try:
            code = window.run_steps(subs)
        except (Failed, Refused) as exc:
            raise _AfterWrites(getattr(exc, "code", "FAILED"), str(exc), out) from None
        except Exception as exc:  # noqa: BLE001 - after writes, any fault is exit 5
            raise _AfterWrites("ERROR", _describe(exc), out) from None
        rows = window.run.journal()
        out["counts"] = _counts(rows)
        out["deferred"] = window.run.notes["deferred"]
        out["alerts"] = len(window.run.notes["alerts"])
        out["sales_close"] = window.run.notes["sales_close"]
        if code == EXIT_FAILED:
            out["code"] = "failed:SALES_CLOSE"
        elif code == EXIT_DEFERRED:
            out["code"] = "deferred"
        return code, out


def _retry_sales_close(ctx, window, found: dict, out: dict) -> tuple:
    """R2-05: a same-key retry of a completed run keeps its failed sales
    close failed. The close is attempted again as separately recorded work
    (a `sales_close` register line, never a second completion), and the
    retry succeeds only after every link reads back inactive."""
    line = _completion(ctx, found["req"], found["lines"])
    try:
        result = json.loads(_recorded_result(ctx, found["req"], line)[1])
    except (Failed, OSError, ValueError):
        raise Failed("RESULT_MISSING", "the completed run's result cannot be read") from None
    closes = [r for r in found["lines"] if r.get("step") == "sales_close"]
    if result.get("sales_close") not in ("failed", "unverified") or (
            closes and closes[-1].get("outcome") == "ok"
            and line_protected(ctx, found["req"], closes[-1])):
        return EXIT_OK, out
    window.sales_close()
    closed = window.run.notes["sales_close"] != "failed"
    record_line(ctx, found["req"], "sales_close", "ok" if closed else "failed", [],
                {"run_key": found["run_key"]})
    out["sales_close"] = window.run.notes["sales_close"]
    if not closed:
        out["code"] = "failed:SALES_CLOSE"
        return EXIT_FAILED, out
    return EXIT_OK, out


class _AfterWrites(Exception):
    def __init__(self, code: str, detail: str, out: dict):
        super().__init__(detail)
        self.code = code
        self.out = out


def _counts(rows: list) -> dict:
    counts = {}
    for r in rows:
        key = r["step"] if r["step"] not in ("discover", "request", "bind") \
            else f"{r['step']}:{r['outcome'].split(':')[0]}"
        counts[key] = counts.get(key, 0) + 1
    return counts


def cmd_repair(ctx, args) -> tuple:
    out = {"cmd": "repair", "run_key": args.run_key, "code": "ok", "repaired": [],
           "incomplete": [], "releases": []}
    _lock(args)
    _run_key(args.run_key)
    todo = incomplete_runs(ctx)
    if not todo and not release_needed(ctx) and not markers_needed(ctx):
        return EXIT_OK, out  # idle: no Stripe call, no workspace, no write
    check_targets(ctx)
    with _web_lock(ctx):
        keys = [r["run_key"] for r in todo]
        before = {run["run_key"] for run, _line in release_needed(ctx)}
        try:
            still = repair_all(ctx)
        except Exception as exc:  # noqa: BLE001 - an incomplete run keeps the site down
            raise _AfterWrites("REPAIR", _describe(exc), out) from None
    out["repaired"] = [k for k in keys if k not in still]
    out["incomplete"] = still
    out["releases"] = sorted(before)
    if still:
        out["code"] = "failed:INCOMPLETE"
        return EXIT_AFTER_WRITES, out
    return EXIT_OK, out


def _seat_or_refuse(ctx, subscription_id: str) -> dict:
    from earwitness.app import purchases as _p
    if not SUB_RE.match(subscription_id or ""):
        raise Refused("USAGE", "--subscription is not a subscription ID")
    con = ctx.control(readonly=True)
    try:
        seat = _p.seat(con, subscription_id)
    finally:
        con.close()
    if seat is None:
        raise Refused("NO_SEAT", "no seat for this subscription")
    return seat


def _mini_run(ctx, args, command: str, precheck, writes, reached) -> tuple:
    """finish and retire: one seat's window write under the same completion
    rule as the window (baseline, write, checks, preserve, record, release).
    Success, including "already complete", means the seat is in the
    requested final state (R1-05): a completed attempt that stopped short
    is followed by a new attempt under the next run key."""
    out = {"cmd": command, "subscription_id": args.subscription, "code": "ok"}
    _lock(args)
    base_key = _run_key(args.run_key) if args.run_key else _la_date(ctx)
    check_targets(ctx)
    with _web_lock(ctx):
        _repair_first(ctx, out)
        seat = _seat_or_refuse(ctx, args.subscription)
        if reached(seat):
            out["already_complete"] = True
            return EXIT_OK, out
        attempt = 1
        while True:
            run_key = f"{base_key}+{command}-{args.subscription}" + \
                ("" if attempt == 1 else f"-a{attempt}")
            if run_state(ctx, run_key)[0] != "complete":
                break
            attempt += 1
        out["run_key"] = run_key
        context = precheck(seat)
        run = Run(ctx, run_key, command)
        _begin(ctx, run, out)
        try:
            writes(run, seat, context)
            checks = checks_for(ctx, command, run.journal(), context.get("fresh"))
            _mini_point(f"{command}:before_record")
            if not run.complete(checks):
                raise Failed("CHECKS" if not checks["ok"] else "PRESERVE",
                             "; ".join(checks["problems"])[:300])
        except _AfterWrites:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _AfterWrites(getattr(exc, "code", "ERROR"), _describe(exc), out) from None
    return EXIT_OK, out


def _owner_member(ctx, seat: dict) -> bool:
    """The seat's principal is an active owner of its workspace (read-only)."""
    opened, entry = _open_workspace_ro(ctx, seat["workspace_id"])
    if opened is None:
        return False
    try:
        row = opened.execute("SELECT role, revoked FROM memberships WHERE workspace_id=?"
                             " AND member=?", (entry["row_namespace"], seat["principal"])
                             ).fetchone()
    finally:
        opened.close()
    return bool(row) and row[0] == "owner" and not row[1]


def cmd_finish(ctx, args) -> tuple:
    """9.5 step 4: after Reno's `create`, the membership and `active`."""
    from earwitness.app import billing as _billing
    from earwitness.app import local_accounts as _la_mod
    from earwitness.app import purchases as _p
    ctx.require_configured()
    ctx.require_fallback()

    def reached(seat):
        return seat["state"] == "active" and _owner_member(ctx, seat)

    def precheck(seat):
        s = seat["subscription_id"]
        if seat["state"] != "awaiting_account":
            raise Refused("NOT_AWAITING_ACCOUNT", f"the seat is {seat['state']}")
        if seat["principal"] in ctx.policy.removed_principals:
            raise Refused("REMOVED_PRINCIPAL", "the seat's principal was removed")
        con = ctx.control(readonly=True)
        try:
            entry = registry_entry(con, seat["workspace_id"])
            account = _la_mod.find_by_principal(con, ctx.issuer, seat["principal"])
            paid_problem = paid_use_problem(con, entry) if entry is not None else ""
        finally:
            con.close()
        # R1-02: the seat must be exactly its verified protected record.
        req, doc = buyer_request(ctx, s)
        recorded = recorded_tuple(ctx, doc, req) if req else None
        if entry is None or recorded is None or recorded != {
                "principal": seat["principal"], "email": seat["email"],
                "subscription_id": s, "customer_id": seat["customer_id"],
                "offer": seat["offer"], "workspace_id": seat["workspace_id"],
                "db_identity": entry["db_identity"]}:
            raise Refused("RECORD_CONFLICT", "the seat is not its protected record")
        if paid_problem:
            # BY3-r21 (Astra r23 MED-1): no write for a workspace whose
            # rights record does not allow paid use.
            print(json.dumps({"alert": True, "code": "workspace_not_paid_capable",
                              "subscription_id": s, "workspace": seat["workspace_id"],
                              "reason": paid_problem}), file=sys.stderr)
            raise Refused("NOT_PAID_CAPABLE", f"{seat['workspace_id']}: {paid_problem}",
                          workspace=seat["workspace_id"], reason=paid_problem)
        try:
            sub = ctx.stripe.get_subscription(s, customer=seat["customer_id"])
            # BY3-r18 (Astra r20 MED-1, MED-2): admission only from valid
            # evidence: the seat's own subscription (checked again for a
            # substituted read) and its own latest invoice, paid.
            _billing.check_window_subscription(sub, s, customer=seat["customer_id"])
            paid = paid_latest_invoice(ctx, sub, seat["customer_id"])
        except _billing.ProviderEvidenceError as exc:
            raise Refused("PROVIDER_EVIDENCE_INVALID", f"{exc.kind}:{exc.field}",
                          kind=exc.kind, field=exc.field) from None
        except _billing.ProviderError as exc:
            raise Refused("STRIPE_UNREADABLE", _safe(exc)) from None
        problem = seat_resource_problem(ctx, seat, sub, billing_identity(entry))
        if problem:
            raise Refused("RESOURCE_MISMATCH", problem)
        if sub["status"] != "active" or not paid:
            raise Refused("NOT_PAID", "the subscription is not active with a paid invoice")
        if account is None or _la_mod.lookup_key(account["email"]) != seat["email"]:
            raise Refused("NO_ACCOUNT", "no local account for the seat's principal and email")
        if account["password_hash"] is None:
            raise Refused("NO_PASSWORD", "the account has no password yet")
        return {"entry": entry, "sub": sub, "fresh": {s: sub}}

    def writes(run, seat, context):
        from earwitness.app import db as _db
        s, w = seat["subscription_id"], seat["workspace_id"]
        con = ctx.control()
        try:
            with _immediate(con):
                _p.journal(con, run.run_key, s, "finish_intent", w, now=_iso(ctx.now()))
        finally:
            con.close()
        _mini_point("finish:before_membership")
        # The workspace takes today's verified resource first, so the gate
        # checked below is not a stale one (R1-06).
        why = reconcile(ctx, run.run_key, seat, "ok", context["sub"], "finish")
        if why:
            raise Failed("RECONCILE", why)
        path = workspace_path(ctx, context["entry"])
        wcon = _db.open_existing(path, readonly=False)
        try:
            with _db.transaction(wcon, write=True):
                wcon.execute("INSERT INTO memberships (workspace_id, member, role, created_at)"
                             " VALUES (?, ?, 'owner', ?) ON CONFLICT(workspace_id, member)"
                             " DO UPDATE SET role='owner', revoked=0",
                             (context["entry"]["row_namespace"], seat["principal"],
                              _iso(ctx.now())))
        finally:
            wcon.close()
        _mini_point("finish:after_membership")
        con = ctx.control()
        try:
            with _immediate(con):
                _p.transition(con, s, "active", run_key=run.run_key, now=_iso(ctx.now()))
                _p.journal(con, run.run_key, s, "finish", "active", now=_iso(ctx.now()))
        finally:
            con.close()
        _mini_point("finish:after_active")

    return _mini_run(ctx, args, "finish", precheck, writes, reached)


def cmd_retire(ctx, args) -> tuple:
    """13: an ended seat whose workspace is listed in retired_workspace_ids."""
    from earwitness.app import purchases as _p
    ctx.require_configured()

    def reached(seat):
        return seat["state"] == "retired"

    def precheck(seat):
        if seat["state"] != "ended" or not seat["workspace_id"]:
            raise Refused("NOT_ENDED", f"the seat is {seat['state']}")
        if seat["workspace_id"] not in ctx.policy.retired_workspace_ids:
            raise Refused("NOT_RETIRED_IN_ADMISSION", "add the workspace to"
                          " retired_workspace_ids in the admission edit first")
        return {}

    def writes(run, seat, _context):
        _mini_point("retire:before_transition")
        con = ctx.control()
        try:
            with _immediate(con):
                _p.transition(con, seat["subscription_id"], "retired", run_key=run.run_key,
                              now=_iso(ctx.now()))
                _p.journal(con, run.run_key, seat["subscription_id"], "retire", "retired",
                           now=_iso(ctx.now()))
        finally:
            con.close()
        _mini_point("retire:after_transition")

    return _mini_run(ctx, args, "retire", precheck, writes, reached)


def _refuse_if_open(ctx) -> None:
    """Crash recovery guard (kept beside the lock): an online write while a
    purchase run is open would break that run's preserve. Uses the local
    verified marker, so it works with the protected volume unmounted."""
    if open_runs_local(ctx):
        raise Refused("PURCHASE_RUN_OPEN", "a purchase run is open; retry after the window"
                      " ends and repair exits 0")


@contextlib.contextmanager
def _online_lock():
    """online-lock-contract.md: the fixed window lock path, a fresh
    O_RDWR|O_CREAT|O_CLOEXEC 0600 descriptor of our own, flock
    LOCK_EX|LOCK_NB; held only around the write transaction. A window in
    progress (the runner holds it from preflight to cleanup) refuses us;
    while we hold it, the runner's preflight is refused LOCKED."""
    import errno
    import fcntl
    path = window_lock.lock_path()
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise Refused("WINDOW_LOCKED", "the nightly window holds the lock; retry"
                              " after it ends") from None
            raise
        yield
    finally:
        os.close(fd)


def _delivery_proof(ctx, con, subscription_id: str) -> str:
    """The release that authorizes marking this buyer delivered, read on
    `con`: an active undelivered seat, its account's current password time,
    and an eligible release file listing the buyer as complete after it."""
    from earwitness.app import local_accounts as _la_mod
    from earwitness.app import purchases as _p
    seat = _p.seat(con, subscription_id)
    if seat is None or seat["state"] != "active" or not seat["workspace_id"] \
            or seat.get(_p.CREDENTIAL_COLUMN):
        raise Refused("NOT_ACTIVE", "the seat is not an undelivered active seat")
    account = _la_mod.find_by_principal(con, ctx.issuer, seat["principal"])
    if account is None or account["password_set_at"] is None:
        raise Refused("NO_PASSWORD", "the account has no password")
    set_at = float(account["password_set_at"])
    proof = None
    for path in release_files():
        doc = read_release(path)
        if doc is None or not eligible(path, doc):
            continue
        at = _parse_iso(doc["result"].get("generated_at"))
        if subscription_id in (doc["result"].get("complete") or []) and \
                at is not None and at > set_at:
            proof = os.path.basename(path)
    if proof is None:
        raise Refused("NO_ELIGIBLE_RELEASE", "no eligible release file lists this buyer as"
                      " complete after the latest password")
    return proof


def cmd_delivered(ctx, args) -> tuple:
    """9.5 step 5, online: only credential_delivered_at, and only with an
    eligible release file that lists the buyer as complete after the
    account's latest password. The proof is checked once early, and again
    inside the locked write transaction (R3-04): a window that ran in
    between (a reset, a new release) makes the early proof stale."""
    from earwitness.app import purchases as _p
    out = {"cmd": "delivered", "subscription_id": args.subscription, "code": "ok"}
    _refuse_if_open(ctx)
    seat = _seat_or_refuse(ctx, args.subscription)
    if seat["state"] != "active" or not seat["workspace_id"]:
        raise Refused("NOT_ACTIVE", f"the seat is {seat['state']}")
    if seat.get(_p.CREDENTIAL_COLUMN):
        out["already_delivered"] = True
        return EXIT_OK, out
    con = ctx.control(readonly=True)
    try:
        proof = _delivery_proof(ctx, con, args.subscription)
    finally:
        con.close()
    with _online_lock():
        con = ctx.control()
        try:
            with _immediate(con):
                _refuse_if_open(ctx)
                if _delivery_proof(ctx, con, args.subscription) != proof:
                    raise Refused("NO_ELIGIBLE_RELEASE", "the proof changed; run again")
                if not _p.mark_delivered(con, args.subscription, now=_iso(ctx.now())):
                    raise Refused("NOT_ACTIVE", "the seat changed")
        finally:
            con.close()
    out["release_file"] = proof
    return EXIT_OK, out


CANCELLABLE = ("reserved", "recorded", "bound", "awaiting_account", "active")


def cmd_cancel_request(ctx, args) -> tuple:
    """4.6: read Stripe, store target_end (the earliest); no Stripe write."""
    from earwitness.app import billing as _billing
    from earwitness.app import purchases as _p
    out = {"cmd": "cancel-request", "subscription_id": args.subscription, "code": "ok"}
    ctx.require_configured()
    _refuse_if_open(ctx)
    seat = _seat_or_refuse(ctx, args.subscription)
    if seat["state"] not in CANCELLABLE:
        raise Refused("NOT_CANCELLABLE", f"the seat is {seat['state']}")
    try:
        sub = ctx.stripe.get_subscription(args.subscription, customer=seat["customer_id"])
    except _billing.ProviderEvidenceError as exc:
        # BY3-r18: the documented codes. Another (or no well-formed)
        # subscription or customer is a mismatch (exit 3); a malformed date
        # is END_DATE_MALFORMED (exit 2); any other malformed field exit 2.
        detail = f"{exc.kind}:{exc.field}"
        if exc.field in ("id", "customer"):
            raise Refused("RESOURCE_MISMATCH", detail) from None
        if exc.field in END_DATE_FIELDS:
            raise Failed("END_DATE_MALFORMED", detail) from None
        raise Failed("PROVIDER_EVIDENCE_INVALID", detail) from None
    except _billing.ProviderError as exc:
        raise Failed("STRIPE_UNREADABLE", _safe(exc)) from None
    # Only this seat's subscription and customer, with exact-type dates,
    # may queue a target the window later applies (Astra r19 MED-1).
    problem = seat_resource_problem(ctx, seat, sub)
    if problem:
        raise Refused("RESOURCE_MISMATCH", problem)
    if sub["status"] in PAID_TERMINAL:
        raise Refused("TERMINAL", "the subscription has already ended")
    if sub.get("end_well_formed") is not True or sub.get("period_end_well_formed") is not True:
        raise Failed("END_DATE_MALFORMED", "the subscription's dates are not well formed")
    target = int(sub.get("current_period_end") or 0)
    if target <= 0:
        raise Failed("NO_PERIOD_END", "the subscription has no current period end")
    with _online_lock():
        con = ctx.control()
        try:
            with _immediate(con):
                _refuse_if_open(ctx)
                # R4-02: a window may have ended the seat since the precheck.
                current = _p.seat(con, args.subscription)
                if current is None or current["state"] not in CANCELLABLE:
                    raise Refused("NOT_CANCELLABLE", "the seat is no longer cancellable")
                try:
                    row = _p.queue_cancel_request(con, args.subscription, target,
                                                  now=_iso(ctx.now()))
                except _p.SeatError as exc:
                    raise Refused("SCHEMA_UNPINNED", str(exc)) from None
        finally:
            con.close()
    out.update({"target_end": _iso(row["target_end"]), "requested_at": row["requested_at"],
                "state": row["outcome"] or "queued"})
    return EXIT_OK, out


def cmd_status(ctx, args) -> tuple:
    """5.6: read-only, usable with the site up."""
    from earwitness.app import billing as _billing
    from earwitness.app import db as _db
    from earwitness.app import purchases as _p
    seat = _seat_or_refuse(ctx, args.subscription)
    con = ctx.control(readonly=True)
    try:
        request = _p.cancel_request(con, args.subscription)
        entry = registry_entry(con, seat["workspace_id"]) if seat["workspace_id"] else None
    finally:
        con.close()
    out = {"cmd": "status", "subscription_id": args.subscription, "code": "ok",
           "state": seat["state"], "offer": seat["offer"], "workspace_id": seat["workspace_id"],
           "principal": seat["principal"], "last_error": seat["last_error"],
           "credential_delivered_at": seat.get(_p.CREDENTIAL_COLUMN),
           "fallback_reset_allowed": _p.fallback_reset_allowed(seat),
           "cancel_request": None if request is None else {
               "target_end": _iso(request["target_end"]), "requested_at": request["requested_at"],
               "state": request["outcome"] or "queued"}}
    try:
        sub = ctx.stripe.get_subscription(args.subscription, customer=seat["customer_id"])
        out["stripe"] = {"status": sub["status"],
                         "cancel_at": _iso(sub["cancel_at"]) if sub.get("cancel_at") else None,
                         "current_period_end": _iso(sub["current_period_end"])
                         if sub.get("current_period_end") else None}
    except _billing.ProviderError as exc:
        out["stripe"] = {"status": "unreadable", "detail": _safe(exc)}
        if isinstance(exc, _billing.ProviderEvidenceError):
            out["stripe"].update(kind=exc.kind, field=exc.field)
    path = workspace_path(ctx, entry) if entry is not None else None
    if seat["state"] != "retired" and path:
        try:
            # A read through a normal handle: with the site up it sees the
            # web process's WAL; as the last handle it removes the -wal and
            # -shm files it opened. It writes nothing.
            wcon = _db.open_existing(path, readonly=False)
            try:
                ent = _billing.entitlement_status(wcon, entry["row_namespace"], ctx.billing,
                                                  now=ctx.now(), tenant=billing_identity(entry))
            finally:
                wcon.close()
            out["gate"] = {"state": ent.get("state"), "allowed": bool(ent.get("allowed"))}
        except (FileNotFoundError, RuntimeError, ValueError, sqlite3.Error) as exc:
            out["gate"] = {"state": "unreadable", "detail": type(exc).__name__}
    return EXIT_OK, out


RUNNER_CONFIG = "/etc/earwitness/sync/window.json"
# deferred_over_budget: a budget deferral that alerts (pilot_window.py); mail as deferred
MAIL_ELIGIBLE = ("success", "success_no_change", "deferred", "deferred_over_budget")


def _records_dir(args) -> str:
    """The runner's records folder: --records-dir, else `records_dir` of the
    installed window.json, else the default."""
    if getattr(args, "records_dir", None):
        if _production(os.environ):
            # Code r4 MED-1: in production only the runner's own config names it.
            raise Refused("RECORDS_DIR", "--records-dir is refused in production")
        return args.records_dir
    try:
        config = _read_json(RUNNER_CONFIG)
    except (OSError, ValueError):
        config = None
    if not isinstance(config, dict):
        raise Refused("RUNNER_CONFIG", f"{RUNNER_CONFIG} cannot be read")
    return config.get("records_dir") or DEFAULT_RECORDS_DIR


def _require_qualifying_window(args) -> None:
    """R3-05: send-links acts only for a run the runner itself is about to
    send for: its finish.json for this run key is written (before the send),
    END is ok, the outcome is success, success_no_change, deferred or
    deferred_over_budget, no
    send decision is recorded yet and the diagnostics did not fail. A
    completion record alone never qualifies."""
    path = os.path.join(_records_dir(args), args.run_key, "finish.json")
    try:
        finish = _read_json(path)
    except (OSError, ValueError):
        finish = None
    if not isinstance(finish, dict) or finish.get("run_key") != args.run_key \
            or finish.get("end") != "ok" or finish.get("outcome") not in MAIL_ELIGIBLE \
            or finish.get("send_links") is not None or finish.get("diagnostics_failed"):
        raise Refused("WINDOW_NOT_ELIGIBLE", "no successful window record for this run key;"
                      " the next successful window releases its buyers")


def cmd_send_links(ctx, args) -> tuple:
    """Step 11. Reads only this run's release files, never the bundle. In
    fallback mode it releases no message (BY5 owns the outbox) and marks
    each valid release file of the run eligible, which `delivered` needs."""
    out = {"cmd": "send-links", "run_key": args.run_key, "code": "ok",
           "counts": {"released": 0, "sent": 0, "eligible_files": 0}}
    _run_key(args.run_key)
    if getattr(args, "records_dir", None) and _production(os.environ):
        raise Refused("RECORDS_DIR", "--records-dir is refused in production")
    ok, _why = ctx.configured()
    if not ok:
        out["code"] = "ok"
        out["not_configured"] = True
        return EXIT_OK, out
    ctx.require_fallback()
    _require_qualifying_window(args)
    for path in release_files(args.run_key):
        doc = read_release(path)
        if doc is None:
            print(f"app_purchases: {os.path.basename(path)} is not a valid release file",
                  file=sys.stderr)
            continue
        if not eligible(path, doc):
            _atomic_bytes(path + ".eligible", _json_bytes({
                "run_key": doc["run_key"], "result_sha256": doc["result_sha256"],
                "marked_at": _iso(ctx.now())}))
        out["counts"]["eligible_files"] += 1
    return EXIT_OK, out


def _sales_close_retried_ok(lines) -> bool:
    """A later separately recorded sales close of the run passed (R2-05)."""
    closes = [r for r in lines if r.get("step") == "sales_close"]
    return bool(closes) and closes[-1].get("outcome") == "ok"


def _latest_window_result(ctx) -> dict | None:
    """The latest locally marked, verified window result, read from the
    local register and checked against the recorded hash (works with the
    protected volume unmounted)."""
    best = None
    for run in find_runs(ctx):
        if run["command"] != "window":
            continue
        line = local_completion(ctx, run["req"], run["lines"])
        if line is not None and (best is None or run["run_key"] > best[0]["run_key"]):
            best = (run, line)
    if best is None:
        return None
    run, line = best
    want = line["values"].get("result_sha256")
    for entry in line.get("evidence") or []:
        if entry.get("sha256") != want:
            continue
        try:
            with open(os.path.join(ctx.layout["register_dir"], run["req"], entry["name"]),
                      "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if _sha256_bytes(data) != want:
            return None
        result = json.loads(data)
        if result.get("sales_close") in ("failed", "unverified") and \
                _sales_close_retried_ok(run["lines"]):
            result["sales_close"] = "closed"
        return result
    return None


def cmd_morning_check(ctx, args) -> tuple:
    """5.5: read-only alert list. Delivery (Slack) is a hook: the timer
    unit pipes this line to the alert sender; exit 1 means alerts."""
    from earwitness.app import billing as _billing
    from earwitness.app import purchases as _p
    alerts = []
    out = {"cmd": "morning-check", "code": "ok", "alerts": alerts}
    day = args.date or _la_date(ctx)
    records = args.records_dir
    if not records:
        try:
            records = _read_json(RUNNER_CONFIG).get("records_dir")
        except (OSError, ValueError, AttributeError):
            alerts.append({"code": "runner_config_unreadable"})
        records = records or DEFAULT_RECORDS_DIR
    finish = os.path.join(records, day, "finish.json")
    try:
        record = _read_json(finish)
        buyer = (record.get("steps") or {}).get("buyer_setup")
        if buyer not in (None, "ok", "skipped:no_pending", "skipped:not_configured"):
            deferred = isinstance(buyer, str) and buyer.startswith("deferred")
            alerts.append({"code": "purchase_step_" + ("deferred" if deferred else "failed"),
                           "detail": buyer})
    except (OSError, ValueError, AttributeError):
        alerts.append({"code": "window_not_run", "detail": day})
    ok, why = ctx.configured()
    con = ctx.control(readonly=True)
    try:
        seats = _p.all_seats(con)
        last_sync = _p.last_sync_ok(con)
        late = _p.late_cancel_requests(con)
        entries = {s["workspace_id"]: registry_entry(con, s["workspace_id"])
                   for s in seats if s["workspace_id"]}
        try:
            unpaid_pool = pool_paid_problems(ctx, con)
        except Refused:
            unpaid_pool = []  # an unreadable admission file: configured() says so
    finally:
        con.close()
    for workspace, reason in unpaid_pool:
        alerts.append({"code": "pool_workspace_not_paid_capable", "workspace": workspace,
                       "reason": reason})
    if why == LINKS_MISSING:
        alerts.append({"code": LINKS_MISSING, "offers": ctx.missing_links()})
    elif not ok and any(s["state"] not in ("ended", "retired") for s in seats):
        alerts.append({"code": "not_configured_with_seats", "detail": why})
    if not ok:
        out["not_configured"] = True
        return (1 if alerts else EXIT_OK), out
    try:
        if open_runs_local(ctx):
            alerts.append({"code": "incomplete_purchase_run"})
        latest = _latest_window_result(ctx)
        if latest and latest.get("sales_close") in ("failed", "unverified"):
            alerts.append({"code": "sales_close_" + latest["sales_close"],
                           "detail": latest.get("run_key")})
    except Refused:
        alerts.append({"code": "layout_unreadable"})
    for s in seats:
        if s["state"] in ("refused", "needs_refund"):
            alerts.append({"code": f"seat_{s['state']}", "subscription_id": s["subscription_id"]})
        elif s["state"] in ("awaiting_account", "reserved", "recorded", "bound"):
            alerts.append({"code": f"seat_{s['state']}", "subscription_id": s["subscription_id"]})
        elif s["state"] == "active" and not s.get(_p.CREDENTIAL_COLUMN):
            alerts.append({"code": "credential_undelivered",
                           "subscription_id": s["subscription_id"]})
    if any(s["state"] == "active" for s in seats):
        at = _parse_iso(last_sync) if last_sync else None
        if at is None or ctx.now() - at > SYNC_ALERT_S:
            alerts.append({"code": "sync_stale"})
    # R1-08: every link is checked on its own once the close is due.
    if ctx.now() >= ctx.close_at:
        for offer in ctx.missing_links():
            alerts.append({"code": "sales_close_unconfigured", "offer": offer})
        for offer, link in sorted(ctx.links.items()):
            try:
                if ctx.stripe.get_payment_link(link)["active"]:
                    alerts.append({"code": "sales_close_overdue", "link": link})
            except _billing.ProviderEvidenceError as exc:
                alerts.append(_evidence_item(exc, link=link))
            except _billing.ProviderError as exc:
                alerts.append({"code": "payment_link_unreadable", "link": link,
                               "detail": _safe(exc)})
    # R1-03: every set-up seat's subscription is still exactly its seat's.
    for s in seats:
        if s["state"] == "ended":
            # A safety net (code r7): an ended seat whose subscription still
            # lives was ended without a verified terminal status.
            try:
                if ctx.stripe.get_subscription(s["subscription_id"],
                                               customer=s["customer_id"])["status"] \
                        not in PAID_TERMINAL:
                    alerts.append({"code": "ended_seat_subscription_live",
                                   "subscription_id": s["subscription_id"]})
            except _billing.ProviderEvidenceError as exc:
                alerts.append(_evidence_item(exc, subscription_id=s["subscription_id"]))
            except _billing.ProviderError as exc:
                if isinstance(exc, _billing.ProviderNotFound):
                    alerts.append({"code": "stripe_subscription_not_found",
                                   "subscription_id": s["subscription_id"]})
            continue
        if s["state"] not in ("bound", "awaiting_account", "active") or \
                not entries.get(s["workspace_id"]):
            continue
        try:
            sub = ctx.stripe.get_subscription(s["subscription_id"], customer=s["customer_id"])
        except _billing.ProviderEvidenceError as exc:
            alerts.append(_evidence_item(exc, subscription_id=s["subscription_id"]))
            continue
        except _billing.ProviderError as exc:
            code = "stripe_subscription_not_found" \
                if isinstance(exc, _billing.ProviderNotFound) else "stripe_unreadable"
            alerts.append({"code": code, "subscription_id": s["subscription_id"],
                           "detail": _safe(exc)})
            continue
        problem = seat_resource_problem(ctx, s, sub, billing_identity(entries[s["workspace_id"]]))
        if problem and sub["status"] not in PAID_TERMINAL:
            alerts.append({"code": "seat_resource_mismatch",
                           "subscription_id": s["subscription_id"], "field": problem})
    known = {s["subscription_id"] for s in seats}
    try:
        for offer, prices in ctx.offer_prices().items():
            for price in prices:
                for sub in ctx.stripe.list_subscriptions(price):
                    if sub["status"] not in PAID_TERMINAL and (
                            not sub.get("cancel_at") or int(sub["cancel_at"]) > ctx.pilot_end):
                        alerts.append({"code": "end_date_missing", "subscription_id": sub["id"]})
                    if sub["id"] not in known and sub["status"] == "active" and \
                            paid_latest_invoice(ctx, sub, sub["customer"]):
                        alerts.append({"code": "paid_without_seat", "subscription_id": sub["id"]})
        customers = {s["subscription_id"]: s["customer_id"] for s in seats}
        for req in late:
            sub = ctx.stripe.get_subscription(req["subscription_id"],
                                              customer=customers.get(req["subscription_id"]))
            if sub["status"] not in PAID_TERMINAL:
                alerts.append({"code": "cancel_request_late",
                               "subscription_id": req["subscription_id"]})
    except _billing.ProviderEvidenceError as exc:
        # Code review r22 NOTE-1: a refused listed subscription is named,
        # as in the discovery alert, when its id is well formed.
        where = {"subscription_id": exc.subscription_id} \
            if getattr(exc, "subscription_id", "") else {}
        alerts.append(_evidence_item(exc, **where))
    except _billing.ProviderError as exc:
        alerts.append({"code": "stripe_unreadable", "detail": _safe(exc)})
    return (1 if alerts else EXIT_OK), out


def _evidence_item(exc, **where) -> dict:
    """The morning check's `provider_evidence_invalid` alert (BY3-r18)."""
    return {"code": "provider_evidence_invalid", **where, "kind": exc.kind, "field": exc.field}


# -------------------------------------------------------------------- main

COMMANDS = {"pending": cmd_pending, "window": cmd_window, "repair": cmd_repair,
            "finish": cmd_finish, "retire": cmd_retire, "delivered": cmd_delivered,
            "cancel-request": cmd_cancel_request, "status": cmd_status,
            "send-links": cmd_send_links, "morning-check": cmd_morning_check}
WINDOW_COMMANDS = frozenset({"window", "repair", "finish", "retire"})


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("USAGE", message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="app_purchases.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--run-key", required=True)
    p = sub.add_parser("window")
    p.add_argument("--window-lock-fd", type=int)
    p.add_argument("--run-key", required=True)
    p.add_argument("--budget-s", type=int, required=True)
    p = sub.add_parser("repair")
    p.add_argument("--window-lock-fd", type=int)
    p.add_argument("--run-key", required=True)
    for name in ("finish", "retire"):
        p = sub.add_parser(name)
        p.add_argument("--subscription", required=True)
        p.add_argument("--window-lock-fd", type=int)
        p.add_argument("--run-key")
    for name in ("delivered", "cancel-request", "status"):
        p = sub.add_parser(name)
        p.add_argument("--subscription", required=True)
    p = sub.add_parser("send-links")
    p.add_argument("--run-key", required=True)
    p.add_argument("--records-dir")
    p = sub.add_parser("morning-check")
    p.add_argument("--records-dir")
    p.add_argument("--date")
    return parser


def main(argv=None, *, provider=None, now=None, env=None, monotonic=None) -> int:
    out = {"cmd": None, "code": "failed:USAGE"}
    try:
        args = _parser().parse_args(argv)
        out = {"cmd": args.command, "code": "ok"}
        if getattr(args, "run_key", None):
            out["run_key"] = args.run_key
        if args.command in WINDOW_COMMANDS:
            _lock(args)  # before anything else: no lock, no write (exit 3)
        ctx = Ctx(env, provider, now, monotonic)
        code, out = COMMANDS[args.command](ctx, args)
    except Refused as exc:
        out["code"] = f"refused:{exc.code}"
        out.update(exc.extra)
        print(f"app_purchases: refused: {exc}", file=sys.stderr)
        code = EXIT_REFUSED
    except _AfterWrites as exc:
        out = dict(exc.out, code=f"failed:{exc.code}")
        print(f"app_purchases: failed after writes began: {exc}", file=sys.stderr)
        code = EXIT_AFTER_WRITES
    except Failed as exc:
        out["code"] = f"failed:{exc.code}"
        print(f"app_purchases: failed: {exc}", file=sys.stderr)
        code = EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - before any write: a plain failure
        out["code"] = "failed:ERROR"
        print(f"app_purchases: failed: {_describe(exc)}", file=sys.stderr)
        code = EXIT_FAILED
    print(json.dumps(out, sort_keys=True))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
