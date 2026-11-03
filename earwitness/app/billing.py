"""Billing: Stripe checkout/portal adapters and webhook reconciliation (Task 13).

Production billing requires live Stripe configuration; local stub or
private-key payments and test-mode success presented as live success
are defects, not features. Acknowledged priced products and their
provider metadata come from explicit local configuration, never invented
here. Money is integer cents; no float math, no card data through this
server (Stripe hosts checkout; this server creates sessions and verifies
signed webhooks).

Entitlement is verified server state, never event truth. A signed
webhook is only a reconciliation trigger: the server binds the
provider customer/subscription to its own workspace record, reads the
subscription's CURRENT provider resource through the provider
protocol, checks the configured price catalog, and writes only what
the provider currently reports, serialized per subscription. Event
timestamps, delivery order, and max(period_end) never establish
entitlement. Failed or ambiguous reconciliation retains the last
verified state and can never grant access or extend it past verified
expiry. Unknown or blocked states fail closed, and no browser storage
gates access. Past-due accounts block after the configured grace
window; cancels hold access to the verified period end.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field

from .access import AuthorizationError

STRIPE_API = "https://api.stripe.com/v1"
WEBHOOK_TOLERANCE_S = 300
# The one Stripe API version this code reads and writes (design 8.1). It is
# pinned on the webhook endpoint and sent as Stripe-Version on every call,
# so events and replies both have its shapes.
STRIPE_VERSION = "2026-09-30.endive"
# Where an invoice names its subscription in STRIPE_VERSION: since
# 2025-03-31.basil the top-level `invoice.subscription` is gone and the
# subscription sits under `parent.subscription_details`. Its metadata
# (the billing identity) sits beside it.
INVOICE_SUBSCRIPTION_PATH = ("parent", "subscription_details", "subscription")
INVOICE_METADATA_PATH = ("parent", "subscription_details", "metadata")
# The single webhook route handles only these (design 8.1).
WEBHOOK_EVENT_TYPES = ("customer.subscription.updated",
                       "customer.subscription.deleted",
                       "invoice.paid", "invoice.payment_failed")
WEBHOOK_MAX_BYTES = 64 * 1024
# Bounded Stripe calls (design 8.5).
PROVIDER_DEADLINE_S = 10.0
PROVIDER_MAX_REPLY_BYTES = 256 * 1024
PROVIDER_MAX_ERROR_BYTES = 16 * 1024
PROVIDER_CHUNK_BYTES = 16 * 1024
# The per-subscription lock table holds at most this many entries.
SUBSCRIPTION_LOCKS_MAX = 1000

_BILLING_DDL = """
CREATE TABLE IF NOT EXISTS billing_events (
    event_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT 'local',
    customer_id TEXT NOT NULL DEFAULT '',
    subscription_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    price_id TEXT NOT NULL DEFAULT '',
    period_end INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_entitlement (
    workspace_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unknown',
    price_id TEXT NOT NULL DEFAULT '',
    trial_end INTEGER NOT NULL DEFAULT 0,
    current_period_end INTEGER NOT NULL DEFAULT 0,
    past_due_since INTEGER NOT NULL DEFAULT 0,
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS billing_trials (
    workspace_id TEXT PRIMARY KEY,
    trial_state TEXT NOT NULL DEFAULT '',
    trial_kind TEXT NOT NULL DEFAULT '',
    trial_outcome TEXT NOT NULL DEFAULT '',
    trial_reservation_id TEXT NOT NULL DEFAULT '',
    trial_reserved_at INTEGER NOT NULL DEFAULT 0,
    customer_history TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS billing_bindings (
    workspace_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL DEFAULT '',
    bound_by TEXT NOT NULL DEFAULT '',
    bound_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS billing_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL DEFAULT ''
);
"""


# Every operation name the entitlement gate can see: customer-facing
# service operations (the names services.py passes to its membership and
# role helpers), the agent API tools, and the MCP endpoint. A test keeps
# this list equal to the code. It only validates the exempt list; the
# gate itself never consults it, so a name missing here is still gated.
KNOWN_OPERATIONS = (
    "customer_alert", "customer_alert_backfill", "customer_alert_lifecycle",
    "customer_alert_read", "customer_alerts", "customer_allocate",
    "customer_archive", "customer_attach_evidence",
    "customer_attach_target_evidence", "customer_brand_detail",
    "customer_brands", "customer_briefs", "customer_campaign_detail",
    "customer_campaign_draft", "customer_campaign_targets",
    "customer_campaigns", "customer_check_alert", "customer_compare",
    "customer_creative_detail", "customer_delete_item",
    "customer_delete_target", "customer_detach_evidence",
    "customer_edit_item", "customer_episode_reads", "customer_evidence_fit",
    "customer_export_download",
    "customer_filter_values", "customer_freeze_report",
    "customer_note_collection", "customer_overview", "customer_preferences",
    "customer_reader_status", "customer_record", "customer_record_window",
    "customer_recording", "customer_report", "customer_report_ad",
    "customer_reports", "customer_research_export", "customer_research_query",
    "customer_review_episode",
    "customer_save_alert", "customer_save_brief", "customer_save_campaign",
    "customer_save_item", "customer_save_mistake", "customer_save_placement",
    "customer_save_preferences", "customer_save_target", "customer_saved_item",
    "customer_saved_items", "customer_search", "customer_show_detail",
    "customer_shows", "customer_summary",
    "shared_search", "shared_evidence", "shared_transcript",
    "customer_mistake_read", "customer_report_html", "customer_show_artwork",
    "collab_invite_send", "owner_ad_review",
    "billing_checkout", "billing_portal",
    "get_workspace_context", "search_evidence", "get_evidence",
    "search_transcripts", "get_transcript_window", "list_brands",
    "list_shows", "list_episodes", "summarize_evidence", "list_watchlists",
    "get_watchlist_results", "list_campaigns", "get_campaign",
    "get_source_provenance", "search", "fetch", "mcp",
)

# UNAPPROVED: the one list of operations that stay open when entitlement
# checks are on. Reno has not approved the offer or the access matrix.
# Every other customer-facing operation (service routes, the agent API,
# MCP, share-guest reads) needs an active entitlement, so a typo in this
# list gates more, never less. Billing must stay open so a workspace can
# pay; overview and reader status show the workspace and its connections.
# EARWITNESS_BILLING_UNGATED_OPERATIONS replaces it.
UNAPPROVED_UNGATED_OPERATIONS = (
    "billing_checkout", "billing_portal", "customer_overview",
    "customer_reader_status",
)

# UNAPPROVED trial-lifecycle policy defaults, strictest reading: no
# release after a failed or abandoned checkout, and any earlier trial or
# subscription (cancelled, expired or paid) counts as the one trial.
TRIAL_RESERVATION_TTL_S = 24 * 3600


@dataclass
class BillingConfig:
    secret_key: str = ""
    webhook_secret: str = ""
    success_url: str = ""
    cancel_url: str = ""
    prices: dict = field(default_factory=dict)
    past_due_grace_s: int = 72 * 3600
    livemode_required: bool = True
    # Entitlement checks: off unless configured. (UNAPPROVED list below.)
    entitlement_required: bool = False
    ungated_operations: tuple = UNAPPROVED_UNGATED_OPERATIONS
    # UNAPPROVED trial-lifecycle policy switches (strictest defaults).
    trial_release_abandoned: bool = False
    trial_reservation_ttl_s: int = TRIAL_RESERVATION_TTL_S
    trial_cancelled_counts: bool = True
    trial_expired_counts: bool = True
    prior_subscription_blocks_trial: bool = True
    # Exact external workspace IDs the gate never refuses (the judges'
    # demo and the owner's workspace), and the pool workspaces sold to
    # buyers. A workspace may not be both.
    comped_workspaces: tuple = ()
    pool_workspaces: tuple = ()
    # In-app checkout stays off: buying happens on Stripe (design 4).
    inapp_checkout: bool = False
    # The Stripe portal configuration every portal session uses.
    portal_configuration: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.secret_key and self.webhook_secret
                    and self.success_url and self.cancel_url)


def _flag(src, name: str, default: bool) -> bool:
    raw = str(src.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def config_from_env(env=None) -> BillingConfig:
    import os
    src = env if env is not None else os.environ
    prices: dict = {}
    raw_prices = str(src.get("EARWITNESS_BILLING_PRICES") or "")
    if raw_prices:
        try:
            loaded = json.loads(raw_prices)
            if isinstance(loaded, dict):
                prices = loaded
        except ValueError:
            prices = {}
    try:
        grace_h = int(src.get("EARWITNESS_BILLING_PAST_DUE_GRACE_HOURS") or 72)
    except ValueError:
        grace_h = 72
    if str(src.get("EARWITNESS_BILLING_GATED_OPERATIONS") or "").strip():
        # The old allow-list failed open on a typo; ignoring it would
        # silently open routes its owner thought were gated.
        raise ValueError(
            "EARWITNESS_BILLING_GATED_OPERATIONS was removed: when entitlement"
            " is required every customer operation is gated except those in"
            " EARWITNESS_BILLING_UNGATED_OPERATIONS")
    ungated = tuple(name.strip() for name in str(
        src.get("EARWITNESS_BILLING_UNGATED_OPERATIONS") or "").split(",")
        if name.strip()) or UNAPPROVED_UNGATED_OPERATIONS
    for name in ungated:
        if name not in KNOWN_OPERATIONS:
            raise ValueError(
                f"EARWITNESS_BILLING_UNGATED_OPERATIONS names unknown operation"
                f" {name!r}")
    try:
        ttl = int(src.get("EARWITNESS_BILLING_TRIAL_RESERVATION_TTL_S")
                  or TRIAL_RESERVATION_TTL_S)
    except ValueError:
        ttl = TRIAL_RESERVATION_TTL_S
    if str(src.get("EARWITNESS_BILLING_POOL_WORKSPACES") or "").strip():
        # BY2: the pools live in the admission file's purchase_seats block.
        raise ValueError(
            "EARWITNESS_BILLING_POOL_WORKSPACES was removed: the purchase"
            " pools are the admission file's purchase_seats.pools")
    from . import admission as _admission
    comped = _id_list(src, "EARWITNESS_BILLING_COMPED_WORKSPACES")
    pools = _admission.configured_pools(src)
    both = [w for w in comped if w in pools]
    if both:
        raise ValueError(
            "EARWITNESS_BILLING_COMPED_WORKSPACES names a workspace that"
            " the admission file's purchase pools also list: "
            + ", ".join(both))
    return BillingConfig(
        secret_key=str(src.get("STRIPE_SECRET_KEY") or ""),
        webhook_secret=str(src.get("STRIPE_WEBHOOK_SECRET") or ""),
        success_url=str(src.get("EARWITNESS_BILLING_SUCCESS_URL") or ""),
        cancel_url=str(src.get("EARWITNESS_BILLING_CANCEL_URL") or ""),
        prices=prices,
        past_due_grace_s=max(0, grace_h) * 3600,
        livemode_required=str(
            src.get("EARWITNESS_BILLING_LIVEMODE_REQUIRED") or "1").lower()
        not in ("0", "false", "no"),
        entitlement_required=_flag(
            src, "EARWITNESS_BILLING_ENTITLEMENT_REQUIRED", False),
        ungated_operations=ungated,
        trial_release_abandoned=_flag(
            src, "EARWITNESS_BILLING_TRIAL_RELEASE_ABANDONED", False),
        trial_reservation_ttl_s=max(1, ttl),
        trial_cancelled_counts=_flag(
            src, "EARWITNESS_BILLING_TRIAL_CANCELLED_COUNTS", True),
        trial_expired_counts=_flag(
            src, "EARWITNESS_BILLING_TRIAL_EXPIRED_COUNTS", True),
        prior_subscription_blocks_trial=_flag(
            src, "EARWITNESS_BILLING_PRIOR_SUBSCRIPTION_BLOCKS_TRIAL", True),
        comped_workspaces=comped,
        pool_workspaces=pools,
        inapp_checkout=_flag(src, "EARWITNESS_BILLING_INAPP_CHECKOUT", False),
        portal_configuration=str(
            src.get("EARWITNESS_BILLING_PORTAL_CONFIGURATION") or "").strip(),
    )


def _id_list(src, name: str) -> tuple:
    """A comma-separated list of exact workspace IDs (blanks dropped)."""
    return tuple(part.strip() for part in str(src.get(name) or "").split(",")
                 if part.strip())


# Trial history (one row per workspace) lives in its own table, listed in
# db.OPTIONAL_TABLES: a backup made before it existed still restores, and
# billing_entitlement keeps exactly the columns every older backup has.
# An older database whose billing_entitlement carries trial columns from
# an earlier build keeps working; those columns are never read.
_ENTITLEMENT_COLUMNS = (
    "workspace_id", "customer_id", "status", "price_id", "trial_end",
    "current_period_end", "past_due_since", "cancel_at_period_end",
    "updated_at")


def ensure_billing_schema(con) -> None:
    for statement in _BILLING_DDL.split(";"):
        if statement.strip():
            con.execute(statement)
    cols = {r[1] for r in con.execute("PRAGMA table_info(billing_events)")}
    if "subscription_id" not in cols:
        con.execute("ALTER TABLE billing_events"
                    " ADD COLUMN subscription_id TEXT NOT NULL DEFAULT ''")
    cols = {r[1] for r in con.execute("PRAGMA table_info(billing_events)")}
    if "reason" not in cols:
        con.execute("ALTER TABLE billing_events"
                    " ADD COLUMN reason TEXT NOT NULL DEFAULT ''")


def _require_billing_schema(con):
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"billing_events", "billing_entitlement", "billing_bindings", "billing_subscriptions"} <= tables:
        raise ValueError("billing schema requires explicit workspace setup")


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class ProviderError(Exception):
    """The current provider resource could not be read unambiguously."""


class ProviderNotFound(ProviderError):
    """The provider reports no such subscription."""


class ProviderTimeout(ProviderError):
    """A Stripe call passed its deadline (design 8.5)."""


class ProviderTooLarge(ProviderError):
    """A Stripe reply passed its size cap (design 8.5)."""


class ProviderEvidenceError(ProviderError):
    """A window read whose answer is not valid evidence for the request that
    produced it (BY3-r18): another object's id, another customer, a field
    the window reads without its exact JSON type, or a list that does not
    say it is complete (BY3-r19). `kind` (subscription, subscription_list,
    customer, invoice, payment_intent, charge, payment_link) and `field`
    come from a fixed vocabulary; nothing else of the answer is carried,
    except `subscription_id`: the id of the one listed subscription the
    error is about, when it is a well-formed id, else "" (code review r21
    NOTE-1). `mismatch` is True for another object or customer, False for
    a malformed field. Never a ProviderNotFound."""

    def __init__(self, kind: str, field: str, *, mismatch: bool = False,
                 subscription_id: str = ""):
        super().__init__(f"provider_evidence_invalid:{kind}:{field}")
        self.kind, self.field, self.mismatch = kind, field, mismatch
        self.subscription_id = subscription_id


class ProviderBusy(Exception):
    """No provider slot or lock entry is free; the caller answers 503."""


def _resolve(host: str, port: int) -> list:
    """Name resolution. NOT bounded by the deadline: getaddrinfo cannot be
    aborted from the standard library (the named gap, docs/hosting/billing.md)."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class _CallSocket:
    """The socket a call currently owns, published to its deadline timer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sock = None
        self.expired = False

    def publish(self, sock) -> bool:
        """Hand `sock` to the timer. True when the deadline already passed."""
        with self._lock:
            self._sock = sock
            return self.expired

    def abort(self) -> None:
        with self._lock:
            self.expired = True
            sock = self._sock
        if sock is None:
            return
        try:
            # The plain socket shutdown, not SSLSocket's: it wakes a thread
            # blocked in connect, the handshake or a read.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def bounded_request(method: str, url: str, *, headers: dict | None = None,
                    body: bytes | None = None, idempotency_key: str = "",
                    deadline_s: float | None = None,
                    ssl_context: ssl.SSLContext | None = None) -> tuple:
    """One Stripe HTTPS exchange inside one deadline (design 8.5).

    Returns (status, body). The deadline starts before any network step.
    A timer shuts down and closes whatever socket the call owns when it
    fires: the raw socket is published before connect, the TLS socket
    before the handshake, so connect, handshake, status line, headers and
    body are all cut. A deadline that passed before publishing is seen
    right after it. A reply over PROVIDER_MAX_REPLY_BYTES raises
    ProviderTooLarge; an error body is read only up to
    PROVIDER_MAX_ERROR_BYTES. Name resolution is not bounded (the named
    DNS gap). Standard library only."""
    total = PROVIDER_DEADLINE_S if deadline_s is None else deadline_s
    started = time.monotonic()
    owner = _CallSocket()
    timer = threading.Timer(total, owner.abort)
    timer.daemon = True
    timer.start()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        timer.cancel()
        raise ProviderError("Stripe URL must be https")
    port = parts.port or 443
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    payload = body or b""
    lines = [f"{method} {target} HTTP/1.1", f"Host: {parts.netloc}",
             f"Stripe-Version: {STRIPE_VERSION}", "Connection: close",
             f"Content-Length: {len(payload)}"]
    if idempotency_key:
        lines.append(f"Idempotency-Key: {idempotency_key}")
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload
    sock = resp = None
    try:
        try:
            infos = _resolve(parts.hostname, port)
        except OSError as exc:
            raise ProviderError("Stripe address did not resolve") from exc
        if not infos:
            raise ProviderError("Stripe address did not resolve")
        family, kind, proto, _, address = infos[0]
        sock = socket.socket(family, kind, proto)
        if owner.publish(sock):
            raise ProviderTimeout("Stripe call passed its deadline")
        sock.connect(address)
        context = ssl_context or _tls_context()
        sock = context.wrap_socket(sock, server_hostname=parts.hostname,
                                   do_handshake_on_connect=False)
        if owner.publish(sock):
            raise ProviderTimeout("Stripe call passed its deadline")
        sock.do_handshake()
        sock.sendall(request)
        resp = http.client.HTTPResponse(sock, method=method)
        resp.begin()
        error = not 200 <= resp.status < 300
        cap = PROVIDER_MAX_ERROR_BYTES if error else PROVIDER_MAX_REPLY_BYTES
        data = bytearray()
        while len(data) <= cap:
            chunk = resp.read(min(PROVIDER_CHUNK_BYTES, cap + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > cap:
            if error:
                del data[cap:]
            else:
                raise ProviderTooLarge("Stripe reply is too large")
        if owner.expired:
            raise ProviderTimeout("Stripe call passed its deadline")
        return resp.status, bytes(data)
    except ProviderError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if owner.expired or isinstance(exc, TimeoutError) \
                or time.monotonic() - started >= total:
            raise ProviderTimeout("Stripe call passed its deadline") from exc
        raise ProviderError("Stripe call failed") from exc
    finally:
        timer.cancel()
        if resp is not None:
            resp.close()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _stripe_request(config: BillingConfig, method: str, path: str,
                    fields: dict, *, idempotency_key: str = "") -> dict:
    """Form-encoded Stripe call through bounded_request, with the secret
    key as basic-auth username. Card data never passes through. An HTTP
    error raises ValueError with Stripe's message; a timeout, an over-size
    reply or a broken connection raises ProviderError."""
    if not config.configured:
        raise ValueError("billing is not configured")
    return _stripe_call(config.secret_key, method, path, fields,
                        idempotency_key=idempotency_key)


def _stripe_call(secret_key: str, method: str, path: str, fields,
                 *, idempotency_key: str = "") -> dict:
    """The one Stripe exchange behind _stripe_request and the window's
    StripeWindowProtocol: form-encoded, bounded (design 8.5), with
    Stripe-Version, the key as basic-auth username. `fields` is a dict or a
    list of pairs (repeated keys such as expand[])."""
    if not secret_key:
        raise ValueError("billing is not configured")
    encoded = urllib.parse.urlencode(fields)
    credentials = base64.b64encode(f"{secret_key}:".encode()).decode()
    url = STRIPE_API + path
    body = None
    if method == "POST":
        body = encoded.encode()
    elif encoded:
        url += "?" + encoded
    headers = {"Authorization": f"Basic {credentials}",
               "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    status, data = bounded_request(method, url, headers=headers, body=body,
                                   idempotency_key=idempotency_key)
    if not 200 <= status < 300:
        try:
            detail = json.loads(data.decode()).get("error", {})
        except (ValueError, UnicodeDecodeError, AttributeError):
            detail = {}
        message = detail.get("message") if isinstance(detail, dict) else ""
        raise ValueError(f"stripe request failed: {message or status}")
    try:
        return json.loads(data.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("Stripe reply is not JSON") from exc


def _check_return(config: BillingConfig, url: str, name: str) -> str:
    from . import browser_auth as _ba
    parsed = urllib.parse.urlparse(url or "")
    if not url or parsed.scheme or parsed.netloc:
        raise ValueError(f"billing {name} URL must be a local route")
    if not _ba.allowed_return(url):
        raise ValueError(f"billing {name} URL is not an allowed route")
    if name == "success" and config.success_url and url != config.success_url:
        raise ValueError("billing success URL does not match configuration")
    if name == "cancel" and config.cancel_url and url != config.cancel_url:
        raise ValueError("billing cancel URL does not match configuration")
    return url


def validate_checkout(config: BillingConfig, *, price_id: str,
                      success_url: str = "", cancel_url: str = "",
                      trial_days: int = 0) -> tuple:
    """Every caller-input check of a checkout, with no provider call and
    no state change, so a bad request can never consume a trial
    reservation. Returns (success_url, cancel_url, offered_trial_days)."""
    if not config.configured:
        raise ValueError("billing is not configured")
    if price_id not in (config.prices or {}):
        raise ValueError("unknown price: not in the configured catalog")
    success = _check_return(config, success_url or config.success_url,
                            "success")
    cancel = _check_return(config, cancel_url or config.cancel_url, "cancel")
    # The trial is the catalog's offer for this price, never the caller's:
    # a caller value is accepted only when it names that same offer. A
    # price without a catalog trial field offers no trial.
    meta = config.prices[price_id]
    offered = meta.get("trial_days", 0) if isinstance(meta, dict) else 0
    if not isinstance(offered, int) or isinstance(offered, bool) \
            or offered < 0 or offered > 365:
        raise ValueError("catalog trial_days must be 0..365")
    if trial_days and trial_days != offered:
        raise ValueError("trial_days must match the configured price offer")
    return success, cancel, offered


def create_checkout_session(config: BillingConfig, *, customer_id: str = "",
                            price_id: str, workspace_id: str,
                            success_url: str = "", cancel_url: str = "",
                            trial_days: int = 0, allow_trial: bool = True,
                            trial_reservation_id: str = "") -> dict:
    """Create a Stripe Checkout session for a configured price. Amounts
    come from the configured price catalog; nothing is invented here.

    `allow_trial=False` (the trial was not reserved for this history)
    sends no trial. `trial_reservation_id` rides on the session and
    subscription metadata so a session-expired event can name the
    reservation it belongs to."""
    success, cancel, offered = validate_checkout(
        config, price_id=price_id, success_url=success_url,
        cancel_url=cancel_url, trial_days=trial_days)
    # Workspace binding travels on three legs: client_reference_id and
    # Session metadata are supplemental only. The binding the reconciler
    # trusts lives on the Subscription itself, so it must be set here via
    # subscription_data — otherwise a normal first purchase fails closed
    # because the current Subscription resource names no workspace.
    fields = {"mode": "subscription", "line_items[0][price]": price_id,
              "line_items[0][quantity]": "1",
              "success_url": success + "?session_id={CHECKOUT_SESSION_ID}",
              "cancel_url": cancel,
              "client_reference_id": workspace_id,
              "metadata[workspace_id]": workspace_id,
              "subscription_data[metadata][workspace_id]": workspace_id}
    if customer_id:
        fields["customer"] = customer_id
    else:
        fields["customer_creation"] = "always"
    if offered and allow_trial:
        fields["subscription_data[trial_period_days]"] = str(offered)
        if trial_reservation_id:
            fields["metadata[trial_reservation]"] = trial_reservation_id
            fields["subscription_data[metadata][trial_reservation]"] = \
                trial_reservation_id
    session = _stripe_request(config, "POST", "/checkout/sessions", fields)
    if config.livemode_required and not session.get("livemode"):
        raise ValueError("refusing test-mode checkout as live success")
    return {"checkout_session_id": session.get("id"),
            "checkout_url": session.get("url"),
            "livemode": bool(session.get("livemode"))}


def create_portal_session(config: BillingConfig, *, customer_id: str,
                          return_url: str = "") -> dict:
    if not config.configured:
        raise ValueError("billing is not configured")
    if not customer_id:
        raise ValueError("a Stripe customer is required for the portal")
    if not config.portal_configuration:
        raise ValueError("the billing portal is not configured")
    ret = _check_return(config, return_url or config.success_url, "success")
    session = _stripe_request(config, "POST", "/billing_portal/sessions",
                              {"customer": customer_id,
                               "configuration": config.portal_configuration,
                               "return_url": ret})
    if config.livemode_required and not session.get("livemode"):
        raise ValueError("refusing test-mode portal as live success")
    return {"portal_url": session.get("url"),
            "livemode": bool(session.get("livemode"))}


def verify_webhook_signature(config: BillingConfig, payload: bytes,
                             header: str, *, now: float | None = None) -> None:
    """Stripe signature scheme: t=timestamp,v1=hmac; recent timestamp;
    constant-time compare over every v1 candidate."""
    if not config.webhook_secret:
        raise ValueError("billing webhook is not configured")
    ts: int | None = None
    candidates: list[str] = []
    for part in str(header or "").split(","):
        key, _, value = part.partition("=")
        if key.strip() == "t":
            try:
                ts = int(value.strip())
            except ValueError:
                ts = None
        elif key.strip() == "v1" and value.strip():
            candidates.append(value.strip())
    moment = now if now is not None else time.time()
    if ts is None or abs(moment - ts) > WEBHOOK_TOLERANCE_S:
        raise ValueError("webhook timestamp is missing or too old")
    if not candidates:
        raise ValueError("webhook has no signatures")
    signed = f"{ts}.".encode() + bytes(payload or b"")
    expected = hmac.new(config.webhook_secret.encode(), signed,
                        hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise ValueError("webhook signature mismatch")


class TenantMismatch(Exception):
    """A delivery or a binding is not verified for this workspace's billing
    identity. Raised before anything is written; the webhook route answers
    it exactly like an unknown workspace id."""


# The binder name the verified first-purchase rule records. Hosted
# deliveries stamp it with the billing identity they were verified for
# (webhook_binder); a binding counts only for that exact identity. The bare
# name is a legacy or module-level binding, never verified for a hosted
# workspace. Any other binder is an explicit operator bind.
WEBHOOK_BINDER = "webhook-first-purchase"


def webhook_binder(tenant: str) -> str:
    """The bound_by value of a binding verified for billing identity
    `tenant` (same 120-character bound as bind_billing_customer)."""
    return f"{WEBHOOK_BINDER}:{tenant}"[:120]


def workspace_billing_identity(workspace_id: str, db_identity: str) -> str:
    """The provider-facing identity of a hosted workspace:
    '<external workspace id>:<database identity>'. The row namespace
    ('local') is shared by every workspace and is never used here.

    The database identity is random per database file and checked against
    the registry on every open; the external id is unique in the registry.
    Together they differ for a reused or retired external id (a new file)
    and for a copied file registered under another id. '' when either part
    is missing, which every billing path refuses."""
    if not workspace_id or not db_identity:
        return ""
    return f"{workspace_id}:{db_identity}"


class SubscriptionProtocol:
    """Current-resource provider protocol: signed webhooks trigger,
    this protocol verifies. Returns a normalized subscription:

    {"id", "customer", "status", "price_id", "current_period_end",
     "trial_end", "cancel_at_period_end", "metadata_workspace"} plus the
    optional provider times "canceled_at" and "ended_at" (0 when absent).
    Raises ProviderNotFound for unknown ids, ProviderError for any
    other failure. Never invents a resource.
    """

    def get_subscription(self, subscription_id: str) -> dict:
        raise NotImplementedError


class StripeSubscriptionProtocol(SubscriptionProtocol):
    """Live Stripe current-resource reads over the existing stdlib
    adapter. No SDK, no traffic except this explicit verification."""

    def __init__(self, config: BillingConfig):
        if not config.secret_key:
            raise ValueError("billing is not configured")
        self._config = config

    def get_subscription(self, subscription_id: str) -> dict:
        if not subscription_id:
            raise ProviderError("subscription id is required")
        try:
            sub = _stripe_request(self._config, "GET",
                                  f"/subscriptions/{subscription_id}", {})
        except ValueError as exc:
            if "no such subscription" in str(exc).lower():
                raise ProviderNotFound(
                    f"unknown subscription: {subscription_id}") from exc
            raise ProviderError(f"provider read failed: {exc}") from exc
        # ProviderError (timeout, size, broken connection) passes through.
        if not isinstance(sub, dict) or not sub.get("id"):
            raise ProviderError("provider returned no subscription")
        return _normalize_subscription(sub)


def period_unreadable(resource: dict) -> bool:
    """True when an active or past_due current resource has no valid
    positive paid-period end. Such a resource is unreadable (design 8.6):
    the stored state stands and the delivery stays retryable. An end is
    never derived from event data."""
    if resource.get("status") not in ("active", "past_due"):
        return False
    try:
        return int(resource.get("current_period_end") or 0) <= 0
    except (TypeError, ValueError):
        return True


def _normalize_subscription(sub: dict) -> dict:
    customer = sub.get("customer") or ""
    if isinstance(customer, dict):
        customer = customer.get("id") or ""
    items = ((sub.get("items") or {}).get("data") or [])
    item = (items[0] or {}) if items else {}
    price_id = str((item.get("price") or {}).get("id") or "") if item else ""

    def _int(key: str, holder=None) -> int:
        try:
            return max(0, int((sub if holder is None else holder).get(key) or 0))
        except (TypeError, ValueError):
            return 0

    # STRIPE_VERSION keeps the period on the subscription item; an older
    # shape kept it on the subscription.
    period = _int("current_period_end", item) or _int("current_period_end")
    return {"id": str(sub.get("id") or ""),
            "customer": str(customer or ""),
            "status": str(sub.get("status") or ""),
            "price_id": price_id,
            "current_period_end": period,
            "cancel_at": _int("cancel_at"),
            "trial_end": _int("trial_end"),
            "cancel_at_period_end": bool(sub.get("cancel_at_period_end")),
            "canceled_at": _int("canceled_at"),
            "ended_at": _int("ended_at"),
            "metadata_workspace": str(
                ((sub.get("metadata") or {}).get("workspace_id")) or "")}


class FixtureSubscriptionProtocol(SubscriptionProtocol):
    """Local provider protocol fixture for tests and offline proof.

    Serves only explicitly scripted resources; unscripted ids raise
    ProviderError (never a silent success), and scripted failures
    raise what the scenario needs. No network, no SDK.
    """

    def __init__(self, resources: dict | None = None,
                 failures: dict | None = None, *, delay: float = 0.0,
                 block: threading.Event | None = None):
        self._resources = {k: dict(v) for k, v in (resources or {}).items()}
        self._failures = dict(failures or {})
        self.calls: list[str] = []
        # A slow provider: sleep `delay` seconds, or wait until `block` is
        # set, inside the call (on the caller's thread).
        self.delay = delay
        self.block = block

    def get_subscription(self, subscription_id: str) -> dict:
        self.calls.append(subscription_id)
        if self.block is not None:
            self.block.wait(30)
        if self.delay:
            time.sleep(self.delay)
        if subscription_id in self._failures:
            raise self._failures[subscription_id]
        if subscription_id not in self._resources:
            raise ProviderError(
                f"no fixture resource: {subscription_id}")
        return dict(self._resources[subscription_id])


# -- The nightly window's Stripe calls (purchase design 5.3, BY3) ----------
#
# Only the window commands (scripts/app_purchases.py) use these, with the
# window key (STRIPE_SECRET_KEY from /etc/earwitness/window-purchase.env,
# design 10.2 item 6). Every call goes through _stripe_call, so it is
# bounded (8.5) and sends Stripe-Version. Shapes follow STRIPE_VERSION:
# the period sits on the subscription item, and an invoice names its
# payments under `payments` (expanded), not a top-level charge.

WINDOW_LIST_PAGE = 100
WINDOW_LIST_MAX_PAGES = 20
# A subscription id an evidence error may carry (as scripts/app_purchases.SUB_RE).
_LISTED_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,128}$")


def normalize_window_subscription(sub: dict) -> dict:
    """_normalize_subscription plus what the window checks: the offer tag,
    the item count and quantity, the latest invoice, the period start and
    the creation time. `cancel_at` 0 means none (null). `malformed` names
    the first window-read field without its exact type (BY3-r18); a broken
    structure normalizes as empty and is refused by check_window_subscription.
    A malformed latest invoice reads as none (never a paid invoice)."""
    malformed = _window_subscription_malformed(sub)
    if malformed in ("items", "items.has_more", "price", "metadata"):
        sub = dict(sub, items={"data": []}, metadata=None)
    out = _normalize_subscription(sub)
    items = ((sub.get("items") or {}).get("data") or [])
    item = (items[0] or {}) if items else {}

    def _int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    invoice = _ref(sub.get("latest_invoice"))[0]
    metadata = sub.get("metadata") or {}
    out.update({
        "offer": str(metadata.get("offer") or "") if isinstance(metadata, dict) else "",
        "item_count": len(items),
        "quantity": _int(item.get("quantity")) if item else 0,
        "latest_invoice": str(invoice or ""),
        "current_period_start": _int(item.get("current_period_start"))
        or _int(sub.get("current_period_start")),
        "created": _int(sub.get("created")),
        # The refund step's fields with their exact types (Astra r16 MED-1):
        # a string id and status, a well-formed customer and latest invoice.
        "well_formed": (_own_id(sub.get("id")) != "" and isinstance(sub.get("status"), str)
                        and _ref(sub.get("customer"))[1]
                        and _ref(sub.get("latest_invoice"))[1]),
        # The end-date step's own fields (code review r17 NOTE-1): a string
        # id and status, and the dates its cancel_at computation reads, each
        # null or an int >= 0. The refund fields are not among them.
        "end_well_formed": (_own_id(sub.get("id")) != "" and isinstance(sub.get("status"), str)
                            and _null_or_amount(sub.get("cancel_at"))
                            and _null_or_amount(item.get("current_period_start"))
                            and _null_or_amount(sub.get("current_period_start"))),
        # The period end cancel-request stores as a target (Astra r19
        # MED-1), with the same exact types.
        "period_end_well_formed": (_null_or_amount(item.get("current_period_end"))
                                   and _null_or_amount(sub.get("current_period_end"))),
        # BY3-r18: the first field the window reads that lacks its exact
        # type; "" when none. check_window_subscription refuses any.
        "malformed": malformed,
    })
    return out


# The integer fields of a subscription the window reads (the end date, the
# period, reconciliation's trial end and the terminal times), each null or an
# int >= 0 that is not a bool.
_SUBSCRIPTION_TIMES = ("current_period_start", "current_period_end", "cancel_at", "trial_end",
                       "canceled_at", "ended_at", "created")


def _window_subscription_malformed(sub: dict) -> str:
    """The first field the window reads (the seat check, admission, the end
    date and reconciliation) without its exact JSON type, else "". The
    latest invoice is not among them: it is a refund-only and admission
    field, checked where it is read (the `well_formed` flag; an absent or
    malformed one is never a paid invoice), so a live refund row's billing
    end never depends on it (BY3-r15)."""
    if _own_id(sub.get("id")) == "":
        return "id"
    if not isinstance(sub.get("status"), str):
        return "status"
    customer, ok = _ref(sub.get("customer"))
    if not ok or not customer:
        return "customer"
    items = sub.get("items")
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
        return "items"
    # A complete list only (BY3-r19): `has_more` exactly false. A truncated
    # list is not evidence of a one-item purchase; items are never paged.
    if items.get("has_more") is not False:
        return "items.has_more"
    if data:
        price = data[0].get("price")
        if not isinstance(price, dict) or _own_id(price.get("id")) == "":
            return "price"
        for key in ("quantity", "current_period_start", "current_period_end"):
            if not _null_or_amount(data[0].get(key)):
                return "item." + key
    for key in _SUBSCRIPTION_TIMES:
        if not _null_or_amount(sub.get(key)):
            return key
    # The resource flag, never the stored column (the BY1 column-read guard).
    flag = ("cancel_at_period_end", sub.get("cancel_at_period_end"))
    if not isinstance(flag[1], (bool, type(None))):
        return flag[0]
    metadata = sub.get("metadata")
    if metadata is None:
        return ""
    if not isinstance(metadata, dict):
        return "metadata"
    for key in ("offer", "workspace_id"):
        if not isinstance(metadata.get(key), (str, type(None))):
            return "metadata." + key
    return ""


def _ref_id(value) -> str:
    """The id of a Stripe reference: the string itself or an expanded
    object's string `id`; "" when absent or not well formed."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) else ""


# Refund evidence takes each field with its exact JSON type (Astra r16
# MED-1): nothing is coerced with bool(), int() or str(). A malformed value
# makes the object not well formed, which the refund step treats as
# incomplete evidence (scripts/app_purchases.refund_evidence).

def _own_id(value) -> str:
    """An object's own `id`: a nonempty string, else ""."""
    return value if isinstance(value, str) and value else ""


def _ref(value) -> tuple:
    """(id, ok) for a Stripe reference. Absent (missing or null) is ("", True);
    a nonempty string, or an expanded object whose `id` is one, is (id, True);
    anything else is ("", False)."""
    if value is None:
        return "", True
    found = _own_id(value.get("id") if isinstance(value, dict) else value)
    return found, found != ""


def _amount(value) -> tuple:
    """(amount, ok): ok only for an int that is not a bool and is >= 0."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value, True
    return 0, False


def _null_or_amount(value) -> bool:
    """A nullable integer field: null (or absent) or an int >= 0 that is not a bool."""
    return value is None or _amount(value)[1]


# The invoice-payment statuses of STRIPE_VERSION that carry no paid payment,
# so the refund step may skip them. Any other status (missing, null,
# non-string or unrecognized) leaves the invoice incomplete. The vocabulary
# (paid, open, canceled) is unverified until G4 (docs/hosting/purchases.md).
INVOICE_PAYMENT_UNPAID_STATUSES = frozenset({"open", "canceled"})


def normalize_invoice(invoice: dict) -> dict:
    """{"id", "customer", "status", "charges", "payment_intents", "complete"}
    over the invoice's paid payments (STRIPE_VERSION: invoice.payments, each
    with a typed `payment`): `charges` holds the charge IDs of `type: charge`
    payments, `payment_intents` the payment-intent IDs of `type:
    payment_intent` payments (a Payment Link or Checkout card payment), whose
    charge is the intent's latest_charge (StripeWindowProtocol.
    get_payment_intent). `complete` is False when the list is truncated
    (`has_more`), an entry is not an object or its status is neither `paid`
    nor in INVOICE_PAYMENT_UNPAID_STATUSES, or a paid payment has no
    well-formed reference (unknown type, non-object payment, missing or
    non-string ID): the paid payments are then not all known, so the invoice
    is no refund evidence. So is a malformed container or identity: `payments`
    not an object, `payments.data` not a list, `has_more` not the boolean
    false, or a non-string `id` or `customer`."""
    charges, intents = [], []
    payments = invoice.get("payments")
    payments = payments if isinstance(payments, dict) else {}
    data = payments.get("data")
    data = data if isinstance(data, list) else None
    invoice_id, customer = _own_id(invoice.get("id")), _ref(invoice.get("customer"))
    complete = (data is not None and payments.get("has_more") is False
                and invoice_id != "" and customer[1])
    for entry in (data or []):
        if not isinstance(entry, dict):
            complete = False
            continue
        status = entry.get("status")
        if status != "paid":
            if not (isinstance(status, str) and status in INVOICE_PAYMENT_UNPAID_STATUSES):
                complete = False
            continue
        payment = entry.get("payment")
        kind = payment.get("type") if isinstance(payment, dict) else None
        ref = _ref_id(payment.get(kind)) if kind in ("charge", "payment_intent") else ""
        if not ref:
            complete = False
        elif kind == "charge":
            charges.append(ref)
        else:
            intents.append(ref)
    return {"id": invoice_id, "customer": customer[0],
            "status": str(invoice.get("status") or ""), "charges": charges,
            "payment_intents": intents, "complete": complete}


def normalize_payment_intent(intent: dict) -> dict:
    """{"id", "customer", "latest_charge", "well_formed"}; a reference is ""
    when absent. `well_formed` is False for a non-string id or a malformed
    customer or latest_charge."""
    customer, latest = _ref(intent.get("customer")), _ref(intent.get("latest_charge"))
    intent_id = _own_id(intent.get("id"))
    return {"id": intent_id, "customer": customer[0], "latest_charge": latest[0],
            "well_formed": intent_id != "" and customer[1] and latest[1]}


def normalize_charge(charge: dict) -> dict:
    """{"id", "customer", "payment_intent", "amount", "amount_refunded",
    "refunded", "well_formed"}; a reference is "" when absent. `refunded` is
    True only for the JSON boolean true. `well_formed` is False for a
    non-string id, a malformed customer or payment_intent, an `amount` or
    `amount_refunded` that is not an int >= 0 (a bool, string or float is
    not), or a present `refunded` that is not a boolean. It is also False
    when a boolean `refunded` contradicts a positive `amount` (Astra r17
    MED-1, BY3-r15): true while less than the amount is refunded, or false
    while all of it is. Inconsistent evidence is incomplete, never a refund."""
    customer, intent = _ref(charge.get("customer")), _ref(charge.get("payment_intent"))
    amount, amount_refunded = _amount(charge.get("amount")), _amount(charge.get("amount_refunded"))
    flag_ok = "refunded" not in charge or isinstance(charge["refunded"], bool)
    if flag_ok and "refunded" in charge and amount[1] and amount_refunded[1] and amount[0] > 0:
        flag_ok = charge["refunded"] is (amount_refunded[0] >= amount[0])
    charge_id = _own_id(charge.get("id"))
    return {"id": charge_id, "customer": customer[0], "payment_intent": intent[0],
            "amount": amount[0], "amount_refunded": amount_refunded[0],
            "refunded": charge.get("refunded") is True,
            "well_formed": (charge_id != "" and customer[1] and intent[1] and amount[1]
                            and amount_refunded[1] and flag_ok)}


def charge_fully_refunded(charge: dict) -> bool:
    """A well-formed charge whose `refunded` is true, or whose amounts show
    the whole positive amount refunded (a partial refund is not)."""
    if charge.get("well_formed") is not True:
        return False
    return charge["refunded"] is True or (
        charge["amount"] > 0 and charge["amount_refunded"] >= charge["amount"])


# -- The window adapter's validators (BY3-r18) ------------------------------
#
# Every window read is checked here, for the request that produced it, before
# any caller sees it: the answer's id is the one requested (a list item: the
# requested price), its customer is the expected one when the caller names
# it, and every field the window reads has its exact JSON type. Any failure
# raises ProviderEvidenceError; no partial answer is returned. The live
# adapter (StripeWindowProtocol) and the test fixture call these same
# functions. Subscriptions are checked after normalize_window_subscription
# (which records the malformed field), so a caller can check one again;
# the others take the raw object.

WINDOW_VALIDATORS = ("check_window_subscription", "check_listed_subscription",
                     "check_customer", "check_invoice", "check_payment_intent",
                     "check_charge", "check_payment_link")


def check_window_subscription(sub: dict, subscription_id: str, *,
                              customer: str | None = None) -> dict:
    """A normalized subscription that is `subscription_id`, of `customer`
    when given, with every window-read field well typed; else
    ProviderEvidenceError."""
    if sub.get("malformed"):
        raise ProviderEvidenceError("subscription", sub["malformed"])
    if sub.get("id") != subscription_id:
        raise ProviderEvidenceError("subscription", "id", mismatch=True)
    if customer is not None and sub.get("customer") != customer:
        raise ProviderEvidenceError("subscription", "customer", mismatch=True)
    return sub


def check_listed_subscription(sub: dict, price_id: str) -> dict:
    """A normalized list item: well typed, its (first) item at `price_id`.
    The error names the item by its id when that is a well-formed one
    (code review r21 NOTE-1)."""
    sid = sub.get("id", "") if sub.get("malformed") != "id" else ""
    sid = sid if _LISTED_ID_RE.match(sid) else ""
    if sub.get("malformed"):
        raise ProviderEvidenceError("subscription", sub["malformed"], subscription_id=sid)
    if sub.get("price_id") != price_id:
        raise ProviderEvidenceError("subscription", "price", mismatch=True, subscription_id=sid)
    return sub


def check_customer(customer: dict, customer_id: str) -> dict:
    """{"id", "email", "deleted": False} of customer `customer_id`: not
    deleted, with a nonempty string email (its content is the email rule's,
    judged by discovery)."""
    found = _own_id(customer.get("id"))
    if not found:
        raise ProviderEvidenceError("customer", "id")
    deleted = customer.get("deleted")
    if not isinstance(deleted, (bool, type(None))):
        raise ProviderEvidenceError("customer", "deleted")
    if found != customer_id:
        raise ProviderEvidenceError("customer", "id", mismatch=True)
    if deleted is True:
        raise ProviderEvidenceError("customer", "deleted")
    email = customer.get("email")
    if not isinstance(email, str) or not email:
        raise ProviderEvidenceError("customer", "email")
    return {"id": found, "email": email, "deleted": False}


def check_invoice(invoice: dict, invoice_id: str, *, customer: str | None = None) -> dict:
    """normalize_invoice of invoice `invoice_id`, of `customer` when given,
    with a string status and a complete payment list."""
    out = normalize_invoice(invoice)
    if not out["id"]:
        raise ProviderEvidenceError("invoice", "id")
    if not _ref(invoice.get("customer"))[1]:
        raise ProviderEvidenceError("invoice", "customer")
    if not isinstance(invoice.get("status"), str):
        raise ProviderEvidenceError("invoice", "status")
    if not out["complete"]:
        raise ProviderEvidenceError("invoice", "payments")
    if out["id"] != invoice_id:
        raise ProviderEvidenceError("invoice", "id", mismatch=True)
    if customer is not None and out["customer"] != customer:
        raise ProviderEvidenceError("invoice", "customer", mismatch=True)
    return out


def _first_bad(kind: str, checks) -> None:
    for field, ok in checks:
        if not ok:
            raise ProviderEvidenceError(kind, field)


def _optional_customer(kind: str, found: str, customer: str | None) -> None:
    """An intent's or a charge's customer may be absent (refund_evidence's
    optional check); a present one must be `customer` when given."""
    if customer is not None and found and found != customer:
        raise ProviderEvidenceError(kind, "customer", mismatch=True)


def check_payment_intent(intent: dict, intent_id: str, *,
                         customer: str | None = None) -> dict:
    out = normalize_payment_intent(intent)
    if not out["well_formed"]:
        _first_bad("payment_intent", (
            ("id", out["id"] != ""), ("customer", _ref(intent.get("customer"))[1]),
            ("latest_charge", _ref(intent.get("latest_charge"))[1])))
    if out["id"] != intent_id:
        raise ProviderEvidenceError("payment_intent", "id", mismatch=True)
    _optional_customer("payment_intent", out["customer"], customer)
    return out


def check_charge(charge: dict, charge_id: str, *, customer: str | None = None) -> dict:
    out = normalize_charge(charge)
    if not out["well_formed"]:
        _first_bad("charge", (
            ("id", out["id"] != ""), ("customer", _ref(charge.get("customer"))[1]),
            ("payment_intent", _ref(charge.get("payment_intent"))[1]),
            ("amount", _amount(charge.get("amount"))[1]),
            ("amount_refunded", _amount(charge.get("amount_refunded"))[1]),
            ("refunded", False)))
    if out["id"] != charge_id:
        raise ProviderEvidenceError("charge", "id", mismatch=True)
    _optional_customer("charge", out["customer"], customer)
    return out


def check_payment_link(link: dict, link_id: str) -> dict:
    """{"id", "active"} of Payment Link `link_id`, `active` a JSON boolean."""
    found = _own_id(link.get("id"))
    if not found:
        raise ProviderEvidenceError("payment_link", "id")
    if not isinstance(link.get("active"), bool):
        raise ProviderEvidenceError("payment_link", "active")
    if found != link_id:
        raise ProviderEvidenceError("payment_link", "id", mismatch=True)
    return {"id": found, "active": link["active"]}


class StripeWindowProtocol:
    """The window's Stripe reads and writes: subscription list, read and
    update; customer read; invoice, payment intent and charge read; Payment
    Link read and update. Not-found raises ProviderNotFound; an answer that is
    not valid evidence for the request raises ProviderEvidenceError (the
    validators above); anything else that is not a clean answer raises
    ProviderError (a write is then ambiguous: read again before any retry,
    design 7.2)."""

    def __init__(self, secret_key: str):
        if not secret_key:
            raise ValueError("billing is not configured")
        self._key = secret_key

    def _call(self, method: str, path: str, fields=(), idempotency_key: str = ""):
        # Stripe's message text is never carried on (BY3 r3, R1-09): it can
        # hold an email or a key fragment. Only a fixed name leaves here.
        try:
            reply = _stripe_call(self._key, method, path, list(fields),
                                 idempotency_key=idempotency_key)
        except ValueError as exc:
            if "no such" in str(exc).lower():
                raise ProviderNotFound("not_found") from None
            raise ProviderError("provider_error") from None
        except ProviderTimeout:
            raise ProviderTimeout("timeout") from None
        except ProviderTooLarge:
            raise ProviderTooLarge("too_large") from None
        except ProviderError:
            raise ProviderError("provider_error") from None
        if not isinstance(reply, dict):
            raise ProviderError("provider_error")
        return reply

    def list_subscriptions(self, price_id: str) -> list:
        out, after = [], ""
        for _ in range(WINDOW_LIST_MAX_PAGES):
            fields = [("price", price_id), ("status", "all"),
                      ("limit", str(WINDOW_LIST_PAGE))]
            if after:
                fields.append(("starting_after", after))
            page = self._call("GET", "/subscriptions", fields)
            data = page.get("data")
            if not isinstance(data, list):
                raise ProviderError("provider_error")
            for d in data:
                if not isinstance(d, dict):
                    raise ProviderEvidenceError("subscription", "object")
                out.append(check_listed_subscription(normalize_window_subscription(d), price_id))
            # The page says whether more follow: exactly a JSON boolean
            # (BY3-r19). Anything else is not evidence the list is complete,
            # and nothing gathered so far is returned.
            more = page.get("has_more")
            if not isinstance(more, bool):
                raise ProviderEvidenceError("subscription_list", "has_more")
            if more is False:
                return out
            after = str((data[-1] or {}).get("id") or "") if data else ""
            if not after:
                break
        raise ProviderError("provider_error")

    def get_subscription(self, subscription_id: str, *, customer: str | None = None) -> dict:
        if not subscription_id:
            raise ProviderError("subscription id is required")
        sub = self._call("GET", f"/subscriptions/{subscription_id}")
        return check_window_subscription(normalize_window_subscription(sub), subscription_id,
                                         customer=customer)

    def update_subscription(self, subscription_id: str, *, workspace_identity: str = "",
                            cancel_at: int = 0, idempotency_key: str,
                            customer: str | None = None) -> dict:
        fields = []
        if workspace_identity:
            fields.append(("metadata[workspace_id]", workspace_identity))
        if cancel_at:
            fields.append(("cancel_at", str(int(cancel_at))))
        sub = self._call("POST", f"/subscriptions/{subscription_id}", fields,
                         idempotency_key=idempotency_key)
        return check_window_subscription(normalize_window_subscription(sub), subscription_id,
                                         customer=customer)

    def get_customer(self, customer_id: str) -> dict:
        return check_customer(self._call("GET", f"/customers/{customer_id}"), customer_id)

    def get_invoice(self, invoice_id: str, *, customer: str | None = None) -> dict:
        return check_invoice(self._call("GET", f"/invoices/{invoice_id}",
                                        [("expand[]", "payments")]), invoice_id,
                             customer=customer)

    def get_payment_intent(self, intent_id: str, *, customer: str | None = None) -> dict:
        return check_payment_intent(self._call("GET", f"/payment_intents/{intent_id}"),
                                    intent_id, customer=customer)

    def get_charge(self, charge_id: str, *, customer: str | None = None) -> dict:
        return check_charge(self._call("GET", f"/charges/{charge_id}"), charge_id,
                            customer=customer)

    def get_payment_link(self, link_id: str) -> dict:
        return check_payment_link(self._call("GET", f"/payment_links/{link_id}"), link_id)

    def update_payment_link(self, link_id: str, *, active: bool,
                            idempotency_key: str) -> dict:
        link = self._call("POST", f"/payment_links/{link_id}",
                          [("active", "true" if active else "false")],
                          idempotency_key=idempotency_key)
        return check_payment_link(link, link_id)


def _event_customer(event: dict) -> str:
    obj = (event.get("data") or {}).get("object") or {}
    return str(obj.get("customer") or event.get("customer") or "")


def _path(obj, path: tuple):
    for key in path:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _event_subscription(event: dict) -> str:
    """Provider subscription the event triggers reconciliation for: the
    object's id for subscription events; for invoices, the
    STRIPE_VERSION field (INVOICE_SUBSCRIPTION_PATH); otherwise the
    object's `subscription` (checkout sessions)."""
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return ""
    kind = str(event.get("type") or "")
    if kind.startswith("customer.subscription."):
        return str(obj.get("id") or "")
    if kind.startswith("invoice."):
        found = _path(obj, INVOICE_SUBSCRIPTION_PATH)
        return found if isinstance(found, str) else ""
    return str(obj.get("subscription") or "")


def _event_identities(event: dict) -> list:
    """Every billing identity the event itself names (checkout session,
    subscription or invoice metadata, client_reference_id)."""
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return []
    found = [obj.get("client_reference_id"),
             (obj.get("metadata") or {}).get("workspace_id")]
    for holder in (obj, obj.get("parent") or {}):
        details = (holder.get("subscription_details") or {}) \
            if isinstance(holder, dict) else {}
        found.append((details.get("metadata") or {}).get("workspace_id")
                     if isinstance(details, dict) else None)
    return [str(v) for v in found if v]


def tenant_customer(con, workspace_id: str, tenant: str) -> str:
    """The provider customer this workspace may act on as `tenant` ('' if
    none). A binding the webhook rule made counts only when it is stamped
    with `tenant` (webhook_binder), which only a delivery whose provider
    resource names `tenant` writes; an explicit operator binding counts as
    is. A binding or an unbound billing record naming a customer that was
    never verified raises TenantMismatch: the tenant is never guessed."""
    _require_billing_schema(con)
    row = con.execute("SELECT customer_id, bound_by FROM billing_bindings"
                      " WHERE workspace_id=?", (workspace_id,)).fetchone()
    customer = (row[0] if row else "") or ""
    if not customer:
        unbound = con.execute("SELECT customer_id FROM billing_entitlement"
                              " WHERE workspace_id=?", (workspace_id,)).fetchone()
        if unbound and unbound[0]:
            raise TenantMismatch("an unbound billing record names a provider"
                                 " customer")
        return ""
    binder = row[1] or ""
    if not binder.startswith(WEBHOOK_BINDER):
        return customer
    if not tenant or binder != webhook_binder(tenant):
        raise TenantMismatch("the billing binding was never verified for this"
                             " workspace")
    return customer


def bound_customer(con, workspace_id: str) -> str:
    """Server-recorded provider customer bound to a workspace ('' if none)."""
    _require_billing_schema(con)
    row = con.execute("SELECT customer_id FROM billing_bindings WHERE workspace_id=?",
                      (workspace_id,)).fetchone()
    return row["customer_id"] if row else ""


def bind_billing_customer(con, workspace_id: str, customer_id: str,
                          operator: str = "") -> dict:
    """Explicit local-operator bind of a provider customer to a workspace.

    Webhooks never create this binding except through the verified
    first-purchase rule in apply_event; until bound, events for the
    workspace reconcile nothing.
    """
    ensure_billing_schema(con)
    if not workspace_id or not customer_id:
        raise ValueError("workspace and provider customer are required")
    prior = bound_customer(con, workspace_id)
    con.execute(
        "INSERT INTO billing_bindings (workspace_id, customer_id, bound_by,"
        " bound_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(workspace_id) DO UPDATE SET customer_id=excluded.customer_id,"
        " bound_by=excluded.bound_by, bound_at=excluded.bound_at",
        (workspace_id, customer_id, str(operator or "")[:120], _utcnow_iso()))
    # A replacement customer never resets trial history: keep the replaced
    # id. A first binding only annotates a history row that already exists.
    _remember_customers(con, workspace_id, [prior, customer_id],
                        create=bool(prior and prior != customer_id))
    return {"workspace_id": workspace_id, "customer_id": customer_id,
            "bound_by": str(operator or "")[:120]}


_reconcile_guard = threading.Lock()
_reconcile_locks: dict[str, threading.Lock] = {}


def _subscription_lock(subscription_id: str) -> threading.Lock:
    """One serialization lock per provider subscription."""
    with _reconcile_guard:
        lock = _reconcile_locks.get(subscription_id)
        if lock is None:
            lock = threading.Lock()
            _reconcile_locks[subscription_id] = lock
        return lock


# Every subscription status Stripe reports (design 8.6). Only `active`
# and `past_due` can pass the gate.
STRIPE_STATUSES = ("active", "past_due", "canceled", "unpaid", "incomplete",
                   "incomplete_expired", "trialing", "paused")

_TRIGGER_TYPES = ("checkout.session.completed", "customer.subscription.created",
                  "customer.subscription.updated", "customer.subscription.deleted",
                  "invoice.paid", "invoice.payment_failed")


def apply_event(con, workspace_id: str, event: dict, *,
                now: float | None = None,
                config: BillingConfig | None = None,
                provider: SubscriptionProtocol | None = None,
                tenant: str | None = None,
                fetched: tuple | None = None) -> dict:
    """Reconcile one verified webhook event against current provider truth.

    The event is a trigger, never entitlement: after signature
    verification the server binds the event's customer/subscription to
    its own workspace record, reads the subscription's CURRENT provider
    resource, checks the configured price, and writes only what the
    provider currently reports, serialized per subscription. Event
    timestamps, delivery order, and event-carried period ends never
    establish entitlement. A replayed event id returns the stored
    outcome without re-applying. Failed or ambiguous reconciliation
    retains the last verified state and returns a refusal outcome;
    unrecognized event types are ignored.

    `tenant` is the workspace's billing identity (hosted callers always
    pass it; see workspace_billing_identity). With it, a delivery is
    written only after its identity is verified: any identity the event
    names must be `tenant`, and a subscription trigger must read a
    current provider resource whose metadata names `tenant` (or, when the
    provider no longer has it, one this workspace verified before).
    Anything else, including legacy 'local' or missing metadata and an
    unreadable provider, raises TenantMismatch with nothing written.
    Without it (module-level callers) the pre-L24 single-workspace rule
    applies: the row namespace is the identity and refusals are journaled.

    `fetched` is a provider read already made for this delivery (the
    single webhook route reads Stripe off the event loop, then calls this
    on the loop); it is verified exactly like a read made here.
    """
    import sqlite3
    strict = tenant is not None
    moment = int(now if now is not None else time.time())
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id or not event_type:
        raise ValueError("webhook event needs an id and type")
    customer_id = _event_customer(event)
    subscription_id = _event_subscription(event)
    trigger = event_type in _TRIGGER_TYPES and subscription_id and customer_id
    lock = _subscription_lock(subscription_id) if trigger else threading.Lock()
    with lock:
        if strict:
            if not tenant or any(c != tenant for c in _event_identities(event)):
                raise TenantMismatch("delivery names another billing identity")
            if event_type == "checkout.session.expired" \
                    and tenant not in _event_identities(event):
                raise TenantMismatch("expired checkout names no billing identity")
            if trigger:
                fetched = _verified_fetch(con, workspace_id, tenant,
                                          subscription_id, config, provider,
                                          fetched=fetched)
        ensure_billing_schema(con)
        try:
            con.execute(
                "INSERT INTO billing_events (event_id, workspace_id, customer_id,"
                " subscription_id, event_type, status, price_id, period_end,"
                " reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'received', '', 0, '', ?)",
                (event_id, tenant if strict else workspace_id, customer_id,
                 subscription_id, event_type, _utcnow_iso()))
        except sqlite3.IntegrityError:
            stored = con.execute("SELECT status, reason FROM billing_events"
                                 " WHERE event_id=?", (event_id,)).fetchone()
            return {"event_id": event_id, "duplicate": True,
                    "status": stored["status"] if stored else "received",
                    "reason": stored["reason"] if stored else ""}
        if event_type == "checkout.session.expired":
            return _expired_checkout(con, workspace_id, event_id, event,
                                     config or BillingConfig(), moment,
                                     tenant=tenant)
        if event_type not in _TRIGGER_TYPES:
            return _journal(con, event_id, "ignored",
                            "event type carries no entitlement trigger")
        if not trigger:
            return _journal(con, event_id, "unverifiable",
                            "event names no provider subscription/customer")
        return _reconcile(con, workspace_id, event_id, customer_id,
                          subscription_id, moment, config, provider,
                          tenant=tenant, fetched=fetched)


def _fetch(subscription_id: str, config: BillingConfig | None,
           provider: SubscriptionProtocol | None) -> tuple:
    """One current-resource read: ("ok", resource), ("gone", ""),
    ("failed", reason) or ("unavailable", "")."""
    if provider is None:
        if config is None or not config.secret_key:
            return "unavailable", ""
        provider = StripeSubscriptionProtocol(config)
    try:
        return "ok", provider.get_subscription(subscription_id)
    except ProviderNotFound:
        return "gone", ""
    except ProviderError as exc:
        return "failed", str(exc)


def _verified_fetch(con, workspace_id: str, tenant: str, subscription_id: str,
                    config: BillingConfig | None,
                    provider: SubscriptionProtocol | None, *,
                    fetched: tuple | None = None) -> tuple:
    """Read the subscription (or take the read already made) and prove it
    belongs to `tenant` before any write, or raise TenantMismatch."""
    kind, resource = fetched or _fetch(subscription_id, config, provider)
    if kind == "ok" and isinstance(resource, dict) \
            and resource.get("id") == subscription_id \
            and resource.get("metadata_workspace") == tenant:
        return kind, resource
    if kind == "gone":
        try:
            known = con.execute("SELECT workspace_id FROM billing_subscriptions"
                                " WHERE subscription_id=?",
                                (subscription_id,)).fetchone()
        except sqlite3.OperationalError:
            known = None
        if known is not None and known[0] == workspace_id:
            try:
                verified = tenant_customer(con, workspace_id, tenant)
            except TenantMismatch:
                verified = ""
            if verified:
                return kind, resource
    raise TenantMismatch("subscription is not verified for this billing identity")


def _journal(con, event_id: str, status: str, reason: str = "") -> dict:
    con.execute("UPDATE billing_events SET status=?, reason=? WHERE event_id=?",
                (status, reason, event_id))
    return {"event_id": event_id, "duplicate": False, "status": status,
            "reason": reason}


def _reconcile(con, workspace_id: str, event_id: str, customer_id: str,
               subscription_id: str, moment: int,
               config: BillingConfig | None,
               provider: SubscriptionProtocol | None, *,
               tenant: str | None = None, fetched: tuple | None = None) -> dict:
    bound = bound_customer(con, workspace_id)
    if bound and customer_id != bound:
        return _journal(con, event_id, "customer_mismatch",
                        f"event customer {customer_id} is not the workspace-bound"
                        f" customer {bound}; webhook authenticity is not authority")
    kind, resource = fetched or _fetch(subscription_id, config, provider)
    if kind == "unavailable":
        return _journal(con, event_id, "unverifiable",
                        "no provider protocol available")
    if kind == "gone":
        return _reconcile_gone(con, workspace_id, event_id, customer_id,
                               subscription_id, moment)
    if kind == "failed":
        return _journal(con, event_id, "reconcile_failed",
                        f"provider read failed; last verified state retained: {resource}")
    if not isinstance(resource, dict) or resource.get("id") != subscription_id:
        return _journal(con, event_id, "reconcile_failed",
                        "provider returned an ambiguous resource;"
                        " last verified state retained")
    if resource.get("customer") != customer_id:
        return _journal(con, event_id, "customer_mismatch",
                        "event names a customer the provider resource does"
                        " not belong to")
    if bound and resource.get("customer") != bound:
        return _journal(con, event_id, "customer_mismatch",
                        "provider resource belongs to a different customer"
                        " than the workspace binding")
    if bound and tenant and resource.get("metadata_workspace") == tenant:
        # The provider resource names this identity and belongs to the
        # bound customer: a webhook-made binding (legacy or stamped for
        # another identity) is now verified for this one. Operator
        # bindings are left as they are.
        row = con.execute("SELECT bound_by FROM billing_bindings"
                          " WHERE workspace_id=?", (workspace_id,)).fetchone()
        binder = (row[0] if row else "") or ""
        if binder.startswith(WEBHOOK_BINDER) and binder != webhook_binder(tenant):
            con.execute("UPDATE billing_bindings SET bound_by=?, bound_at=?"
                        " WHERE workspace_id=?",
                        (webhook_binder(tenant), _utcnow_iso(), workspace_id))
    if not bound:
        # Verified first purchase only: the provider resource itself
        # must attribute this workspace (checkout metadata), and the
        # event customer must be the resource customer.
        identity = tenant if tenant is not None else workspace_id
        if resource.get("metadata_workspace") != identity:
            return _journal(con, event_id, "unbound",
                            "no workspace customer binding; event cannot claim one")
        bind_billing_customer(con, workspace_id, customer_id,
                              operator=webhook_binder(tenant) if tenant
                              else WEBHOOK_BINDER)
        bound = customer_id
    price_id = resource.get("price_id") or ""
    if config is not None and price_id not in (config.prices or {}):
        return _journal(con, event_id, "unpriced",
                        f"verified price {price_id or '<none>'} is not in the"
                        " configured catalog")
    return _write_verified(con, workspace_id, event_id, customer_id,
                           subscription_id, resource, price_id, moment)


def _reconcile_gone(con, workspace_id: str, event_id: str, customer_id: str,
                    subscription_id: str, moment: int) -> dict:
    """Provider reports no such subscription: only a previously verified
    subscription for this workspace may transition to canceled, keeping
    its last verified period end. Unknown ids are foreign, never
    authority, and change nothing."""
    known = con.execute("SELECT workspace_id FROM billing_subscriptions"
                        " WHERE subscription_id=?", (subscription_id,)).fetchone()
    if known is None or known["workspace_id"] != workspace_id:
        return _journal(con, event_id, "foreign_resource",
                        "provider knows no such subscription and this workspace"
                        " never verified it; last verified state retained")
    row = con.execute("SELECT * FROM billing_entitlement WHERE workspace_id=?",
                      (workspace_id,)).fetchone()
    period = int((dict(row).get("current_period_end") if row else 0) or 0)
    _store_entitlement(con, workspace_id, customer_id, "canceled",
                       (dict(row).get("price_id") if row else "") or "",
                       int((dict(row).get("trial_end") if row else 0) or 0),
                       period,
                       int((dict(row).get("past_due_since") if row else 0) or 0),
                       0)
    _record_trial_history(con, workspace_id, customer_id, "canceled", {},
                          moment)
    return _journal(con, event_id, "reconciled",
                    "verified subscription is gone; canceled at last verified"
                    " period end, never extended")


def _write_verified(con, workspace_id: str, event_id: str, customer_id: str,
                    subscription_id: str, resource: dict, price_id: str,
                    moment: int) -> dict:
    row = con.execute("SELECT * FROM billing_entitlement WHERE workspace_id=?",
                      (workspace_id,)).fetchone()
    current = dict(row) if row else {}
    period = int(current.get("current_period_end") or 0)
    past_due_since = int(current.get("past_due_since") or 0)
    verified_period = max(0, int(resource.get("current_period_end") or 0))
    verified_trial = max(0, int(resource.get("trial_end") or 0))
    sub_status = resource.get("status") or ""
    if sub_status not in STRIPE_STATUSES:
        return _journal(con, event_id, "reconcile_failed",
                        f"provider reports unrecognized status {sub_status!r};"
                        " last verified state retained")
    if period_unreadable(resource):
        return _journal(con, event_id, "reconcile_failed",
                        f"provider reports {sub_status} with no valid period"
                        " end; last verified state retained")
    # Stored as Stripe reports it (design 8.6); the gate decides. Verified
    # expiry only: a resource without a period keeps the last verified one,
    # never an event-carried value.
    status = sub_status
    period = verified_period or period
    trial_end = verified_trial or int(current.get("trial_end") or 0)
    past_due_since = (past_due_since or moment) if status == "past_due" else 0
    cancel_at = max(0, int(resource.get("cancel_at") or 0))
    if not cancel_at and resource.get("cancel_at_period_end") \
            and status in ("active", "past_due"):
        cancel_at = period
    _store_entitlement(con, workspace_id, customer_id, status, price_id,
                       trial_end, period, past_due_since, cancel_at)
    _record_trial_history(con, workspace_id, customer_id, status, resource,
                          moment)
    con.execute(
        "INSERT INTO billing_subscriptions (subscription_id, workspace_id,"
        " customer_id, verified_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(subscription_id) DO UPDATE SET workspace_id=excluded.workspace_id,"
        " customer_id=excluded.customer_id, verified_at=excluded.verified_at",
        (subscription_id, workspace_id, customer_id, _utcnow_iso()))
    return _journal(con, event_id, "reconciled",
                    f"verified provider status {sub_status}")


def _store_entitlement(con, workspace_id: str, customer_id: str, status: str,
                       price_id: str, trial_end: int, period: int,
                       past_due_since: int, cancel_at: int) -> None:
    """`cancel_at` (Stripe's end date, epoch seconds) is kept in the
    `cancel_at_period_end` column, so billing_entitlement keeps exactly
    the columns every older backup has. Rows written before BY1 hold 0 or
    1 there (a flag, not a time). Read it only through stored_cancel_at."""
    con.execute(
        "INSERT INTO billing_entitlement (workspace_id, customer_id, status,"
        " price_id, trial_end, current_period_end, past_due_since,"
        " cancel_at_period_end, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(workspace_id) DO UPDATE SET customer_id=excluded.customer_id,"
        " status=excluded.status, price_id=excluded.price_id,"
        " trial_end=excluded.trial_end,"
        " current_period_end=excluded.current_period_end,"
        " past_due_since=excluded.past_due_since,"
        " cancel_at_period_end=excluded.cancel_at_period_end,"
        " updated_at=excluded.updated_at",
        (workspace_id, customer_id, status, price_id, trial_end, period,
         past_due_since, cancel_at, _utcnow_iso()))


def parse_webhook_event(config: BillingConfig, payload: bytes) -> dict:
    """The verified payload as an event: a JSON object with an id and a
    type, and live mode when live mode is required. Needs no workspace, so
    the route runs it before resolving one."""
    try:
        event = json.loads((payload or b"").decode())
    except ValueError as exc:
        raise ValueError("webhook payload is not JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("webhook payload is not an object")
    if not str(event.get("id") or "") or not str(event.get("type") or ""):
        raise ValueError("webhook event needs an id and type")
    if config.livemode_required and not event.get("livemode"):
        raise ValueError("refusing test-mode webhook as live success")
    return event


def handle_webhook(config: BillingConfig, con, workspace_id: str,
                   payload: bytes, signature: str, *,
                   now: float | None = None,
                   provider: SubscriptionProtocol | None = None,
                   tenant: str | None = None) -> dict:
    """Verify the signature, then reconcile (never trust) the event.
    `tenant`: the workspace's billing identity (see apply_event)."""
    verify_webhook_signature(config, payload, signature, now=now)
    event = parse_webhook_event(config, payload)
    return apply_event(con, workspace_id, event, now=now, config=config,
                       provider=provider, tenant=tenant)


# -- One webhook endpoint (design 8.1-8.4) ---------------------------------
#
# POST /api/v1/billing/webhook (asgi.py) runs these in order: bounded body,
# signature, parse; the completed-delivery journal; the per-subscription
# lock; the journal again; the seat; the workspace journal (crash
# recovery); the bounded Stripe read; the workspace commit; the completion.

_DELIVERIES_DDL = (
    "CREATE TABLE IF NOT EXISTS billing_deliveries ("
    " event_id TEXT PRIMARY KEY,"
    " status_code INTEGER NOT NULL,"
    " body BLOB NOT NULL,"
    " completed_at TEXT NOT NULL)")


def stored_delivery(control, event_id: str) -> tuple | None:
    """The stored answer (status, body) of a completed delivery, or None.
    The table lives in the control store and is created by the first
    completion, so a store that never had billing on is unchanged."""
    try:
        row = control.execute("SELECT status_code, body FROM billing_deliveries"
                              " WHERE event_id=?", (event_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    return (int(row[0]), bytes(row[1])) if row else None


def complete_delivery(control, event_id: str, status_code: int,
                      body: bytes) -> None:
    """Record a delivery's answer. Written after the workspace commit, so
    a crash between the two leaves the workspace journal to finish it."""
    control.execute(_DELIVERIES_DDL)
    control.execute("INSERT OR IGNORE INTO billing_deliveries (event_id,"
                    " status_code, body, completed_at) VALUES (?, ?, ?, ?)",
                    (event_id, int(status_code), bytes(body), _utcnow_iso()))
    control.commit()


def journaled_outcome(con, event_id: str) -> tuple | None:
    """(status, reason) of an event the workspace already committed, or
    None. Crash recovery: the completion is written without a new read."""
    try:
        row = con.execute("SELECT status, reason FROM billing_events"
                          " WHERE event_id=?", (event_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return (str(row[0]), str(row[1] or "")) if row else None


def webhook_subscription(event: dict) -> str:
    """The subscription a single-route delivery is about ('' when the
    event is not one of WEBHOOK_EVENT_TYPES or names none)."""
    if str(event.get("type") or "") not in WEBHOOK_EVENT_TYPES:
        return ""
    return _event_subscription(event)


def fetch_current(subscription_id: str, provider: SubscriptionProtocol) -> tuple:
    """The Stripe read of one delivery: ("ok", resource), ("gone", "") or
    ("failed", reason). Runs on a worker thread."""
    return _fetch(subscription_id, None, provider)


class SeatUnavailable(Exception):
    """A transient fault reading a seat (the registry, the workspace file
    or its price). The delivery answers 503 and completes nothing, so
    Stripe retries it; only a definite absence is "no seat"."""


def open_seat_db(store, workspace_id: str, data_dir: str, *,
                 readonly: bool = False):
    """(con, entry) for a seat's workspace, None when it definitely has
    none, or SeatUnavailable on a transient fault. The same checks as
    connections.open_registered_db (registered, contained path, existing
    file, matching database identity), which answers every failure as one
    404 and so cannot tell a missing workspace from a failed read."""
    import os
    from . import connections as _conn
    from . import db as _db
    try:
        entry = _conn.resolve_workspace(store, workspace_id)
    except _conn.GrantError as exc:
        if exc.status == 404:
            return None
        raise SeatUnavailable(str(exc)) from exc
    except sqlite3.Error as exc:
        raise SeatUnavailable(f"registry read failed: {exc}") from exc
    real_db = _conn._contained_db_path(data_dir, entry["db_path"])
    if real_db is None or not os.path.isfile(real_db):
        return None
    try:
        con = _db.open_existing(real_db, readonly=readonly)
    except FileNotFoundError as exc:
        if not os.path.isfile(real_db):
            return None
        raise SeatUnavailable(f"workspace open failed: {exc}") from exc
    except Exception as exc:
        raise SeatUnavailable(f"workspace open failed: {exc}") from exc
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'"
                          ).fetchone()
    except sqlite3.Error as exc:
        con.close()
        raise SeatUnavailable(f"workspace identity read failed: {exc}") from exc
    if row is None or row["value"] != entry["db_identity"]:
        con.close()
        return None
    return con, entry


def event_conflict(event: dict) -> bool:
    """True when the signed event names more than one billing identity
    (BY1's strict metadata rule; the delivery is the fixed 404)."""
    return len(set(_event_identities(event))) > 1


def offer_prices(config: BillingConfig, offer: str) -> frozenset:
    """The configured prices of one offer: EARWITNESS_BILLING_PRICES maps
    each price to its metadata, whose `offer` names `ew-a` or `ew-b`."""
    return frozenset(pid for pid, meta in (config.prices or {}).items()
                     if isinstance(meta, dict) and meta.get("offer") == offer)


class PurchaseSeats:
    """The seat lookup of design 8.2 step 2, on `purchase_seats`
    (purchases.py) in the control store:
    seat_for_subscription(subscription_id) -> (workspace_id, offer) | None.

    Only a seat in purchases.WEBHOOK_STATES (`bound`, `active`,
    `awaiting_account`) that holds a workspace takes the workspace path.
    No seat, a `refused` or `needs_refund` row (BY3 r9: the window decides
    it), a NULL workspace or any other state is None (200, no change; the
    window finds it). A failed
    read of the control store raises SeatUnavailable (503, retried).

    Replaces BY1's BindingSeats: a workspace that has a verified billing
    binding but no seat (the comped judges' demo and owner workspaces) no
    longer takes the workspace path; its deliveries complete as no_seat."""

    def __init__(self, store):
        self._store = store

    def seat_for_subscription(self, subscription_id: str) -> tuple | None:
        from . import purchases as _purchases
        if not subscription_id:
            return None
        try:
            control = self._store.auth_connection()
        except sqlite3.Error as exc:
            raise SeatUnavailable(f"control store open failed: {exc}") from exc
        try:
            row = _purchases.seat(control, subscription_id)
        except sqlite3.Error as exc:
            raise SeatUnavailable(f"seat read failed: {exc}") from exc
        finally:
            control.close()
        if row is None or row["state"] not in _purchases.WEBHOOK_STATES \
                or not row["workspace_id"]:
            return None
        return row["workspace_id"], row["offer"]


class SubscriptionLocks:
    """One asyncio.Lock per subscription, awaited on the event loop, with
    a holder count. An entry leaves the table when its last holder does;
    a new subscription past max_entries raises ProviderBusy (503)."""

    def __init__(self, max_entries: int = SUBSCRIPTION_LOCKS_MAX):
        self.max_entries = max_entries
        self._entries: dict = {}

    def __len__(self) -> int:
        return len(self._entries)

    @contextlib.asynccontextmanager
    async def hold(self, subscription_id: str):
        entry = self._entries.get(subscription_id)
        if entry is None:
            if len(self._entries) >= self.max_entries:
                raise ProviderBusy("too many subscriptions in flight")
            entry = self._entries[subscription_id] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._entries.pop(subscription_id, None)


def entitlement_status(con, workspace_id: str, config: BillingConfig | None = None,
                       *, now: float | None = None,
                       tenant: str | None = None) -> dict:
    """Server-side entitlement (design 8.6): `active` passes until the
    earlier of its period end and its end date (`cancel_at`); `past_due`
    passes inside the grace window and never past the end date. Everything
    else fails closed: `canceled` and `unpaid` at once whatever the period
    end, `incomplete`, `incomplete_expired`, `trialing`, `paused`, unknown
    and unrecognized states, and an `active` or `past_due` row with no
    verified period end.
    With `tenant` (the workspace's billing identity), a billing record
    whose customer was never verified for it fails closed too."""
    _require_billing_schema(con)
    if tenant is not None:
        try:
            tenant_customer(con, workspace_id, tenant)
        except TenantMismatch:
            return {"state": "unknown", "allowed": False,
                    "reason": "billing record is not verified for this workspace"}
    moment = int(now if now is not None else time.time())
    row = con.execute("SELECT * FROM billing_entitlement WHERE workspace_id=?",
                      (workspace_id,)).fetchone()
    if row is None:
        if _trial_row(con, workspace_id).get("trial_state") == "reserved":
            return {"state": "unknown", "allowed": False,
                    "reason": "trial reserved, no verified subscription"}
        return {"state": "unknown", "allowed": False,
                "reason": "no billing record"}
    ent = dict(row)
    status = ent.get("status") or "unknown"
    period_end = int(ent.get("current_period_end") or 0)
    cancel_at = stored_cancel_at(ent)
    price = ent.get("price_id") or ""
    if status == "unknown":
        # A row can exist for trial or customer history alone; it never
        # carries a verified subscription.
        reserved = _trial_row(con, workspace_id).get("trial_state") == "reserved"
        return {"state": "unknown", "allowed": False,
                "reason": "trial reserved, no verified subscription"
                if reserved else "no billing record"}
    if status in ("active", "past_due") and period_end <= 0:
        # Never open-ended: a row with no verified paid-period end (an
        # older write, or one made before this check) is refused.
        return {"state": "blocked", "allowed": False,
                "reason": "no verified period end", "price_id": price}
    if cancel_at and cancel_at <= moment and status in ("active", "past_due"):
        return {"state": "blocked", "allowed": False,
                "reason": "subscription reached its end date",
                "price_id": price}
    if status == "active":
        if period_end and period_end <= moment:
            return {"state": "blocked", "allowed": False,
                    "reason": "subscription period ended", "price_id": price}
        return {"state": "active", "allowed": True, "price_id": price,
                "current_period_end": period_end, "cancel_at": cancel_at,
                "cancel_at_period_end": bool(cancel_at)
                and cancel_at == period_end}
    if status == "past_due":
        grace = config.past_due_grace_s if config else 72 * 3600
        since = int(ent.get("past_due_since") or moment)
        if moment - since <= grace:
            return {"state": "past_due", "allowed": True,
                    "grace_remaining_s": grace - (moment - since),
                    "cancel_at": cancel_at, "price_id": price}
        return {"state": "blocked", "allowed": False,
                "reason": "past-due grace window exhausted", "price_id": price}
    if status in ("canceled", "unpaid"):
        # Refused at once, whatever the period end (design 8.6).
        return {"state": "blocked", "allowed": False,
                "reason": f"subscription {status}", "price_id": price}
    if status in STRIPE_STATUSES:
        return {"state": "blocked", "allowed": False,
                "reason": f"subscription is {status}", "price_id": price}
    return {"state": "unknown", "allowed": False,
            "reason": f"unrecognized billing status: {status}"}


def stored_cancel_at(ent: dict) -> int:
    """The one reader of billing_entitlement.cancel_at_period_end.

    Since BY1 that column holds Stripe's `cancel_at` (epoch seconds), not
    a flag: the column keeps its old name so older backups still restore.
    0 or 1 (the pre-BY1 flag) means no end date. Never read the column as
    a boolean; a test keeps every other read out."""
    try:
        value = int(ent.get("cancel_at_period_end") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 1 else 0


class EntitlementRequired(AuthorizationError):
    """A gated operation was refused because the workspace has no active
    entitlement. HTTP 402; the message names the operation and the reason."""

    def __init__(self, operation: str, reason: str, state: str = "unknown"):
        super().__init__(f"entitlement required for {operation}: {reason}", 402)
        self.operation = operation
        self.reason = reason
        self.state = state


def require_entitlement(con, workspace_id: str, operation: str,
                        config: BillingConfig, *, now: float | None = None,
                        tenant: str | None = None):
    """Refuse an operation unless server-side payment state allows it.

    Does nothing (and reads nothing) unless entitlement checks are on.
    When on, every operation is gated except the configured exempt list.
    A missing billing schema or an unreadable record fails closed."""
    if not config.entitlement_required \
            or operation in (config.ungated_operations or ()):
        return None
    try:
        ent = entitlement_status(con, workspace_id, config, now=now,
                                 tenant=tenant)
    except (ValueError, sqlite3.Error):
        ent = {"state": "unknown", "allowed": False,
               "reason": "billing state unavailable"}
    if not ent.get("allowed"):
        raise EntitlementRequired(
            operation, str(ent.get("reason") or "no active subscription"),
            str(ent.get("state") or "unknown"))
    return ent


# -- One trial per customer history (AA-02) -------------------------------
#
# A history is the workspace plus every provider customer it has ever
# presented or been bound to (a replacement customer never resets it).
# The reservation lives on the workspace's billing_trials row, so the
# workspace primary key plus one write transaction make it atomic across
# concurrent checkouts, and a verified subscription (a delayed webhook, in
# any order, any number of times) turns it into a consumed trial.
#
# UNAPPROVED policy lives only in BillingConfig: release after abandoned
# or failed checkout, and how cancelled, expired and paid-only history
# count. Defaults are the strictest reading.


def _entitlement_row(con, workspace_id: str) -> dict:
    row = con.execute("SELECT " + ", ".join(_ENTITLEMENT_COLUMNS)
                      + " FROM billing_entitlement WHERE workspace_id=?",
                      (workspace_id,)).fetchone()
    return dict(row) if row else {}


def _trial_row(con, workspace_id: str) -> dict:
    """The workspace's trial history, {} when it has none. A restored
    older backup may not have the table yet: reads treat that as no
    history; every writer runs ensure_billing_schema first."""
    rows = _trial_rows(con, "SELECT * FROM billing_trials WHERE workspace_id=?",
                       (workspace_id,))
    return dict(rows[0]) if rows else {}


def _trial_rows(con, sql: str, args: tuple) -> list:
    """Read billing_trials; a database whose table does not exist yet
    (an older restored backup) has no rows."""
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []


def _history_ids(row: dict) -> list:
    try:
        loaded = json.loads(row.get("customer_history") or "[]")
    except ValueError:
        return []
    return [str(c) for c in loaded] if isinstance(loaded, list) else []


def _remember_customers(con, workspace_id: str, customer_ids, *,
                        create: bool = True) -> None:
    row = _trial_row(con, workspace_id)
    if not row and not create and not _entitlement_row(con, workspace_id):
        return
    have = _history_ids(row)
    merged = list(have)
    for cid in customer_ids:
        if cid and cid not in merged:
            merged.append(cid)
    if merged == have and row:
        return
    con.execute(
        "INSERT INTO billing_trials (workspace_id, customer_history)"
        " VALUES (?, ?)"
        " ON CONFLICT(workspace_id) DO UPDATE SET"
        " customer_history=excluded.customer_history",
        (workspace_id, json.dumps(merged, separators=(",", ":"))))


def _has_subscription(con, workspace_id: str) -> bool:
    return con.execute("SELECT 1 FROM billing_subscriptions WHERE workspace_id=?",
                       (workspace_id,)).fetchone() is not None


def _workspace_trial_counts(con, workspace_id: str, config: BillingConfig,
                            now: int) -> tuple:
    """(counts, reason): does this workspace's own history already hold
    the one trial?"""
    ent = _entitlement_row(con, workspace_id)
    row = {**ent, **_trial_row(con, workspace_id)}
    state = row.get("trial_state") or ""
    if state == "released":
        return False, ""
    if state == "reserved":
        age = now - int(row.get("trial_reserved_at") or 0)
        if config.trial_release_abandoned and age > config.trial_reservation_ttl_s \
                and not _has_subscription(con, workspace_id):
            return False, ""
        return True, "an earlier checkout already reserved the trial"
    if state == "consumed":
        if row.get("trial_kind") == "paid":
            return (config.prior_subscription_blocks_trial,
                    "an earlier subscription exists")
        outcome = row.get("trial_outcome") or ""
        if outcome == "cancelled":
            return config.trial_cancelled_counts, "an earlier trial was cancelled"
        if outcome == "expired":
            return config.trial_expired_counts, "an earlier trial expired"
        return True, "an earlier trial exists"
    # History that predates trial reservations.
    if int(row.get("trial_end") or 0) > 0 or row.get("status") == "trialing":
        return True, "an earlier trial exists"
    if _has_subscription(con, workspace_id) or row.get("status") in (
            "active", "past_due", "canceled"):
        return (config.prior_subscription_blocks_trial,
                "an earlier subscription exists")
    return False, ""


def _trial_verdict(con, workspace_id: str, customer_ids, config: BillingConfig,
                   now: int) -> tuple:
    own = _trial_row(con, workspace_id)
    ids = {c for c in customer_ids if c} | set(_history_ids(own))
    related = [workspace_id]
    for cid in sorted(ids):
        quoted = json.dumps(cid)
        hits = [
            *con.execute("SELECT workspace_id FROM billing_entitlement"
                         " WHERE customer_id=?", (cid,)),
            *_trial_rows(con, "SELECT workspace_id FROM billing_trials"
                              " WHERE instr(customer_history, ?) > 0", (quoted,)),
            *con.execute("SELECT workspace_id FROM billing_bindings"
                         " WHERE customer_id=?", (cid,)),
            *con.execute("SELECT workspace_id FROM billing_subscriptions"
                         " WHERE customer_id=?", (cid,))]
        for hit in hits:
            other = hit[0]
            if other and other not in related:
                related.append(other)
    for other in related:
        counts, reason = _workspace_trial_counts(con, other, config, now)
        if counts:
            suffix = "" if other == workspace_id else \
                " (same customer history, another workspace)"
            return True, reason + suffix
    return False, ""


def trial_eligibility(con, workspace_id: str, customer_id: str = "",
                      config: BillingConfig | None = None, *,
                      now: float | None = None) -> dict:
    """Read-only: would a trial be reserved for this history right now?"""
    _require_billing_schema(con)
    cfg = config or BillingConfig()
    moment = int(now if now is not None else time.time())
    ids = [customer_id, bound_customer(con, workspace_id)]
    counts, reason = _trial_verdict(con, workspace_id, ids, cfg, moment)
    return {"eligible": not counts,
            "reason": f"trial is not available: {reason}" if counts else ""}


def reserve_trial(con, workspace_id: str, customer_id: str = "",
                  config: BillingConfig | None = None, *,
                  now: float | None = None) -> dict:
    """Atomically reserve the one trial for this customer history.

    Runs in one write transaction (a savepoint inside the caller's), so
    of any number of concurrent checkouts exactly one is granted. A
    refused reservation changes nothing. Granting reserves only; it never
    grants entitlement."""
    from . import db as _db
    cfg = config or BillingConfig()
    moment = int(now if now is not None else time.time())
    ensure_billing_schema(con)
    with _db.transaction(con, write=True):
        ids = [customer_id, bound_customer(con, workspace_id)]
        counts, reason = _trial_verdict(con, workspace_id, ids, cfg, moment)
        if counts:
            return {"granted": False, "reservation_id": "",
                    "reason": f"trial is not available: {reason}"}
        reservation_id = uuid.uuid4().hex
        history = _history_ids(_trial_row(con, workspace_id))
        for cid in ids:
            if cid and cid not in history:
                history.append(cid)
        con.execute(
            "INSERT INTO billing_trials (workspace_id,"
            " trial_state, trial_kind, trial_outcome, trial_reservation_id,"
            " trial_reserved_at, customer_history)"
            " VALUES (?, 'reserved', 'trial', '', ?, ?, ?)"
            " ON CONFLICT(workspace_id) DO UPDATE SET"
            " trial_state='reserved', trial_kind='trial', trial_outcome='',"
            " trial_reservation_id=excluded.trial_reservation_id,"
            " trial_reserved_at=excluded.trial_reserved_at,"
            " customer_history=excluded.customer_history",
            (workspace_id, reservation_id, moment,
             json.dumps(history, separators=(",", ":"))))
        return {"granted": True, "reservation_id": reservation_id, "reason": ""}


def release_trial_reservation(con, workspace_id: str, reservation_id: str,
                              config: BillingConfig | None = None) -> dict:
    """Release a reserved (never consumed) trial after an abandoned
    checkout. UNAPPROVED policy: does nothing unless the release switch is
    on. Only the matching, still-reserved reservation of a history with no
    verified subscription is released."""
    from . import db as _db
    cfg = config or BillingConfig()
    if not cfg.trial_release_abandoned:
        return {"released": False,
                "reason": "release after an abandoned checkout is disabled"
                          " (UNAPPROVED policy)"}
    with _db.transaction(con, write=True):
        row = _trial_row(con, workspace_id)
        if not reservation_id or row.get("trial_state") != "reserved" \
                or row.get("trial_reservation_id") != reservation_id:
            return {"released": False,
                    "reason": "no matching reserved trial to release"}
        if _has_subscription(con, workspace_id):
            return {"released": False,
                    "reason": "a verified subscription exists; trial stays used"}
        con.execute("UPDATE billing_trials SET trial_state='released'"
                    " WHERE workspace_id=?", (workspace_id,))
        return {"released": True, "reason": "abandoned checkout released the trial"}


def _expired_checkout(con, workspace_id: str, event_id: str, event: dict,
                      config: BillingConfig, moment: int, *,
                      tenant: str | None = None) -> dict:
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    claimed = str(meta.get("workspace_id") or obj.get("client_reference_id") or "")
    if claimed != (tenant if tenant is not None else workspace_id):
        return _journal(con, event_id, "ignored",
                        "expired checkout names another workspace")
    out = release_trial_reservation(con, workspace_id,
                                    str(meta.get("trial_reservation") or ""),
                                    config)
    return _journal(con, event_id, "reconciled" if out["released"] else "ignored",
                    out["reason"])


def _record_trial_history(con, workspace_id: str, customer_id: str,
                          status: str, resource: dict, moment: int) -> None:
    """Fold one verified provider state into the trial history. Order
    independent: it reads only the verified resource and the stored row."""
    row = {**_entitlement_row(con, workspace_id), **_trial_row(con, workspace_id)}
    prior_trial = row.get("trial_state") == "consumed" \
        and row.get("trial_kind") == "trial"
    prior = (row.get("trial_outcome") or "") if prior_trial else ""
    trial_end = max(int(resource.get("trial_end") or 0),
                    int(row.get("trial_end") or 0))
    period_end = int(resource.get("current_period_end") or 0) \
        or int(row.get("current_period_end") or 0)
    was_trial = prior_trial or trial_end > 0 or status == "trialing"
    converted = prior == "converted" or (
        was_trial and (status == "active" or (trial_end and period_end > trial_end)))
    if not was_trial:
        outcome = ""
    elif converted:
        outcome = "converted"
    elif status == "trialing":
        outcome = "trialing"
    elif status == "canceled":
        # Cancelled before the trial ended, or it ran out. Dated by the
        # provider's own times; webhook processing time is only the last
        # resort, and a later gone-event never overrides a dated outcome.
        when = int(resource.get("canceled_at") or 0) or int(resource.get("ended_at") or 0)
        if not when and prior in ("cancelled", "expired"):
            outcome = prior
        else:
            when = when or moment
            outcome = "cancelled" if (not trial_end or when < trial_end) else "expired"
    else:
        outcome = prior
    history = _history_ids(row)
    if customer_id and customer_id not in history:
        history.append(customer_id)
    con.execute(
        "INSERT INTO billing_trials (workspace_id, trial_state, trial_kind,"
        " trial_outcome, customer_history) VALUES (?, 'consumed', ?, ?, ?)"
        " ON CONFLICT(workspace_id) DO UPDATE SET trial_state='consumed',"
        " trial_kind=excluded.trial_kind, trial_outcome=excluded.trial_outcome,"
        " customer_history=excluded.customer_history",
        (workspace_id, "trial" if was_trial else "paid", outcome,
         json.dumps(history, separators=(",", ":"))))
