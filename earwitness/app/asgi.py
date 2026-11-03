"""Production hosted HTTP surface: agent API, connections, OAuth metadata.

Package E (hosted code only; no live provider, no deploy). Pure-stdlib
ASGI: no framework import here (uvicorn serves it in production; tests
drive it with direct ASGI calls, no sockets). One authorization policy:
Bearer -> server-controlled grant -> AccessContext built from the grant;
request bodies contribute business arguments only. Handler-scoped DB
handles are opened and closed per request, never shared with SDK
workers. The MCP surface is mounted by delegating /mcp to D's
streamable app built per (workspace, scopes) grant.
"""
from __future__ import annotations

import asyncio
import collections
import contextvars
import functools
import ipaddress
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field

from earwitness.app import access as _access
from earwitness.app import agent_contracts as _contracts
from earwitness.app import connections as _conn
from earwitness.app import freshness as _freshness
from earwitness.app import health as _health
from earwitness.app import oplog as _oplog
from earwitness.app import public_pricing as _public_pricing
from earwitness.app import rights_admission as _rights

READS_PER_MINUTE = 60
BURST = 20
# A browser page fans out: Settings alone is five reads, six on a fresh
# load. Open + two save+reload cycles is 21 reads; 40 holds that and the
# pages before it.
SESSION_BURST = 40
# A browser session refills faster than an agent grant: a judge clicking
# through the pages with a 300 ms pause on each usable page reads 85-135
# a minute for minutes (faster when the host answers faster; at most 120
# needed in 10-round walks on the DET copy). At 60 a minute the session
# bucket ran dry and the client's retries hid the 429s (OWN-13,
# RC-SCR-R2-02; PFC-R1-06: 123 of 312 responses). Clicking on the instant
# each page is usable runs 130-160 a minute; a few of those reads wait for
# the client's retry. The workspace bucket still bounds all sessions
# together.
SESSION_READS_PER_MINUTE = 150
# All sessions of a workspace together (burst the same). Six judges and
# ops-check share one workspace: FIX1's normal-load walks laid over each
# other need 330-370 a minute for six sessions, 400-440 for seven and
# 430-490 for eight (NOGAP-PF-FIX2); at 300 the bucket ran 64-148 reads
# short. Eight sessions flooding at the session rate still reach it.
WORKSPACE_READS_PER_MINUTE = 600
# A clip's audio (GET /api/v1/customer/records/{ref}/media from a browser
# session) is read by the browser's own media loader in byte-Range chunks;
# the app cannot pace it or see a Retry-After. It spends a bucket of its
# own per session and per workspace, never the page-read buckets (AUDIO-429,
# measured counts in docs/hosting/request-limits.md). A refused media read
# costs nothing and refills nothing: WebKit's loader asks again at once
# after a refusal, and a cost would keep it refused (AUDIO-429-FIX1).
MEDIA_PER_MINUTE = 30
MEDIA_BURST = 30
WORKSPACE_MEDIA_PER_MINUTE = 300
# Show covers (GET /api/v1/customer/shows/{id}/artwork) are a lane of their
# own (ART-PERF-FIX1): a Shows page loads one per card, up to 24 at once,
# and over HTTP/2 a browser sends them all together. Limiter keys that
# start with ART_LANE spend the cover budget per session and per workspace,
# never the page-read one, and take cover in-flight slots and queue places
# (per session, per workspace and in the process), never a page read's. A
# cover request runs with _LIMIT_LANE set to ART_LANE, so every key the
# limiter keeps for it starts with ART_LANE.
# Covers read in a pool of their own (HostedApp.art_threads). One read is
# about 20 ms, so the session rate is at most 12 s of one thread a minute.
# docs/hosting/show-artwork.md.
ART_LANE = "art:"
ART_PER_MINUTE = 600
ART_BURST = 240
WORKSPACE_ART_PER_MINUTE = 2400
ART_MAX_IN_FLIGHT_PER_GRANT = 4
ART_GRANT_QUEUE_MAX = 28
ART_MAX_IN_FLIGHT_PER_WORKSPACE = 8
ART_QUEUE_MAX = 64
ART_WAITERS_MAX = 64
MAX_CONCURRENT_PER_GRANT = 2
MAX_CONCURRENT_PER_WORKSPACE = 10
# The workspace in-flight cap is a setting (default above). Sign-in sets it
# for a shared workspace with many editors; 1..64, anything else refuses
# to start (health.check_config). The per-session cap is not a setting.
WORKSPACE_MAX_IN_FLIGHT_ENV = "EARWITNESS_WORKSPACE_MAX_IN_FLIGHT"
# A full in-flight cap waits this long for a slot before 429 (FIFO per cap).
# At most LIMIT_QUEUE_MAX requests wait per workspace, LIMIT_GRANT_QUEUE_MAX
# per session/grant, and LIMIT_WAITERS_MAX in the whole process (a quarter
# of uvicorn's --limit-concurrency when that is set, at most 64), so waits
# never use up the server's connections; one more is refused at once.
# docs/hosting/request-limits.md.
LIMIT_WAIT_S = 8.0
LIMIT_QUEUE_MAX = 32
LIMIT_GRANT_QUEUE_MAX = 4
LIMIT_WAITERS_MAX = 64
MAX_BODY_BYTES = 2_000_000
BILLING_SLOTS = 2
# Request bodies are read whole before routing and authentication (L21).
# Only POST, PUT and DELETE to /api/ paths and /mcp are read; other paths
# never take a body. A read must finish within BODY_READ_TIMEOUT_S (the
# whole body, not per chunk) or it is answered 408. All reads in the
# process share one budget: BODY_MAX_READS at once, BODY_MAX_PER_ADDRESS
# from one client address (IPv6 by /64), and BODY_MAX_BUFFERED_BYTES held
# across them. Bytes are charged as they arrive, never reserved: a read
# that has sent nothing holds no bytes. A declared Content-Length larger
# than the space left is refused at once. Over budget is refused before
# anything more is kept: 503 for the shared budget, 429 for the address
# cap, both with Retry-After. A finished, timed-out or disconnected read
# gives its share back at once. Memory is bounded by the byte budget, so
# the read count can be generous; uvicorn --limit-concurrency bounds tasks.
BODY_READ_TIMEOUT_S = 15.0
BODY_MAX_READS = 128
BODY_MAX_PER_ADDRESS = 4
BODY_MAX_BUFFERED_BYTES = 32 * 1024 * 1024
BODY_RETRY_AFTER_S = 1
# The one hosted MCP URL: the path of EARWITNESS_MCP_RESOURCE_URI.
MCP_PATH = "/mcp"
# Longest a buffered MCP call may run before it is cut off with 504 and its
# concurrency slot is freed.
MCP_CALL_TIMEOUT_S = 30.0
RESEARCH_QUERY_PATH = "/api/v1/customer/research-query"
# Personal agent tokens (agent access design 4.2): browser sessions only.
AGENT_TOKENS_PATH = "/api/v1/customer/agent-tokens"
# Owner ad review (BRAND-CONFIRM-ROUTE): the workspace owner's review queue
# and actions, browser sessions only (HostedApp._owner_review). The page's
# two files are served to that owner only; anyone else gets the 404 page.
OWNER_REVIEW_PATH = "/api/v1/owner/ad-review"
OWNER_PAGE_PREFIX = "/app/owner/"
OWNER_PAGE_FILES = {"/app/owner/": ("owner/index.html", "text/html; charset=utf-8"),
                    "/app/owner/review.js": ("owner/review.js",
                                             "text/javascript; charset=utf-8")}
# The actions the owner route offers: this read only (no corpus-wide rule).
OWNER_REVIEW_ACTIONS = ("confirm", "type", "not_ad")
# OBR-R1-02: the operator's comma-separated allow-list of workspace ids that
# have the owner route. Empty or unset: the route is off (404 everywhere).
OWNER_REVIEW_WORKSPACES_ENV = "EARWITNESS_OWNER_REVIEW_WORKSPACES"
# OBR-R1-05: a typed brand or heard spelling: at most 80 characters, no C0
# or C1 control and no bidi embedding, override or isolate character.
OWNER_TEXT_MAX = 80
OWNER_TEXT_REFUSED = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
# OBR-R1-04: rules only the local operator page makes; undo leaves them alone.
OWNER_UNDO_REFUSED_RULES = ("alias", "stoplist", "self_promo")


def _private_page(send):
    """send, with Cache-Control: private, no-store and Vary: Cookie on the
    response start (any earlier cache-control or vary is replaced)."""
    async def wrapped(message):
        if message.get("type") == "http.response.start":
            headers = [(k, v) for k, v in message.get("headers", [])
                       if k.lower() not in (b"cache-control", b"vary")]
            headers += [(b"cache-control", b"private, no-store"), (b"vary", b"Cookie")]
            message = dict(message, headers=headers)
        await send(message)
    return wrapped


def _owner_review_workspaces() -> frozenset:
    """The allow-listed workspace ids, read per request."""
    import os
    return frozenset(part.strip() for part in
                     os.environ.get(OWNER_REVIEW_WORKSPACES_ENV, "").split(",")
                     if part.strip())
# Guarded writes that write nothing locally and wait on a provider
# (Stripe): the epoch is checked under the control lock, which is then
# released before the slow external call.
EXTERNAL_CALL_PATHS = frozenset({"/api/v1/billing/checkout", "/api/v1/billing/portal"})
# Outward sends that write locally around a provider call (L3 invite
# email): authority is checked under the control lock, then the handler
# runs with the workspace connection outside any transaction so its claim
# commits before the provider call and its outcome commits after.
OUTWARD_SEND_PATH = re.compile(r"/api/v1/workspaces/[^/]+/invites/[^/]+/send")
# Ask start and cancel write the workspace on connections of their own
# (ask.start_turn, ask.cancel_turn): their handlers run as an outward send's
# does, outside the request's workspace transaction.
ASK_OWN_WRITE_PATH = re.compile(r"/api/v1/customer/ask(?:/turns/[^/]+/cancel)?")
ASK_TURN_PATH = re.compile(r"/api/v1/customer/ask/(turns|threads)/([^/]+)(/cancel|/report)?")
ASK_REPORT_MAX = 500
# Meaning alerts (design 2026-10-26-meaning-alerts.md): terms, preview and
# Check now write the workspace on connections of their own
# (meaning.suggest_terms, start_preview, start_check), as Ask's start does.
MEANING_OWN_WRITE_PATH = re.compile(
    r"/api/v1/customer/meaning-alerts/(?:terms|preview|ma_[0-9a-f]{24}/check)")
MEANING_PATH = re.compile(
    r"/api/v1/customer/meaning-alerts(?:/(terms|preview)|/runs/([^/]+)"
    r"|/matches/([^/]+)/report|/([^/]+?)(?:/(check|lifecycle|read))?)?")
MEANING_BODY_KEYS = {"sentence", "terms", "shows", "brand"}
# The live brief check (LIVE-BRIEF-CHECK): the press writes the workspace on
# connections of its own (brief_check_live.start), as Check now does.
BRIEF_CHECK_OWN_WRITE_PATH = re.compile(r"/api/v1/customer/brief-check")
BRIEF_CHECK_RUN_PATH = re.compile(r"/api/v1/customer/brief-check/runs/([^/]+)")
BRIEF_CHECK_BODY_KEYS = {"campaign_id", "placement_id", "brief"}
ASK_REPORTS_PER_TURN = 5
TOOL_RESPONSE_CAP = 64 * 1024
CHUNK_BYTES = 1 << 20
CHUNK_TIMEOUT_S = 30.0
# Anonymous sign-in (login and callback) runs before any session exists, so
# it has its own limiter (SigninLimiter). Every attempt is charged to its
# client address (IPv6 by /64): SIGNIN_PER_MINUTE, burst SIGNIN_BURST, at
# most SIGNIN_MAX_PER_ADDRESS at once. Attempts that will call the provider
# also take one of SIGNIN_MAX_CONCURRENT provider slots. There is no shared
# per-minute budget: anyone can make a claim that reaches the token
# endpoint, so a shared budget would let many addresses lock everyone out.
# At most SIGNIN_MAX_TRACKED addresses are remembered. Refusals are 429
# before any provider request.
SIGNIN_PER_MINUTE = 20
SIGNIN_BURST = 10
SIGNIN_MAX_PER_ADDRESS = 2
SIGNIN_MAX_CONCURRENT = 4
SIGNIN_MAX_TRACKED = 10_000
# Built-in email and password sign-in (PW design rev 4, EARWITNESS_SIGNIN_LOCAL
# =on). A login body is at most PASSWORD_BODY_MAX bytes and a presented
# password at most local_accounts.PRESENTED_MAX_BYTES, checked before any
# hash. Each hash takes one of the SIGNIN_MAX_CONCURRENT slots. After the
# per-address limiter, the pair limiter (PairLimiter) allows PAIR_LIMIT
# attempts per (email key, address key) per PAIR_WINDOW_S, counting
# attempts in flight; it remembers at most PAIR_MAX_TRACKED pairs.
PASSWORD_BODY_MAX = 4096
PAIR_LIMIT = 5
PAIR_WINDOW_S = 900.0
PAIR_MAX_TRACKED = 10_000

# Explicit operation routes (§14: no RPC dispatcher). Each row names its
# tool; the handler factory below serves exactly these paths.
AGENT_ROUTES = (
    ("POST", "/api/v1/context", "get_workspace_context"),
    ("POST", "/api/v1/evidence/search", "search_evidence"),
    ("GET", "/api/v1/evidence/{evidence_id}", "get_evidence"),
    ("POST", "/api/v1/transcripts/search", "search_transcripts"),
    ("POST", "/api/v1/transcripts/window", "get_transcript_window"),
    ("GET", "/api/v1/brands", "list_brands"),
    ("GET", "/api/v1/shows", "list_shows"),
    ("GET", "/api/v1/episodes", "list_episodes"),
    ("POST", "/api/v1/evidence/summary", "summarize_evidence"),
    ("GET", "/api/v1/watchlists", "list_watchlists"),
    ("POST", "/api/v1/watchlists/evaluate", "get_watchlist_results"),
    ("GET", "/api/v1/campaigns", "list_campaigns"),
    ("GET", "/api/v1/campaigns/{campaign_id}", "get_campaign"),
    ("GET", "/api/v1/sources/{source_version_id}", "get_source_provenance"),
    ("POST", "/api/v1/compat/search", "search"),
    ("POST", "/api/v1/compat/fetch", "fetch"),
)

# Personal agent tokens (AGT-SEC-R5-01). _dispatch checks every request
# whose bearer resolves to a personal (pat:) grant against
# personal_token_route before any routing, in every route family. A route
# with no declaration, or an operation without declared scopes, gets 403;
# a token missing a declared scope gets 403. Customer reads: method, path
# pattern after /api/v1/customer, and the operation its service passes to
# _require_member (scopes: services.READER_BEARER_SCOPES). First match wins.
CUSTOMER_TOKEN_ROUTES = (
    ("GET", r"/overview", "customer_overview"),
    ("GET", r"/reader-status", "customer_reader_status"),
    ("GET", r"/search", "customer_search"),
    ("GET", r"/records/.+/mistake", "customer_mistake_read"),
    ("GET", r"/records/.+/media", "customer_recording"),
    ("GET", r"/records/.+", "customer_record_window"),
    ("GET", r"/summary", "customer_summary"),
    ("GET", r"/brands", "customer_brands"),
    ("GET", r"/brands/[^/]+", "customer_brand_detail"),
    ("GET", r"/shows", "customer_shows"),
    ("GET", r"/episodes/[^/]+", "customer_episode_reads"),
    ("GET", r"/shows/[^/]+", "customer_show_detail"),
    ("GET", r"/shows/[^/]+/artwork", "customer_show_artwork"),
    ("GET", r"/compare", "customer_compare"),
    ("GET", r"/filter-values", "customer_filter_values"),
    ("POST", r"/research-query", "customer_research_query"),
    ("GET", r"/archive", "customer_archive"),
    ("GET", r"/exports/\d+", "customer_export_download"),
    ("GET", r"/creatives/[^/]+", "customer_creative_detail"),
    ("GET", r"/saved-items", "customer_saved_items"),
    ("GET", r"/saved-items/[^/]+", "customer_saved_item"),
    ("GET", r"/mistakes", "customer_note_collection"),
    ("GET", r"/mistakes/[^/]+", "customer_note_collection"),
    ("GET", r"/alerts", "customer_alerts"),
    ("GET", r"/alerts/\d+", "customer_alert"),
    ("GET", r"/preferences", "customer_preferences"),
    ("GET", r"/campaigns", "customer_campaigns"),
    ("GET", r"/campaigns/\d+", "customer_campaign_detail"),
    ("GET", r"/campaigns/\d+/draft", "customer_campaign_draft"),
    ("GET", r"/campaigns/\d+/evidence-fit", "customer_evidence_fit"),
    ("GET", r"/campaigns/\d+/targets", "customer_campaign_targets"),
    ("GET", r"/campaigns/\d+/reports", "customer_reports"),
    ("GET", r"/campaigns/\d+/reports/\d+", "customer_report"),
    ("GET", r"/campaigns/\d+/reports/\d+/html", "customer_report_html"),
    ("GET", r"/briefs", "customer_briefs"),
    ("GET", r"/briefs/[^/]+/versions/[^/]+", "customer_briefs"),
)
# The token's own connection: status, audit and revoke of the presented
# grant only (no workspace data, no scope).
SELF_CONNECTION_ROUTES = (
    ("GET", r"/api/v1/connections"),
    ("GET", r"/api/v1/connections/[^/]+/audit"),
    ("POST", r"/api/v1/connections/[^/]+/revoke"),
)
# Credential-free routes: no workspace data; the bearer is ignored.
PUBLIC_TOKEN_ROUTES = (
    ("GET", "/healthz"), ("GET", "/readyz"),
    ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ("GET", "/api/v1/openapi.json"),
)


def personal_token_route(method: str, path: str):
    """(family, operation, scopes) for a route a personal token may call,
    or None. scopes is None when the operation declares none (refused).
    MCP and agent REST tools: agent_contracts.TOOL_SCOPES (the MCP
    transport checks each tool call)."""
    import re as _re
    from . import agent_contracts as _ac
    from . import services as _services
    if (method, path) in PUBLIC_TOKEN_ROUTES:
        return ("public", path, ())
    if path == MCP_PATH:
        # Every method: _mcp authenticates, then answers 405 to non-POST
        # (MCP clients read 405 on GET/DELETE as "not offered").
        return ("mcp", "mcp", ())
    for route_method, route_path, tool in AGENT_ROUTES:
        if method == route_method and HostedApp._match(route_path, path) is not None:
            return ("agent", tool, _ac.TOOL_SCOPES.get(tool))
    if path.startswith("/api/v1/customer/"):
        base = path[len("/api/v1/customer"):]
        for route_method, pattern, operation in CUSTOMER_TOKEN_ROUTES:
            if method == route_method and _re.fullmatch(pattern, base, _re.DOTALL):
                return ("customer", operation,
                        _services.READER_BEARER_SCOPES.get(operation))
        return None
    for route_method, pattern in SELF_CONNECTION_ROUTES:
        if method == route_method and _re.fullmatch(pattern, path):
            return ("self", "connection", ())
    return None


@dataclass
class HostedConfig:
    """Production configuration; names match the SETUP env contract."""
    resource_uri: str = ""
    issuer: str = ""
    public_base_url: str = ""
    grants_db: str = ""
    data_dir: str = ""
    app_dir: str = ""
    database_url: str = ""
    jwks_file: str = ""  # local JWKS for provider tokens (agent_verify)
    max_token_lifetime_s: int = 900
    require_at_jwt: bool = True
    allowed_hosts: tuple = ()
    allowed_origins: tuple = ()
    # Operations. mode is "production" (every safety rule is enforced at
    # start) or "local-test" (explicit; fixtures and local runs). The pilot
    # runs exactly one web process: the rate limiter below is per process,
    # so web_processes must be 1 and WEB_CONCURRENCY unset or 1.
    mode: str = ""
    web_processes: int = 1
    web_concurrency: str = ""
    error_sink: str = ""  # "module:callable"; empty = error reporting off
    # L14 admission file. None (hand-built config) defers to the process
    # environment. The start-up check reads this field, but the run-time
    # gates read the process environment, so the two agree only when both
    # come from config_from_env over the same environment. Hand-built
    # configs are for tests.
    admission_file: str | None = None
    # Pilot rights (hosted runtime 5.2). pilot_features: None (variable
    # unset) means every feature and is refused in production; () means
    # none. pilot_end: the approved end, ISO 8601 with seconds and Z or an
    # explicit offset, compared as an instant.
    pilot_features: tuple | None = None
    pilot_end: str = ""
    # Built-in email and password sign-in (EARWITNESS_SIGNIN_LOCAL=on). The
    # local issuer is the origin of public_base_url.
    signin_local: bool = False
    # The window runner's last-success.json (fresh-data design 12.1):
    # Overview's "Data updated". Unreadable or invalid is left out.
    last_success_file: str = _freshness.DEFAULT_LAST_SUCCESS_FILE
    # The clip guard (CLIPGUARD R8; EARWITNESS_AD_RULES_CLIP_GUARD): on, off,
    # or empty (off). create_app sets it for the process, once
    # (ad_reads.set_clip_guard); another value refuses to start.
    clip_guard: str = ""
    # Requests in flight at once per workspace (EARWITNESS_WORKSPACE_MAX_IN_FLIGHT).
    workspace_max_in_flight: int = MAX_CONCURRENT_PER_WORKSPACE
    # Requests waiting for a limiter slot in the whole process (derived
    # from uvicorn's limit-concurrency; see _waiters_limit).
    limit_waiters: int = LIMIT_WAITERS_MAX
    clock: object = field(default=None)  # injectable now() -> float for tests
    # READ-CACHE warm-up (ReadWarmer): seconds between data-state checks of
    # every registered workspace; 0 turns warm-up off (hand-built configs).
    read_warm_s: float = 0.0

    def now(self) -> float:
        if self.clock is not None:
            return self.clock()
        return time.time()


def _waiters_limit(source, argv) -> int:
    """The process-wide waiter limit: a quarter of uvicorn's
    limit-concurrency (--limit-concurrency N or =N on the command line,
    else UVICORN_LIMIT_CONCURRENCY), at least 1 and at most
    LIMIT_WAITERS_MAX. Unset or unreadable: LIMIT_WAITERS_MAX."""
    raw = str(source.get("UVICORN_LIMIT_CONCURRENCY", "") or "").strip()
    for n, arg in enumerate(argv):
        if arg == "--limit-concurrency" and n + 1 < len(argv):
            raw = argv[n + 1].strip()
        elif arg.startswith("--limit-concurrency="):
            raw = arg.split("=", 1)[1].strip()
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        return LIMIT_WAITERS_MAX
    return max(1, min(LIMIT_WAITERS_MAX, int(raw) // 4))


def config_from_env(env=None, argv=None) -> HostedConfig:
    import os
    source = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    hosts = tuple(h for h in source.get("EARWITNESS_ALLOWED_HOSTS", "").split(",") if h)
    origins = tuple(o for o in source.get("EARWITNESS_ALLOWED_ORIGINS", "").split(",") if o)
    try:
        processes = int(source.get("EARWITNESS_WEB_PROCESSES", "1") or "1")
    except ValueError:
        processes = -1  # refused by the configuration check
    # ASCII digits only: int() would also take "+16", "1_6" and other
    # scripts' digits.
    raw_cap = str(source.get(WORKSPACE_MAX_IN_FLIGHT_ENV, "") or "").strip()
    if not raw_cap:
        workspace_cap = MAX_CONCURRENT_PER_WORKSPACE
    elif raw_cap.isascii() and raw_cap.isdigit():
        workspace_cap = int(raw_cap)
    else:
        workspace_cap = -1  # refused by the configuration check
    return HostedConfig(
        mode=source.get("EARWITNESS_MODE", ""),
        web_processes=processes,
        web_concurrency=source.get("WEB_CONCURRENCY", ""),
        error_sink=source.get("EARWITNESS_ERROR_SINK", ""),
        admission_file=source.get("EARWITNESS_ADMISSION_FILE", ""),
        resource_uri=source.get("EARWITNESS_MCP_RESOURCE_URI", ""),
        issuer=source.get("EARWITNESS_OAUTH_ISSUER", ""),
        public_base_url=source.get("EARWITNESS_PUBLIC_BASE_URL", ""),
        grants_db=source.get("EARWITNESS_GRANTS_DB", ""),
        data_dir=source.get("EARWITNESS_DATA_DIR", ""),
        app_dir=source.get("EARWITNESS_APP_DIR", ""),
        database_url=source.get("EARWITNESS_DATABASE_URL", ""),
        jwks_file=source.get("EARWITNESS_OAUTH_JWKS_FILE", ""),
        max_token_lifetime_s=int(
            source.get("EARWITNESS_OAUTH_MAX_TOKEN_LIFETIME_S") or 900),
        require_at_jwt=source.get("EARWITNESS_OAUTH_REQUIRE_AT_JWT", "").lower()
        not in ("0", "false", "no"),
        allowed_hosts=hosts, allowed_origins=origins,
        pilot_features=_rights.parse_features(source.get("EARWITNESS_PILOT_FEATURES")),
        pilot_end=source.get("EARWITNESS_PILOT_END", ""),
        signin_local=str(source.get("EARWITNESS_SIGNIN_LOCAL") or "").strip().lower() == "on",
        last_success_file=source.get("EARWITNESS_LAST_SUCCESS_FILE")
        or _freshness.DEFAULT_LAST_SUCCESS_FILE,
        clip_guard=source.get("EARWITNESS_AD_RULES_CLIP_GUARD") or "",
        workspace_max_in_flight=workspace_cap,
        limit_waiters=_waiters_limit(source, argv),
        read_warm_s=_read_warm_seconds(source.get("EARWITNESS_READ_WARM_S"),
                                       2.0 if env is None else 0.0))


class ClientGone(Exception):
    """The client disconnected while its request waited for a limiter
    slot. Nothing is sent; the request holds no slot from that wait."""


class BodyWhileWaiting(ClientGone):
    """A body this request had not read (a GET with one, say) arrived
    while it waited for a limiter slot. The watch keeps none of it and
    reads no more; the request leaves the queue and answers 413 with
    Connection: close (LW-R1-01)."""


class _Receive:
    """The request's ASGI receive, able to watch for a disconnect while
    the request waits for a limiter slot. The watch keeps no body bytes:
    only an empty whole body and the disconnect are kept (and handed out
    first, so a handler never loses one). A body not read yet ends the
    watch at its first message (BodyWhileWaiting); the rest stays unread,
    so the transport's flow control holds it back."""

    def __init__(self, receive):
        self._receive = receive
        self._kept: collections.deque = collections.deque()
        self._task = None
        self._gone = None
        self._complete = False  # a whole request body has been read

    async def __call__(self):
        if self._kept:
            return self._kept.popleft()
        message = await self._receive()
        self._note(message)
        return message

    def _note(self, message) -> None:
        """Mark the whole body read once its last message has passed."""
        if message.get("type") == "http.request" and not message.get("more_body"):
            self._complete = True

    def gone(self):
        """A future set when the wait must end: its result is the
        ClientGone (or BodyWhileWaiting) to raise. Watching starts now."""
        loop = asyncio.get_running_loop()
        if self._gone is None:
            self._gone = loop.create_future()
        if self._task is None and not self._gone.done():
            self._task = loop.create_task(self._watch())
        return self._gone

    def _end(self, reason: ClientGone) -> None:
        if not self._gone.done():
            self._gone.set_result(reason)

    async def _watch(self):
        while True:
            message = await self._receive()
            if message.get("type") == "http.disconnect":
                self._kept.append(message)
                self._end(ClientGone())
                return
            if message.get("type") != "http.request":
                continue
            if self._complete:
                # A second whole body: this transport does not block until
                # a disconnect (a test double). Stop rather than spin.
                return
            if message.get("body") or message.get("more_body"):
                self._end(BodyWhileWaiting())  # kept: nothing
                return
            self._complete = True
            self._kept.append(message)

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None


# SPEED-FIX2 (screen review SPV-C-02): the current request's thread pool
# time: how long its pool calls waited for a thread and how long they ran
# (request.complete pool_wait_ms, pool_run_ms, pool_calls), so a slow
# step shows whether it queued or computed.
_POOL_TIMES: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_pool_times", default=None)


def _pool_times() -> dict:
    return {"wait_s": 0.0, "run_s": 0.0, "calls": 0}


def _pool_fields(times) -> dict:
    """request.complete's pool fields; none when the request used no pool."""
    if not times or not times["calls"]:
        return {}
    return {"pool_wait_ms": round(times["wait_s"] * 1000, 1),
            "pool_run_ms": round(times["run_s"] * 1000, 1), "pool_calls": times["calls"]}


# The current request's _Receive (set in HostedApp.__call__): a limiter
# wait watches it for a disconnect.
_RECEIVE_WATCH: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_receive_watch", default=None)
# Why the limiter last refused this request (logged as limit=): "bucket"
# (an empty rate bucket), "queue_full" (that cap's wait queue is full),
# "cap" (the process waiter limit), "timeout" (the wait ran out). No
# caller identifiers.
_LIMIT_REASON: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_limit_reason", default="")
# The limiter lane of the request being admitted (ART-PERF-FIX1): "" for
# page reads, ART_LANE for a show cover (set around _with_workspace).
_LIMIT_LANE: contextvars.ContextVar = contextvars.ContextVar(
    "earwitness_limit_lane", default="")

def _read_warm_seconds(raw, default: float = 2.0) -> float:
    """EARWITNESS_READ_WARM_S: seconds between warm-up checks (0 or a
    negative number turns warm-up off; anything unreadable keeps the
    default). The default is 2 s for the server's own environment and 0
    for a config built from an explicit mapping (tests and scripts:
    RC-R1-05), unless that mapping sets it."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 and value == value and value != float("inf") else 0.0


class RateLimiter:
    """Per-grant token bucket (60/min, burst 20; a browser session's
    burst is session_burst and its rate session_reads_per_minute, the app
    SESSION_READS_PER_MINUTE), per-workspace bucket (600/min), and
    in-flight caps (2/grant, 10/workspace by default). A clip's audio
    (admit media=True) spends a per-session and a per-workspace media
    bucket instead, under the same in-flight caps. In-memory and per
    process. Topology decision: the pilot runs exactly one web process, so
    these limits are the whole limit. create_app refuses to start a second
    one (config web_processes, WEB_CONCURRENCY and a lock beside the
    control store). More than one web process needs a shared limiter first.

    A full in-flight cap waits (LIMITER-WAIT): each cap key (one grant or
    session, one workspace) has a count and a FIFO queue of futures. A
    newcomer takes a slot only when the count is under the cap and nobody
    is queued; otherwise it queues, up to grant_queue_max per grant,
    queue_max per workspace and waiters_max in the process (past any:
    refused at once), for at most wait_s (then refused). A release hands its slot
    straight to the oldest live waiter, so the count never dips and no
    newcomer can take it first; with nobody waiting the count drops. A
    waiter that times out, disconnects or is cancelled leaves its queue; if
    a slot reached it at that moment it is passed on. A request queues for
    its grant slot first, then its workspace slot, so a grant at its own
    cap never blocks other grants in the workspace queue. The rate buckets
    never wait: an empty bucket is refused at once with its own delay.
    Every method runs without awaiting inside its bookkeeping, so each is
    atomic on the event loop thread."""

    def __init__(self, reads_per_minute: int = READS_PER_MINUTE,
                 burst: int = BURST,
                 workspace_per_minute: int = WORKSPACE_READS_PER_MINUTE,
                 max_per_grant: int = MAX_CONCURRENT_PER_GRANT,
                 max_per_workspace: int = MAX_CONCURRENT_PER_WORKSPACE,
                 session_burst: int | None = None,
                 wait_s: float = LIMIT_WAIT_S,
                 queue_max: int = LIMIT_QUEUE_MAX,
                 grant_queue_max: int = LIMIT_GRANT_QUEUE_MAX,
                 waiters_max: int = LIMIT_WAITERS_MAX,
                 media_per_minute: int = MEDIA_PER_MINUTE,
                 media_burst: int = MEDIA_BURST,
                 workspace_media_per_minute: int = WORKSPACE_MEDIA_PER_MINUTE,
                 session_reads_per_minute: int | None = None):
        self._rate = reads_per_minute / 60.0
        self._session_rate = self._rate if session_reads_per_minute is None \
            else session_reads_per_minute / 60.0
        self._burst = burst
        self._session_burst = burst if session_burst is None else session_burst
        self._ws_rate = workspace_per_minute / 60.0
        self._media_rate = media_per_minute / 60.0
        self._media_burst = media_burst
        self._ws_media_rate = workspace_media_per_minute / 60.0
        self._buckets: dict = {}
        self._grants: dict = {}
        self._workspaces: dict = {}
        self._media_turns: dict = {}
        self._max_grant = max_per_grant
        self._max_ws = max_per_workspace
        self.wait_s = wait_s
        self.queue_max = queue_max
        self.grant_queue_max = grant_queue_max
        self.waiters_max = waiters_max
        self.art_waiters_max = ART_WAITERS_MAX   # the app: at most waiters_max
        self._queues: dict = {}  # (kind, key) -> deque of futures

    def waiting(self, art: bool | None = None) -> int:
        """Requests queued for a slot now: every lane, or only the cover
        lane (art=True) or only the others (art=False)."""
        return sum(len(q) for (_kind, key), q in self._queues.items()
                   if art is None or key.startswith(ART_LANE) == art)

    @staticmethod
    def _laned(key: str) -> str:
        """key in the current request's lane (_LIMIT_LANE)."""
        lane = _LIMIT_LANE.get()
        return lane + key if lane and not key.startswith(lane) else key

    def _grant_bucket(self, grant_id: str, now: float, burst: float,
                      media: bool = False, session: bool = False) -> dict:
        rate = self._media_rate if media else self._session_rate if session else self._rate
        grant = self._buckets.setdefault(
            ("media:" if media else "") + self._laned(grant_id),
            {"tokens": burst, "at": now, "rate": rate})
        if self._laned(grant_id).startswith(ART_LANE):
            burst, rate = float(ART_BURST), ART_PER_MINUTE / 60.0
            if grant["rate"] != rate:     # just made with the page-read values
                grant.update(tokens=burst, rate=rate)
        grant["tokens"] = min(
            burst, grant["tokens"] + max(0.0, now - grant["at"]) * rate)
        grant["at"] = max(grant["at"], now)
        return grant

    def _ws_bucket(self, workspace_id: str, now: float,
                   media: bool = False) -> dict:
        workspace_id = self._laned(workspace_id)
        rate = self._ws_media_rate if media else self._ws_rate
        if workspace_id.startswith(ART_LANE):
            rate = WORKSPACE_ART_PER_MINUTE / 60.0
        ws = self._buckets.setdefault(
            ("wsmedia:" if media else "ws:") + workspace_id,
            {"tokens": float(rate * 60.0), "at": now,
             "cap": float(rate * 60.0), "rate": rate})
        ws["tokens"] = min(
            ws["cap"], ws["tokens"] + max(0.0, now - ws["at"]) * rate)
        ws["at"] = max(ws["at"], now)
        return ws

    def _burst_of(self, session: bool, media: bool) -> float:
        return float(self._media_burst if media else
                     self._session_burst if session else self._burst)

    def grant_wait(self, grant_id: str, now: float, *, session: bool = False,
                   media: bool = False) -> float:
        """Seconds until the grant's own bucket (the one admit spends)
        holds a token, or 0. A refusal at admit_workspace comes after admit
        spent that token, so its Retry-After is the longer of the two."""
        grant = self._grant_bucket(grant_id, now,
                                   self._burst_of(session, media), media, session)
        return max(0.0, (1.0 - grant["tokens"]) / grant["rate"])

    def _empty(self, grant, ws) -> float:
        """Seconds until the emptier bucket holds a token, or 0."""
        if grant is not None and grant["tokens"] < 1.0:
            return (1.0 - grant["tokens"]) / grant["rate"]
        if ws["tokens"] < 1.0:
            return (1.0 - ws["tokens"]) / ws["rate"]
        return 0.0

    def _counts(self, kind: str) -> dict:
        return self._grants if kind == "grant" else \
            self._media_turns if kind == "media" else self._workspaces

    async def _take(self, kind: str, key: str, cap: int,
                    deadline: float, watch) -> bool:
        """Take one slot of the kind ("grant" or "workspace") for key,
        waiting FIFO until deadline (loop time). True when taken; False
        when refused (deadline or full queue). Raises ClientGone when the
        client disconnects while it waits; no slot is kept then."""
        key = self._laned(key)
        counts = self._counts(kind)
        queue = self._queues.get((kind, key))
        art = key.startswith(ART_LANE)
        if art:
            cap = (ART_MAX_IN_FLIGHT_PER_GRANT if kind == "grant"
                   else ART_MAX_IN_FLIGHT_PER_WORKSPACE)
        if counts.get(key, 0) < cap and not queue:
            counts[key] = counts.get(key, 0) + 1
            return True
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if art:
            bound = ART_GRANT_QUEUE_MAX if kind == "grant" else ART_QUEUE_MAX
        else:
            bound = self.grant_queue_max if kind == "grant" else self.queue_max
        if remaining <= 0:
            _LIMIT_REASON.set("timeout")
            return False
        if queue is not None and len(queue) >= bound:
            _LIMIT_REASON.set("queue_full")
            return False
        if self.waiting(art) >= (self.art_waiters_max if art else self.waiters_max):
            _LIMIT_REASON.set("cap")
            return False
        if queue is None:
            queue = self._queues[(kind, key)] = collections.deque()
        slot = loop.create_future()
        queue.append(slot)
        gone = watch.gone() if watch is not None else None
        try:
            await asyncio.wait([slot] if gone is None else [slot, gone],
                               timeout=remaining,
                               return_when=asyncio.FIRST_COMPLETED)
        except BaseException:  # cancelled: keep nothing, pass a handed slot on
            if watch is not None:
                watch.stop()
            if slot.done() and not slot.cancelled():
                self._give(kind, key)
            else:
                self._leave(kind, key, slot)
            raise
        if watch is not None:
            watch.stop()
        taken = slot.done() and not slot.cancelled()
        if not taken:
            self._leave(kind, key, slot)
            _LIMIT_REASON.set("timeout")
        if gone is not None and gone.done():
            if taken:
                self._give(kind, key)
            reason = gone.result()
            raise reason if isinstance(reason, ClientGone) else ClientGone()
        return taken

    def _leave(self, kind: str, key: str, slot) -> None:
        """Take a waiter that got no slot out of its queue."""
        slot.cancel()
        queue = self._queues.get((kind, key))
        if queue is None:
            return
        try:
            queue.remove(slot)
        except ValueError:
            pass
        if not queue:
            del self._queues[(kind, key)]

    def _give(self, kind: str, key: str) -> None:
        """Hand one slot to the oldest live waiter for key, or free it."""
        key = self._laned(key)
        counts = self._counts(kind)
        queue = self._queues.get((kind, key))
        handed = False
        while queue:
            slot = queue.popleft()
            if not slot.done():
                slot.set_result(True)
                handed = True
                break
        if queue is not None and not queue:
            del self._queues[(kind, key)]
        if not handed:
            counts[key] = max(0, counts.get(key, 1) - 1)

    async def admit(self, grant_id: str, workspace_id: str,
                    now: float, *, session: bool = False, media: bool = False,
                    watch=None, deadline: float | None = None) -> float:
        """Return 0 when admitted, or seconds to wait (429 Retry-After).
        A full in-flight cap waits until deadline (loop time; default
        wait_s from now). Raises ClientGone after a disconnect (watch).
        media=True spends the media buckets instead of the read buckets
        (same in-flight caps). A refusal costs nothing, as for page reads."""
        burst = self._burst_of(session, media)
        grant = self._grant_bucket(grant_id, now, burst, media, session)
        empty = self._empty(grant, self._ws_bucket(workspace_id, now, media))
        if empty:
            _LIMIT_REASON.set("bucket")
            return empty
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.wait_s
        if not await self._take("grant", grant_id, self._max_grant,
                                deadline, watch):
            return 1.0
        try:
            if not await self._take("workspace", workspace_id,
                                    self._max_ws, deadline, watch):
                self._give("grant", grant_id)
                return 1.0
        except BaseException:
            self._give("grant", grant_id)
            raise
        # Tokens may have been spent while this request waited. The same
        # `now` (the caller's clock): the wait earns no tokens.
        grant = self._grant_bucket(grant_id, now, burst, media, session)
        ws = self._ws_bucket(workspace_id, now, media)
        empty = self._empty(grant, ws)
        if empty:
            self._give("workspace", workspace_id)
            self._give("grant", grant_id)
            _LIMIT_REASON.set("bucket")
            return empty
        grant["tokens"] -= 1.0
        ws["tokens"] -= 1.0
        return 0.0

    async def admit_workspace(self, workspace_id: str, now: float, *,
                              media: bool = False, watch=None,
                              deadline: float | None = None,
                              media_turns: int = 0) -> float:
        """Charge one workspace's own bucket and in-flight cap. Called
        only after the caller is known to be authorized for that
        workspace; before that, callers are charged to a bucket of their
        own. Return 0 when admitted, or seconds to wait when capped; a
        full cap waits as in admit. media=True charges the workspace's
        media bucket; a refusal here costs nothing (the bucket is shared
        by every member, so one member's refusals never push another's).
        media_turns (a clip read: the app's media threads): first wait,
        FIFO until deadline, for one of the workspace's media_turns turns,
        and only then take the in-flight slot, so a clip read waiting for
        a media thread holds no slot a page read could use (PFC-R1-04).
        An admitted clip read gives its turn back with release_media_turn."""
        empty = self._empty(None, self._ws_bucket(workspace_id, now, media))
        if empty:
            _LIMIT_REASON.set("bucket")
            return empty
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.wait_s
        turn = bool(media and media_turns)
        if turn and not await self._take("media", workspace_id, media_turns,
                                         deadline, watch):
            return 1.0
        try:
            taken = await self._take("workspace", workspace_id, self._max_ws,
                                     deadline, watch)
        except BaseException:
            if turn:
                self._give("media", workspace_id)
            raise
        if not taken:
            if turn:
                self._give("media", workspace_id)
            return 1.0
        ws = self._ws_bucket(workspace_id, now, media)  # spent while it waited?
        empty = self._empty(None, ws)
        if empty:
            self._give("workspace", workspace_id)
            if turn:
                self._give("media", workspace_id)
            _LIMIT_REASON.set("bucket")
            return empty
        ws["tokens"] -= 1.0
        return 0.0

    async def release_workspace(self, workspace_id: str) -> None:
        self._give("workspace", workspace_id)

    async def release_media_turn(self, workspace_id: str) -> None:
        self._give("media", workspace_id)

    async def release(self, grant_id: str, workspace_id: str) -> None:
        self._give("grant", grant_id)
        self._give("workspace", workspace_id)


class SigninLimiter:
    """Anonymous sign-in limits (L20), in memory and per process.

    Methods never await, so each call is atomic on the event loop thread.
    Every attempt spends its own address's bucket; provider work also
    takes a provider slot. When more than SIGNIN_MAX_TRACKED addresses are
    remembered, idle entries whose bucket has refilled are dropped first,
    then the least recently used idle ones. An entry with an attempt in
    flight is never dropped; while many are in flight, the table is
    rescanned only after it grows by another tenth of the cap."""

    def __init__(self):
        self._rate = SIGNIN_PER_MINUTE / 60.0
        # key -> [tokens, at, in_flight]; insertion order is recency.
        self._addresses: dict = {}
        self._provider_in_flight = 0
        self._next_prune = 0

    def tracked(self) -> int:
        return len(self._addresses)

    def admit_address(self, key: str, now: float) -> float:
        """Return 0 when admitted, or seconds to wait."""
        entry = self._addresses.pop(key, None) or [float(SIGNIN_BURST), now, 0]
        entry[0] = min(float(SIGNIN_BURST),
                       entry[0] + max(0.0, now - entry[1]) * self._rate)
        entry[1] = now
        self._addresses[key] = entry
        if entry[2] >= SIGNIN_MAX_PER_ADDRESS:
            wait = 1.0
        elif entry[0] < 1.0:
            wait = (1.0 - entry[0]) / self._rate
        else:
            entry[0] -= 1.0
            entry[2] += 1
            wait = 0.0
        self._prune(key, now)
        return wait

    def release_address(self, key: str) -> None:
        entry = self._addresses.get(key)
        if entry is not None:
            entry[2] = max(0, entry[2] - 1)

    def admit_provider(self, now: float) -> float:
        """Take a provider slot: 0 when taken, or seconds to wait."""
        if self._provider_in_flight >= SIGNIN_MAX_CONCURRENT:
            return 1.0
        self._provider_in_flight += 1
        return 0.0

    def release_provider(self) -> None:
        self._provider_in_flight = max(0, self._provider_in_flight - 1)

    def _prune(self, current: str, now: float) -> None:
        cap = SIGNIN_MAX_TRACKED
        size = len(self._addresses)
        if size <= cap or size < self._next_prune:
            return
        # Prune a tenth below the cap, and scan again only after another
        # tenth of growth, so the scan runs once per many inserts even when
        # most entries are in flight and cannot be dropped.
        step = max(1, cap // 10)
        target = max(1, cap - step)
        self._prune_to(current, now, target)
        self._next_prune = len(self._addresses) + step

    def _prune_to(self, current: str, now: float, target: int) -> None:
        for key in list(self._addresses):
            if len(self._addresses) <= target:
                return
            tokens, at, flying = self._addresses[key]
            if key != current and not flying and \
                    tokens + max(0.0, now - at) * self._rate >= SIGNIN_BURST:
                del self._addresses[key]
        for key in list(self._addresses):
            if len(self._addresses) <= target:
                return
            if key != current and not self._addresses[key][2]:
                del self._addresses[key]


class _PairHandle:
    __slots__ = ("key", "entry", "done")

    def __init__(self, key: str, entry: list):
        self.key = key
        self.entry = entry
        self.done = False


class PairLimiter:
    """Password sign-in limit per (email key, address key) (PW design 7.2),
    in memory and per process. Methods never await, so each call is atomic
    on the event loop thread.

    The key is an HMAC under a process secret, so the table holds no email
    or address. An entry is [failures, window_start, in_flight]. A
    reservation is refused when failures plus attempts in flight reach the
    limit inside the window. A handle completes once, on its own entry: a
    failure counts (starting the window), a success clears the count, no
    outcome only ends the reservation. An entry with a reservation open is
    never deleted; idle entries go when clean or expired. At the cap a new
    key first prunes; if the table is still full it is refused."""

    def __init__(self, *, limit: int = PAIR_LIMIT, window_s: float = PAIR_WINDOW_S,
                 cap: int = PAIR_MAX_TRACKED):
        import secrets as _secrets
        self._secret = _secrets.token_bytes(32)
        self._limit = limit
        self._window = window_s
        self._cap = cap
        self._entries: dict = {}

    def tracked(self) -> int:
        return len(self._entries)

    def key(self, email_key: str, address_key: str) -> str:
        import hashlib as _hashlib
        import hmac as _hmac
        return _hmac.new(self._secret, f"{email_key}\0{address_key}".encode(
            "utf-8", "surrogatepass"), _hashlib.sha256).hexdigest()

    def _expired(self, entry: list, now: float) -> bool:
        return entry[0] > 0 and now - entry[1] >= self._window

    def prune(self, now: float) -> None:
        for key in [k for k, e in self._entries.items()
                    if not e[2] and (not e[0] or self._expired(e, now))]:
            del self._entries[key]

    def reserve(self, key: str, now: float) -> tuple:
        """(handle, 0) when admitted, or (None, seconds to wait)."""
        entry = self._entries.get(key)
        if entry is None:
            if len(self._entries) >= self._cap:
                self.prune(now)
                if len(self._entries) >= self._cap:
                    return None, self._window
            entry = self._entries[key] = [0, now, 0]
        if self._expired(entry, now):
            entry[0] = 0
        if entry[0] + entry[2] >= self._limit:
            wait = entry[1] + self._window - now if entry[0] else 1.0
            return None, max(1.0, wait)
        entry[2] += 1
        return _PairHandle(key, entry), 0.0

    def complete(self, handle, outcome, now: float) -> None:
        """outcome: True (success), False (failure) or None (no outcome)."""
        if handle is None or handle.done:
            return
        handle.done = True
        entry = handle.entry
        entry[2] = max(0, entry[2] - 1)
        if outcome is False:
            if not entry[0] or self._expired(entry, now):
                entry[0], entry[1] = 1, now
            else:
                entry[0] += 1
        elif outcome is True:
            entry[0] = 0
        if not entry[2] and not entry[0] and self._entries.get(handle.key) is entry:
            del self._entries[handle.key]


def _signin_address(host) -> str:
    """The per-address limit key: an IPv4 address as is, an IPv6 address
    by its /64 (one site or subscriber), anything unparseable as given."""
    text = str(host or "")
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text or "unknown"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# Plain pages for a failed sign-in callback in a browser (L25, Sol
# ui-direction-r1). The admission refusal gets its own fixed page; it
# carries no reason, account, policy or count.
ACCESS_UNAVAILABLE_PAGE = "/app/#/access-unavailable"
SIGNIN_FAILED_PAGE = "/app/#/signin-failed"


def _browser_navigation(headers: dict) -> bool:
    """A top-level browser navigation, not a fetch or API client."""
    return headers.get("sec-fetch-mode", "") == "navigate" or \
        "text/html" in headers.get("accept", "")


def _callback_page_send(app, send):
    """Wrap send for a browser callback: a success passes through; a
    failure (status 400 or more) is replaced by a redirect to the
    admission page (the admission refusal) or the sign-in page (anything
    else). Cookies the failure set are kept; its body is dropped."""
    from . import admission as _admission
    held: dict = {}

    async def page_send(message):
        if message["type"] == "http.response.start" and message["status"] < 400:
            held["pass"] = True
        if held.get("pass"):
            await send(message)
            return
        if message["type"] == "http.response.start":
            held["start"] = message
            held["body"] = []
            return
        held["body"].append(message.get("body", b""))
        if message.get("more_body"):
            return
        start = held["start"]
        try:
            payload = json.loads(b"".join(held["body"]) or b"{}")
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        refused = start["status"] == 403 and isinstance(error, dict) and \
            error.get("message") == _admission.REFUSAL
        dest = ACCESS_UNAVAILABLE_PAGE if refused else SIGNIN_FAILED_PAGE
        cookies = tuple(("Set-Cookie", value.decode("latin-1")) for name, value
                        in start.get("headers", []) if name == b"set-cookie")
        await app._send(send, *_json_response(
            302, {"return_to": dest}, (("Location", dest),) + cookies))

    return page_send


def _json_response(status: int, payload: dict, headers=()) -> tuple:
    if status >= 400 and isinstance(payload, dict) and "ok" not in payload:
        payload = {"ok": False, **payload}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return status, body, headers


def _unauthorized(config: HostedConfig) -> tuple:
    # RFC 9728: the well-known segment sits between the origin and the
    # resource path, where the metadata route serves it.
    parts = urllib.parse.urlsplit(config.resource_uri)
    origin = f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""
    pointer = (origin + "/.well-known/oauth-protected-resource" +
               (parts.path.rstrip("/") or MCP_PATH))
    return _json_response(
        401, {"error": {"code": "UNAUTHORIZED",
                        "message": "missing or invalid token",
                        "status": 401, "details": {}}},
        (("WWW-Authenticate", f'Bearer resource_metadata="{pointer}",'
                              ' error="invalid_token"'),))


def _forbidden(code: str, message: str) -> tuple:
    return _json_response(403, {"error": {"code": code, "message": message,
                                          "status": 403, "details": {}}})


# K6 (RA-R3-02): a refused CSRF token says so with a stable marker,
# error.reason "csrf_invalid", so the customer app keys on the marker, never
# on the message text. Status, code and message stay as they were. The
# marker is sent only when the token or Origin check already failed: it says
# nothing about the session, the account or the workspace.
CSRF_INVALID = "csrf_invalid"
_CSRF_MESSAGE = "valid CSRF token is required"


def _csrf_refused() -> tuple:
    return _json_response(403, {"error": {
        "code": "FORBIDDEN", "message": _CSRF_MESSAGE, "status": 403,
        "details": {}, "reason": CSRF_INVALID}})


def _csrf_error():
    exc = _access.AuthorizationError(_CSRF_MESSAGE, 403)
    exc.csrf_invalid = True
    return exc


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """If-None-Match (RFC 9110 13.1.2, weak comparison) against our strong
    ETag. Caddy's encode handler adds "-<encoding>" to a strong ETag on a
    compressed answer, so a browser may send that form back; the suffix
    is ignored here."""
    want = etag.strip('"')
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        tag = tag.strip('"')
        for enc in ("-gzip", "-zstd", "-br"):
            if tag.endswith(enc):
                tag = tag[:-len(enc)]
        if tag and tag == want:
            return True
    return False


def _not_found() -> tuple:
    return _json_response(404, {"error": {"code": "NOT_FOUND",
                                          "message": "unknown object",
                                          "status": 404, "details": {}}})


def _ask_error(status: int, code: str) -> tuple:
    """An Ask refusal (ASK-VIEW.md): its code only, never question, answer
    or ledger text."""
    return _json_response(status, {"error": {"code": code, "message": code,
                                             "status": status, "details": {}}})


def _ask_invalid(message: str) -> tuple:
    return _json_response(400, {"error": {"code": "INVALID_ARGUMENT", "message": message,
                                          "status": 400, "details": {}}})


def _ask_refusal(exc) -> tuple:
    """ASK-VIEW's refusal for an exception of ask.start_turn or the key load.
    A day cap is the day limit; another ledger code is "paused"; no model
    addendum or a refused key file is "unavailable" (Ask switched off: the
    session has no ask_paused for either, ARC-R1-03)."""
    from . import ask as _ask
    from . import meta_client as _meta
    if isinstance(exc, _ask.AskNotFound):
        return _not_found()
    if isinstance(exc, _ask.AskBusy):
        return _ask_error(429, "ASK_BUSY")
    if isinstance(exc, _ask.AskLimit):
        return _ask_error(429, "ASK_LIMIT_DAY") if exc.code == "DAY" \
            else _ask_error(503, "ASK_PAUSED")
    if isinstance(exc, _ask.AskUnavailable):
        if exc.code == _meta.DAY_CAP:
            return _ask_error(429, "ASK_LIMIT_DAY")
        if exc.code == "NO_ADDENDUM":
            return _ask_error(403, "ASK_UNAVAILABLE")
        return _ask_error(503, "ASK_PAUSED")
    if isinstance(exc, _meta.MetaKeyError):
        return _ask_error(503, "ASK_UNAVAILABLE")
    return _ask_error(400, "INVALID_QUESTION")


def _meaning_error(status: int, code: str) -> tuple:
    """A meaning-alert refusal (ALERT-VIEW.md): its code only, never
    sentence, passage or ledger text."""
    return _json_response(status, {"error": {"code": code, "message": code,
                                             "status": status, "details": {}}})


def _meaning_refusal(exc) -> tuple:
    """ALERT-VIEW's refusal for an exception of the meaning module or the
    key load. A day cap (the member's or the ledger's) is the day limit; any
    other ledger code or a key problem is "paused"; no addendum is
    "unavailable"."""
    from . import meaning as _meaning
    from . import meta_client as _meta
    if isinstance(exc, _meaning.MeaningNotFound):
        return _not_found()
    if isinstance(exc, _meaning.MeaningBusy):
        return _meaning_error(429, "ALERT_BUSY")
    if isinstance(exc, _meaning.MeaningNotActive):
        return _meaning_error(409, "ALERT_NOT_ACTIVE")
    if isinstance(exc, _meaning.MeaningLimit):
        return _meaning_error(429, "ALERT_LIMIT_DAY") if exc.code == "DAY" \
            else _meaning_error(503, "ALERT_PAUSED")
    if isinstance(exc, _meaning.MeaningUnavailable):
        if exc.code == _meta.DAY_CAP:
            return _meaning_error(429, "ALERT_LIMIT_DAY")
        if exc.code == "NO_ADDENDUM":
            return _meaning_error(403, "ALERT_UNAVAILABLE")
        return _meaning_error(503, "ALERT_PAUSED")
    if isinstance(exc, _meta.MetaKeyError):
        return _meaning_error(503, "ALERT_PAUSED")
    return _meaning_error(400, "INVALID_ALERT")


def _brief_check_error(status: int, code: str) -> tuple:
    """A live brief-check refusal: its code only, never brief or ledger text."""
    return _json_response(status, {"error": {"code": code, "message": code,
                                             "status": status, "details": {}}})


def _brief_check_refusal(exc) -> tuple:
    """The refusal for an exception of brief_check_live or the key load: a
    day cap (the member's or the ledger's) is the day limit; no addendum,
    an excerpt-level workspace or a missing table is "unavailable"; any
    other ledger code or a key problem is "paused"."""
    from . import brief_check_live as _live
    from . import meta_client as _meta
    if isinstance(exc, _live.BriefCheckNotFound):
        return _not_found()
    if isinstance(exc, _live.BriefChanged):
        return _brief_check_error(409, "BRIEF_CHANGED")
    if isinstance(exc, _live.BriefCheckBusy):
        return _brief_check_error(429, "BRIEF_CHECK_BUSY")
    if isinstance(exc, _live.BriefCheckLimit):
        return _brief_check_error(429, "BRIEF_CHECK_LIMIT_DAY")
    if isinstance(exc, _live.BriefCheckUnavailable):
        if exc.code == _meta.DAY_CAP:
            return _brief_check_error(429, "BRIEF_CHECK_LIMIT_DAY")
        if exc.code in ("NO_ADDENDUM", "EXCERPT_LEVEL", "TABLE_MISSING", "SWITCH_OFF"):
            return _brief_check_error(403, "BRIEF_CHECK_UNAVAILABLE")
        return _brief_check_error(503, "BRIEF_CHECK_PAUSED")
    if isinstance(exc, _meta.MetaKeyError):
        return _brief_check_error(503, "BRIEF_CHECK_PAUSED")
    return _brief_check_error(400, "INVALID_BRIEF_CHECK")


# Presentation-only static surface (mirrors scripts/app_server.py).
# The public worker owns app/public/* bytes; both adapters only route,
# allowlist and label them. Customer shell stays under /app/.
APP_STATIC = {"index.html": "text/html; charset=utf-8",
              "app.js": "text/javascript; charset=utf-8",
              "ui.js": "text/javascript; charset=utf-8",
              "saved.js": "text/javascript; charset=utf-8",
              "views.js": "text/javascript; charset=utf-8",
              "styles.css": "text/css; charset=utf-8",
              # Design v3 (Exhibit): tokens, print styles, page-group CSS.
              "tokens.css": "text/css; charset=utf-8",
              "print.css": "text/css; charset=utf-8",
              "search.css": "text/css; charset=utf-8",
              "evidence.css": "text/css; charset=utf-8",
              "library.css": "text/css; charset=utf-8",
              "work.css": "text/css; charset=utf-8",
              "account.css": "text/css; charset=utf-8",
              # Redesign (REDESIGN-BUILD): base, page modules, page styles.
              "app-b2.css": "text/css; charset=utf-8",
              "b2.js": "text/javascript; charset=utf-8",
              "search-b2.css": "text/css; charset=utf-8",
              "ad-b2.css": "text/css; charset=utf-8",
              "overview-b2.css": "text/css; charset=utf-8",
              "brand-b2.css": "text/css; charset=utf-8",
              "signin-b2.css": "text/css; charset=utf-8",
              "page-search.js": "text/javascript; charset=utf-8",
              "page-ad.js": "text/javascript; charset=utf-8",
              "page-overview.js": "text/javascript; charset=utf-8",
              "page-brand.js": "text/javascript; charset=utf-8",
              "page-signin.js": "text/javascript; charset=utf-8",
              "fonts/plex.css": "text/css; charset=utf-8",
              "fonts/OFL.txt": "text/plain; charset=utf-8"}
# Self-hosted IBM Plex (SIL OFL 1.1): exact file names only, served
# without sign-in so public and operator pages can use them too.
_PLEX = ("IBMPlexSans-400", "IBMPlexSans-500", "IBMPlexSans-600",
         "IBMPlexSans-700", "IBMPlexSans-Italic-400", "IBMPlexSerif-400",
         "IBMPlexSerif-500", "IBMPlexSerif-600", "IBMPlexSerif-Italic-400",
         "IBMPlexMono-400", "IBMPlexMono-500", "IBMPlexMono-600")
APP_STATIC.update({f"fonts/{face}-{subset}.woff2": "font/woff2"
                   for face in _PLEX for subset in ("latin", "latin-ext")})
PUBLIC_DOCS = {"index.html": "text/html; charset=utf-8",
               "access.html": "text/html; charset=utf-8",
               "coverage.html": "text/html; charset=utf-8",
               "methodology.html": "text/html; charset=utf-8",
               "privacy.html": "text/html; charset=utf-8",
               "terms.html": "text/html; charset=utf-8",
               "pricing.html": "text/html; charset=utf-8",
               "404.html": "text/html; charset=utf-8"}
PUBLIC_ASSETS = {"public.css": "text/css; charset=utf-8",
                 "public.js": "text/javascript; charset=utf-8"}
# Landing images (LAND-2): show cover art, brand logos and product
# screenshots. Exact file names under app/public/img/ only; their sources
# are in app/public/img/MANIFEST.json, which is not served.
_COVERS = ("angie-martinez-irl", "armstrong-getty-on-demand", "behind-the-bastards",
 "better-offline", "bobby-bones-presents-the-bobbycast", "club-shay-shay",
 "daniel-and-kellys-extraordinary-universe",
 "fantasypros-fantasy-football-podcast", "financial-audit", "game-scoop",
 "josh-pate-s-college-football-show",
 "kinda-funny-games-daily-video-games-news-podcast",
 "kinda-funny-gamescast-video-game-podcast", "mojo-in-the-morning",
 "myths-and-legends", "new-rory-mal", "nightcap", "pivot",
 "pod-meets-world", "prof-g-markets", "tbpn",
 "the-ai-daily-brief-artificial-intelligence-news-and-analysis",
 "the-bobby-bones-show", "the-breakfast-club", "the-command-zone",
 "the-dan-le-batard-show", "the-herd-with-colin-cowherd",
 "the-megyn-kelly-show", "the-power-trip",
 "the-rizzuto-show-daily-comedy-podcast", "the-vergecast",
 "two-ts-in-a-pod-with-teddi-mellencamp-and-tamra-judge")
_LOGOS = ("att", "betterhelp", "carvana", "chase", "chatgpt", "draftkings",
 "growtherapy", "linkedin", "mintmobile", "ondeck", "plaid", "progressive",
 "quince", "schwab", "shopify", "simplisafe", "youtube")
_SHOTS = ("search", "evidence", "ask", "ask-how", "compare")
PUBLIC_ASSETS.update({f"img/cover-{s}.jpg": "image/jpeg" for s in _COVERS})
PUBLIC_ASSETS.update({f"img/logo-{s}.png": "image/png" for s in _LOGOS})
# Brand logos for signed-in app pages (logo run 2026-10-30): each confirmed one by one by an
# independent checker; sources and sha256 in MANIFEST.json. Brands without a file show the initials tile.
_APP_LOGOS = ("aaa", "alienware", "ally", "americanexpress", "apple", "applecard",
 "article", "audible", "bitwarden", "blinds", "bloomberg", "bluechew",
 "bollbranch", "boostmobile", "capitalone", "caplyta", "cashapp",
 "claude", "cozyearth", "deleteme", "dell", "discover", "disneyplus",
 "fox", "framer", "google", "granola", "gusto", "happyglp",
 "helixsleep", "hims", "historyhit", "homechef", "hubspot", "hyundai",
 "instagram", "insurify", "ixl", "jerry", "kachava", "klarna",
 "littlebellies", "millerlite", "mod", "nbcnews", "nfl", "nordstrom",
 "oldnavy", "onepay", "orderlymeds", "pacificfertilitycenter",
 "pacificlife", "paypal", "pipedrive", "plutotv", "polymarket",
 "prizepicks", "public", "quo", "ramp", "raycon", "rinse",
 "rocketmoney", "rystiggo", "siriusxm", "solacehealth", "sonos",
 "southernnewhampshireuniversity", "squarespace", "starbucks",
 "statefarm", "subaru", "sundaysfordogs", "sweetgreen", "talkiatry",
 "threatlocker", "tiktok", "usaclimbing", "vanta", "vinted", "vuori",
 "walmart", "warbyparker", "wayfair", "whatnot", "wholefoodsmarket",
 "wise", "zazzle", "ziprecruiter")
PUBLIC_ASSETS.update({f"img/logo-{s}.png": "image/png" for s in _APP_LOGOS})
PUBLIC_ASSETS.update({f"img/shot-{s}.jpg": "image/jpeg" for s in _SHOTS})
# App icon and link preview image (POS-1): exact names only; the sources
# and sha256 are in app/public/img/MANIFEST.json. /favicon.ico stays 204.
PUBLIC_ASSETS.update({"img/favicon.svg": "image/svg+xml",
                      "img/favicon.ico": "image/x-icon",
                      "img/favicon-16.png": "image/png",
                      "img/favicon-32.png": "image/png",
                      "img/apple-touch-icon.png": "image/png",
                      "img/icon-192.png": "image/png",
                      "img/icon-512.png": "image/png",
                      "img/og-image.png": "image/png"})
PUBLIC_ROUTES = {"/": "index.html",
                 "/access/": "access.html",
                 "/coverage/": "coverage.html",
                 "/methodology/": "methodology.html",
                 "/privacy/": "privacy.html",
                 "/terms/": "terms.html",
                 "/pricing/": "pricing.html"}
# Canonical trailing slashes (Design section 2): bare public paths and
# /app redirect to their slash form, preserving the query string.
CANONICAL_REDIRECTS = {"/access": "/access/", "/coverage": "/coverage/",
                        "/methodology": "/methodology/",
                        "/privacy": "/privacy/", "/terms": "/terms/",
                        "/pricing": "/pricing/", "/app": "/app/"}
# The /pricing/ page is rendered from its template plus pricing.json
# (public_pricing). The JSON itself is never served: it is in no table.
PRICING_DOC = "pricing.html"
STATIC_CSP = ("default-src 'self'; style-src 'self';"
              " media-src 'self' https: blob:; connect-src 'self';"
              " object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def _auth_error(config: HostedConfig, exc) -> tuple:
    """Map an authentication failure to its response without flattening:
    unknown bearer -> 401 + pointer; unknown grant claim -> 404,
    preserving indistinguishability."""
    if getattr(exc, "status", 401) == 404:
        return _not_found()
    if getattr(exc, "status", 401) == 403:
        return _forbidden(exc.code if hasattr(exc, "code") else "FORBIDDEN",
                          str(exc))
    return _unauthorized(config)


def _bearer(headers: dict) -> str:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _parse_json_body(raw: bytes) -> dict:
    """Strict hosted JSON-object parse (K02 parity with the local
    adapter): duplicate keys and non-finite constants are rejected and
    the body must be an object. Raises ValueError, which adapters map
    to 400/INVALID_ARGUMENT."""
    def _no_dup_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"duplicate JSON key: {key!r}")
            obj[key] = value
        return obj
    try:
        text = raw.decode("utf-8")
        body = json.loads(text or "{}",
                          object_pairs_hook=_no_dup_keys,
                          parse_constant=lambda v: (_ for _ in ()).throw(
                              ValueError(v)))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON body: {exc}")
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    return body


def _workspace_path(config: HostedConfig, workspace_id: str) -> str:
    """Workspace directory for a grant (db.connect appends the database
    file, matching D's launch-fixed binding semantics)."""
    import os
    if not workspace_id or workspace_id.startswith((".", "/")) or \
            ".." in workspace_id:
        raise _conn.GrantError("NOT_FOUND", "unknown object", 404)
    return os.path.join(config.data_dir, workspace_id)


def _replay_body(raw: bytes, receive):
    """An ASGI receive that yields an already-read body once, then
    defers to the transport (for disconnect)."""
    pending = [{"type": "http.request", "body": raw, "more_body": False}]

    async def replay():
        return pending.pop() if pending else await receive()
    return replay


class _AbortBuffered(Exception):
    def __init__(self, messages):
        self.messages = messages


class BodyBudget:
    """The shared pre-authentication body budget (L21), per process.

    Methods never await, so each call is atomic on the event loop thread.
    Only addresses with a read in flight are remembered, so the table is
    never larger than max_reads."""

    def __init__(self, max_reads: int = BODY_MAX_READS,
                 max_bytes: int = BODY_MAX_BUFFERED_BYTES,
                 max_per_address: int = BODY_MAX_PER_ADDRESS):
        self.max_reads = max_reads
        self.max_bytes = max_bytes
        self.max_per_address = max_per_address
        self.reads = 0
        self.buffered = 0
        self._addresses: dict = {}

    def open(self, key: str) -> int:
        """Start a read: 0 when admitted, else the refusal status."""
        if self.reads >= self.max_reads:
            return 503
        if self._addresses.get(key, 0) >= self.max_per_address:
            return 429
        self.reads += 1
        self._addresses[key] = self._addresses.get(key, 0) + 1
        return 0

    def fits(self, size: int) -> bool:
        return self.buffered + size <= self.max_bytes

    def reserve(self, size: int) -> bool:
        if not self.fits(size):
            return False
        self.buffered += size
        return True

    def close(self, key: str, held: int) -> None:
        self.reads = max(0, self.reads - 1)
        self.buffered = max(0, self.buffered - held)
        left = self._addresses.get(key, 1) - 1
        if left > 0:
            self._addresses[key] = left
        else:
            self._addresses.pop(key, None)


class _BodyRefused(Exception):
    def __init__(self, status: int, code: str, message: str, headers=()):
        super().__init__(message)
        self.response = _json_response(status, {"error": {
            "code": code, "message": message, "status": status,
            "details": {}}}, headers)


class _BodyDisconnected(Exception):
    """The client went away before its body was complete."""


# The password routes read their own body, after their cheap refusals and
# under the PASSWORD_BODY_MAX bound (PW design 5.1, Astra PWB-R1-01).
PASSWORD_PATHS = frozenset({"/api/v1/session/password",
                            "/api/v1/session/password/change"})


def _reads_body(method: str, path: str) -> bool:
    """Only API and MCP routes take a body; pages, assets, redirects and
    the 404 page answer without reading one. The password routes read
    theirs later (PASSWORD_PATHS)."""
    return method in ("POST", "PUT", "DELETE") and (
        path.startswith("/api/") or path == MCP_PATH) and \
        not (method == "POST" and path in PASSWORD_PATHS)


class HostedApp:
    """The production ASGI application (create via create_app)."""

    # Invite email sender (invites_send.InviteMailer). None keeps sending
    # off; create_app sets it only from the UNAPPROVED invite email config.
    invite_mailer = None
    # Stripe current-resource reads for the billing webhook. None reads
    # Stripe through the bounded helper; tests set a fixture protocol.
    billing_provider = None
    # Ask's spend ledger and Meta model client, built per request (Meta Ask
    # design 4.1). None builds them from EARWITNESS_META_BUDGET_DIR and
    # EARWITNESS_META_KEY_FILE; tests set fixture callables.
    ask_ledger = None
    ask_client = None
    # The alerts ledger and the Meta client for meaning alerts (None builds
    # them from EARWITNESS_META_BUDGET_DIR and EARWITNESS_META_KEY_FILE).
    meaning_ledger = None
    meaning_client = None
    # Non-waiting provider slots for billing calls on this process (design
    # 8.5). A slot is freed only when its worker thread ends.
    billing_slots = BILLING_SLOTS
    # Show covers read in a pool of their own (ART-PERF-FIX1), so research
    # reads in the read pool never hold them, and they never hold research.
    art_threads = 2
    # Long reads get thread pools of their own (EXL-R2-01), so they never
    # take the default executor's threads from sign-in provider I/O,
    # billing calls or sends. A request past a busy pool waits its turn.
    export_threads = 2
    # Clip audio reads (~40 ms each: publication, rights and file checks
    # and the file read): off the event loop, in a pool of their own, so
    # sessions playing clips never hold the loop or the page-read pool
    # (A429-SEC-R2-01: 32 players made other page reads ~3.3 s).
    media_threads = 2
    session_marker_threads = 2
    # Customer reads and bookmark captures that can take seconds on a
    # host-size workspace (PERF-HOL, OFF_LOOP_READS): a pool of their own,
    # so they never wait behind an export either. 4 (SPEED-R1): a cold
    # read of one class no longer holds up a second person's remembered
    # read; the in-flight caps (RateLimiter) and the write paths are
    # unchanged.
    read_threads = 4

    def __init__(self, config: HostedConfig):
        self.config = config
        self.store = _conn.GrantsStore(config.grants_db or ":memory:")
        from . import browser_auth
        control = self.store._connect()
        try:
            browser_auth.ensure_auth_schema(control)
            control.commit()
        finally:
            control.close()
        self.limiter = RateLimiter(
            session_burst=SESSION_BURST,
            session_reads_per_minute=SESSION_READS_PER_MINUTE,
            max_per_workspace=config.workspace_max_in_flight,
            waiters_max=config.limit_waiters)
        self.limiter.art_waiters_max = min(ART_WAITERS_MAX, config.limit_waiters)
        self.signin_limiter = SigninLimiter()
        self.pair_limiter = PairLimiter()
        self.signin_cache = browser_auth.ProviderCache()
        from . import billing as _billing_mod
        self.billing_slots_used = 0
        self.billing_locks = _billing_mod.SubscriptionLocks()
        self.body_budget = BodyBudget()
        from concurrent.futures import ThreadPoolExecutor
        self.export_pool = ThreadPoolExecutor(self.export_threads, "ew-export")
        self.session_marker_pool = ThreadPoolExecutor(self.session_marker_threads,
                                                      "ew-session-marker")
        self.read_pool = ThreadPoolExecutor(self.read_threads, "ew-read")
        self.media_pool = ThreadPoolExecutor(self.media_threads, "ew-media")
        # session hash -> [asyncio.Lock, waiters]: one readiness check per
        # session at a time (_session_read_marker).
        self._session_read_gates: dict = {}
        self._mcp_apps: dict = {}
        self.art_pool = ThreadPoolExecutor(self.art_threads, "ew-art")
        self.error_reporter = _oplog.ErrorReporter(sink=None)
        self._process_lock = None
        # Provider JWT access tokens: None (opaque grants only) unless the
        # issuer and a JWKS file are configured.
        from . import agent_verify
        self.verifier = agent_verify.from_config(config)

    @staticmethod
    async def _in_pool(pool, fn, *args):
        """asyncio.to_thread on the given pool: fn runs in a copy of the
        caller's context (request id, logs)."""
        times = _POOL_TIMES.get()
        if times is None:
            call = functools.partial(contextvars.copy_context().run, fn, *args)
            return await asyncio.get_running_loop().run_in_executor(pool, call)
        run = functools.partial(contextvars.copy_context().run, fn, *args)
        queued, began = time.monotonic(), []

        def call():
            began.append(time.monotonic())
            return run()
        try:
            return await asyncio.get_running_loop().run_in_executor(pool, call)
        finally:
            done = time.monotonic()
            times["calls"] += 1
            if began:
                times["wait_s"] += began[0] - queued
                times["run_s"] += done - began[0]
            else:
                times["wait_s"] += done - queued

    def close(self) -> None:
        """Release the single-process lock (tests and orderly shutdown)."""
        warmer, self.read_warmer = getattr(self, "read_warmer", None), None
        if warmer is not None:
            warmer.stop()
        lock, self._process_lock = self._process_lock, None
        if lock is not None:
            lock.close()

    def _mcp_app(self, ctx):
        from earwitness.agents import mcp_server as mcp
        from mcp.server.transport_security import TransportSecuritySettings
        server = mcp.build_server(ctx.workspace_id, ctx.db_path, ctx.scopes, context=ctx)
        # The SDK defaults to localhost-only Host/Origin checks (DNS-rebinding
        # protection for unauthenticated local servers). This endpoint is
        # public and every call needs a bearer grant, which a browser never
        # attaches on its own, so the check would only refuse real hosts.
        return server.streamable_http_app(
            streamable_http_path=MCP_PATH, stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False))

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        inbound = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                inbound = value.decode("latin-1")
        # The ID is always server generated (it becomes the audit ID). A
        # well-formed client ID is logged beside it, never used as it.
        request_id = _oplog.new_request_id()
        client_id = inbound if _oplog.valid_request_id(inbound) else None
        bound = _oplog.bind_request(request_id)
        started = time.monotonic()
        state = {"status": 0}

        async def logged_send(message):
            if message.get("type") == "http.response.start":
                state["status"] = message.get("status", 0)
                message = {**message, "headers": [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode())]}
            await send(message)

        receive = _Receive(receive)
        watch_token = _RECEIVE_WATCH.set(receive)
        reason_token = _LIMIT_REASON.set("")
        pool_token = _POOL_TIMES.set(_pool_times())
        try:
            if method == "GET" and path in ("/healthz", "/readyz"):
                # Before the Host allowlist: probes may use any Host, and the
                # bodies are fixed words with no private data.
                if path == "/healthz":
                    status, payload = _health.liveness()
                else:
                    status, payload = _health.readiness(
                        self.store, self.config.data_dir)
                await self._send(logged_send, *_json_response(status, payload))
            else:
                await self._dispatch(scope, receive, logged_send)
        except BodyWhileWaiting:
            # Its body is unread: answer and close, never read it.
            _LIMIT_REASON.set("body_while_waiting")
            await self._send(logged_send, *_json_response(413, {
                "error": {"code": "INVALID_ARGUMENT",
                          "message": "body too large", "status": 413,
                          "details": {}}}, (("Connection", "close"),)))
        except ClientGone:
            # Left while waiting for a limiter slot: nobody to answer.
            _LIMIT_REASON.set("client_gone")
        except Exception as exc:  # noqa: BLE001 - last-resort 500 with an ID
            _oplog.log_event("request.error", level="error", method=method,
                             path=path, error_type=type(exc).__name__,
                             where=_oplog.error_location(exc))
            self.error_reporter.report(exc, method=method, path=path)
            if state["status"]:
                raise
            await self._send(logged_send, *_json_response(500, {
                "error": {"code": "INTERNAL", "message": "internal error",
                          "status": 500,
                          "details": {"request_id": request_id}}}))
        finally:
            limit = _LIMIT_REASON.get()
            # limit= only when the limiter refused (a 429) or the wait
            # ended the request; an admitted request may have waited.
            _oplog.log_event(
                "request.complete", method=method, path=path,
                status=state["status"], inbound_request_id=client_id,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                **({"limit": limit} if limit and state["status"] in (0, 413, 429)
                   else {}), **_pool_fields(_POOL_TIMES.get()))
            _oplog.reset_request(bound)
            receive.stop()
            _LIMIT_REASON.reset(reason_token)
            _POOL_TIMES.reset(pool_token)
            _RECEIVE_WATCH.reset(watch_token)

    async def _dispatch(self, scope: dict, receive, send) -> None:
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        try:
            headers = {k.decode().lower(): v.decode()
                       for k, v in scope.get("headers", [])}
        except UnicodeDecodeError:
            await self._send(send, *_json_response(
                400, {"error": {"code": "INVALID_ARGUMENT",
                                "message": "request headers must be UTF-8",
                                "status": 400, "details": {}}}))
            return
        if self.config.allowed_hosts:
            host = headers.get("host", "").split(":")[0]
            if host not in self.config.allowed_hosts:
                await self._send(send, *_json_response(
                    400, {"error": {"code": "INVALID_ARGUMENT",
                                    "message": "untrusted host",
                                    "status": 400, "details": {}}}))
                return
        origin = headers.get("origin")
        if origin and self.config.allowed_origins and \
                origin not in self.config.allowed_origins:
            await self._send(send, *_json_response(
                403, {"error": {"code": "FORBIDDEN",
                                "message": "invalid origin",
                                "status": 403, "details": {}}}))
            return
        model_feature = _rights.pilot_feature(method, path)
        if model_feature in _rights.MODEL_FEATURES \
                and not _rights.feature_enabled(self.config, model_feature):
            # Ask design 4.1 (and meaning alerts): with the switch off,
            # refused before any credential (session or bearer) or body is read.
            await self._send(send, *_forbidden("FEATURE_DISABLED",
                                               f"{model_feature} is off for this pilot"))
            return
        refusal = self._personal_token_refusal(method, path, headers)
        if refusal:
            # The one personal-token gate runs first, in every route family
            # (the billing webhook included); it reads no body.
            await self._send(send, *_forbidden("FORBIDDEN", refusal))
            return
        if method == "POST" and path.startswith("/api/v1/billing/") \
                and path.endswith("/webhook"):
            # Before the generic read: billing off and the retired route
            # refuse unread, and the webhook reads its own body at 64 KiB
            # (design 8.4).
            await self._billing_webhook(scope, receive, send, headers, path)
            return
        if _reads_body(method, path):
            # The whole body is read here, before any handler can take the
            # control-store write lock: a slow upload never holds that
            # server-wide lock while it waits. Handlers read the replay.
            try:
                length = int(headers.get("content-length", "0") or "0")
            except ValueError:
                length = 0
            try:
                raw = await self._read_request_body(scope, receive, length)
            except _conn.GrantError as exc:
                await self._send(send, *_json_response(exc.status, {
                    "error": {"code": exc.code, "message": str(exc),
                              "status": exc.status, "details": {}}}))
                return
            except _BodyRefused as exc:
                await self._send(send, *exc.response)
                return
            except _BodyDisconnected:
                return  # nobody is listening; no handler runs
            receive = _replay_body(raw, receive)
        if path == "/.well-known/oauth-protected-resource/mcp" and \
                method == "GET":
            # Off: answer as every agent route does; advertise nothing.
            if not _rights.feature_enabled(self.config, "agent_access"):
                await self._send(send, *_json_response(403, {
                    "error": {"code": "FEATURE_DISABLED",
                              "message": "agent access is off for this pilot",
                              "status": 403, "details": {}}}))
                return
            await self._send(send, *_json_response(200, {
                "resource": self.config.resource_uri,
                "authorization_servers": [self.config.issuer] if
                self.config.issuer else [],
                "scopes_supported": sorted({
                    "research:read", "transcripts:read", "watchlists:read",
                    "campaigns:read", "notes:read"}),
                "bearer_methods_supported": ["header"]}))
            return
        if path == "/api/v1/openapi.json" and method == "GET":
            await self._send(send, *_json_response(200, self.openapi()))
            return
        if path == "/api/v1/connections" and method == "GET":
            await self._connections(send, headers)
            return
        if path.startswith("/api/v1/connections/") and path.endswith("/revoke") \
                and method == "POST":
            await self._revoke(send, headers,
                               path[len("/api/v1/connections/"):-len("/revoke")])
            return
        if path.startswith("/api/v1/connections/") and path.endswith("/audit") \
                and method == "GET":
            await self._audit_stream(scope, receive, send, headers,
                                     path[len("/api/v1/connections/"):-len("/audit")])
            return
        if path == OWNER_REVIEW_PATH or path.startswith("/api/v1/owner/") \
                or path.startswith(OWNER_PAGE_PREFIX):
            await self._owner_review(scope, receive, send, headers, method, path)
            return
        if path == "/api/v1/session" or path.startswith("/api/v1/session/") \
                or path.startswith("/api/v1/customer/") \
                or path.startswith("/api/v1/billing/") \
                or path == "/api/v1/workspaces" \
                or path.startswith("/api/v1/workspaces/") \
                or path == "/api/v1/shares" \
                or path.startswith("/api/v1/shares/") \
                or path.startswith("/api/v1/shared/"):
            query = urllib.parse.parse_qs(
                scope.get("query_string", b"").decode())
            await self._browser_surface(scope, receive, send, headers,
                                        method, path, query)
            return
        for route_method, route_path, tool in AGENT_ROUTES:
            if method != route_method:
                continue
            args = self._match(route_path, path)
            if args is None:
                continue
            await self._agent_tool(scope, receive, send, headers, tool, args)
            return
        if path == MCP_PATH:
            await self._mcp(scope, receive, send, headers)
            return
        if path in CANONICAL_REDIRECTS:
            dest = CANONICAL_REDIRECTS[path]
            try:
                raw_qs = scope.get("query_string", b"").decode("ascii")
            except (UnicodeDecodeError, AttributeError):
                raw_qs = ""
            if raw_qs:
                dest += "?" + raw_qs
            await self._redirect(send, dest)
            return
        if path in PUBLIC_ROUTES:
            await self._public(send, PUBLIC_ROUTES[path])
            return
        if path == "/favicon.ico":
            # No icon art: an empty answer stops the browser's default
            # request from logging a 404 on every page.
            await send({"type": "http.response.start", "status": 204,
                        "headers": [(b"content-security-policy",
                                     STATIC_CSP.encode())]})
            await send({"type": "http.response.body", "body": b""})
            return
        # Exact names only. The server hands us the decoded path, so an
        # encoded form of a listed name (%2F, %70) also matches; nothing
        # unlisted can, and the file opened is always the table's own name.
        if path.startswith("/public/") and path[len("/public/"):] in PUBLIC_ASSETS:
            await self._public(send, path[len("/public/"):])
            return
        if path == "/app/" or path.startswith("/app/"):
            # Validators only for safe methods (RFC 9110 13.1.2: anything
            # else would need 412, not 304).
            await self._static(send, path, headers.get("if-none-match")
                               if method in ("GET", "HEAD") else None)
            return
        await self._public(send, "404.html", status=404)

    @staticmethod
    def _match(route: str, path: str):
        import re
        names = re.findall(r"\{(\w+)\}", route)
        if not names:
            return {} if route == path else None
        pattern = re.sub(r"\{\w+\}", "([^/]+)", route) + "$"
        match = re.match(pattern, path)
        if not match:
            return None
        return {name: urllib.parse.unquote(value)
                for name, value in zip(names, match.groups())}

    async def _send(self, send, status: int, body: bytes, headers=()) -> None:
        payload = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode()),
                   # JSON can carry account data and the CSRF token: no
                   # browser or shared cache may keep it (L8d N2).
                   (b"cache-control", b"private, no-store"),
                   (b"vary", b"Cookie")]
        for name, value in headers:
            payload.append((name.lower().encode(), value.encode()))
        await send({"type": "http.response.start", "status": status,
                    "headers": payload})
        await send({"type": "http.response.body", "body": body})

    def _personal_token_refusal(self, method: str, path: str, headers: dict):
        """The one personal-token gate (AGT-SEC-R5-01), before any routing.
        Returns a refusal message, or None to go on. A bearer that does not
        resolve, or is not a personal grant, goes on unchanged: its own
        route answers 401 or applies its rules. With agent_access off every
        bearer is refused downstream (FEATURE_DISABLED)."""
        if not _rights.feature_enabled(self.config, "agent_access"):
            return None
        token = _bearer(headers)
        if not token:
            return None
        try:
            grant = self.store.resolve_token(token, now=self.config.now())
        except _conn.GrantError:
            return None
        if not (grant.client_id or "").startswith(_conn.PERSONAL_CLIENT_PREFIX):
            return None
        route = personal_token_route(method, path)
        if path == AGENT_TOKENS_PATH or path.startswith(AGENT_TOKENS_PATH + "/"):
            return "agent tokens ride browser sessions"
        if route is None or route[2] is None:
            return "agent tokens cannot use this route"
        for scope_name in route[2]:
            if scope_name not in grant.scopes:
                return f"missing scope: {scope_name}"
        return None

    def _auth(self, headers: dict):
        if not _rights.feature_enabled(self.config, "agent_access"):
            raise _conn.GrantError("FEATURE_DISABLED", "agent access is off for this pilot", 403)
        token = _bearer(headers)
        if not token:
            raise _conn.GrantError("UNAUTHORIZED", "missing token", 401)
        try:
            return self.store.resolve_token(token, now=self.config.now())
        except _conn.UnknownToken:
            verifier = getattr(self, "verifier", None)
            if verifier is None:
                raise
            claims = verifier(token)
            return _conn.resolve_claims(
                self.store, claims, issuer=self.config.issuer,
                audience=self.config.resource_uri, now=self.config.now())

    async def _read_request_body(self, scope: dict, receive,
                                 content_length: int, *,
                                 limit: int = MAX_BODY_BYTES,
                                 too_large: tuple = ("body too large", 413),
                                 oversize: Exception | None = None) -> bytes:
        """Read the body before routing, within the deadline and the
        shared budget (L21). Raises GrantError (413, or `too_large` past
        `limit`), _BodyRefused (408, 429, 503) or _BodyDisconnected; the
        budget share is always given back, and the partial chunks go with
        the frame. A body declared or received past `limit` raises
        `oversize` when given, else the `too_large` GrantError, with no
        further read."""
        too_large = oversize or _conn.GrantError("INVALID_ARGUMENT", *too_large)
        if content_length > limit:
            raise too_large
        retry = (("Retry-After", str(BODY_RETRY_AFTER_S)),
                 ("Connection", "close"))
        budget = self.body_budget
        key = _signin_address((scope.get("client") or ("",))[0])
        refused = budget.open(key)
        if refused == 429:
            raise _BodyRefused(429, "RATE_LIMITED",
                               "too many uploads from this address", retry)
        if refused:
            raise _BodyRefused(503, "UNAVAILABLE", "server busy, retry", retry)
        held = 0
        try:
            if not budget.fits(max(0, content_length)):
                raise _BodyRefused(503, "UNAVAILABLE", "server busy, retry",
                                   retry)
            chunks = []
            received = 0
            async with asyncio.timeout(BODY_READ_TIMEOUT_S):
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        raise _BodyDisconnected()
                    data = message.get("body", b"")
                    received += len(data)
                    if received > limit:
                        raise too_large
                    if not budget.reserve(len(data)):
                        raise _BodyRefused(503, "UNAVAILABLE",
                                           "server busy, retry", retry)
                    held += len(data)  # charged before it is kept
                    chunks.append(data)
                    if not message.get("more_body"):
                        break
            return b"".join(chunks)
        except TimeoutError:
            raise _BodyRefused(408, "REQUEST_TIMEOUT",
                               "request body was not received in time",
                               (("Connection", "close"),)) from None
        finally:
            budget.close(key, held)

    async def _read_body(self, receive, content_length: int) -> bytes:
        if content_length > MAX_BODY_BYTES:
            raise _conn.GrantError("INVALID_ARGUMENT", "body too large", 413)
        chunks = []
        received = 0
        while True:
            message = await receive()
            data = message.get("body", b"")
            received += len(data)
            if received > MAX_BODY_BYTES:
                raise _conn.GrantError("INVALID_ARGUMENT", "body too large",
                                       413)
            chunks.append(data)
            if not message.get("more_body"):
                break
        return b"".join(chunks)

    async def _agent_tool(self, scope: dict, receive, send, headers: dict,
                          tool: str, path_args: dict) -> None:
        import time as _time
        started = _time.monotonic()
        request_id = None
        grant = None
        try:
            try:
                grant = self._auth(headers)
            except _conn.GrantError as exc:
                await self._send(send, *_auth_error(self.config, exc))
                return
            wait = await self.limiter.admit(grant.id, grant.workspace_id,
                                            self.config.now(),
                                            watch=_RECEIVE_WATCH.get())
            if wait > 0:
                await self._send(send, *_json_response(
                    429, {"error": {"code": "RATE_LIMITED",
                                    "message": "slow down and retry",
                                    "status": 429, "details": {}}},
                    (("Retry-After", str(max(1, int(wait + 0.999)))),)))
                return
            try:
                from earwitness import agents as _agents
                spec = next(s for s in _agents.TOOL_DEFS if s["name"] == tool)
                needed = set(spec["scopes"])
                _conn.authorize_request(
                    self.store, grant, required_scopes=needed,
                    workspace_id=grant.workspace_id,
                    policy_rev=grant.policy_rev, now=self.config.now())
                query = urllib.parse.parse_qs(
                    scope.get("query_string", b"").decode())
                if scope["method"] == "GET":
                    body_args = dict(path_args)
                    if "limit" in query:
                        try:
                            body_args["limit"] = int(query["limit"][0])
                        except ValueError:
                            raise _contracts.AgentError(
                                _contracts.INVALID_ARGUMENT, "bad limit")
                    if "cursor" in query:
                        body_args["cursor"] = query["cursor"][0]
                else:
                    length = int(headers.get("content-length", "0") or 0)
                    raw = await self._read_body(receive, length)
                    try:
                        body_args = _parse_json_body(raw) if raw else {}
                    except ValueError:
                        raise _contracts.AgentError(
                            _contracts.INVALID_ARGUMENT, "bad JSON body")
                    if not isinstance(body_args, dict):
                        raise _contracts.AgentError(
                            _contracts.INVALID_ARGUMENT, "bad JSON body")
                    body_args.update(path_args)
                ctx = _conn.build_context(
                    grant, _new_request_id())
                request_id = ctx.request_id
                from . import services
                native_con, entry = services.open_customer_workspace(
                    self.store, grant.workspace_id, self.config.data_dir, ctx, readonly=True)
                ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
                ctx.registry_policy = services.registry_policy(entry)
                ctx.control_store = self.store
                ctx.admitted_role = "owner" if entry["is_owner"] else \
                    services._collaboration.get_role(native_con, entry["row_namespace"], ctx.principal_id)
                try:
                    # Entitlement gate (off unless configured), on the
                    # workspace this request resolved, before any tool runs.
                    services._require_entitlement(native_con, entry, tool)
                finally:
                    native_con.close()
                _contracts.bind_workspace(grant.workspace_id, ctx.db_path)
                budget = _access.ExecutionBudget(deadline_s=10.0)
                try:
                    # Off the event loop, as the MCP route runs its tools
                    # (AGA-R1-04): a slow tool never holds other requests.
                    payload = await asyncio.to_thread(
                        _contracts.AGENT_ENTRYPOINTS[tool], ctx, body_args, budget=budget)
                except _contracts.AgentError as exc:
                    status, code, message = _contracts.error_for_agent(exc)
                    details = {}
                    if code != _contracts.INTERNAL and isinstance(
                            exc.details, dict):
                        keep = exc.details.get("needs_correction")
                        if keep:
                            details = {"needs_correction": list(keep)}
                    await self._send(send, *_json_response(
                        status, {"error": {"code": code, "message": message,
                                           "status": status,
                                           "details": details}}))
                    self._audit(grant, ctx, tool, "error", 0, started)
                    return
                except (TimeoutError, asyncio.CancelledError):
                    raise
                text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if len(text.encode()) > TOOL_RESPONSE_CAP:
                    await self._send(send, *_json_response(
                        200, {"error": {"code": "RESULT_TOO_LARGE",
                                        "message": "response exceeds 64KiB",
                                        "status": 200,
                                        "details": {}}}))
                    self._audit(grant, ctx, tool, "too_large", 0, started)
                    return
                control = self.store._connect()
                try:
                    from . import db
                    with db.transaction(control, write=True):
                        services.fenced_authority_check(
                            self.store, self.config.data_dir, ctx, operation=tool,
                            control=control, now=self.config.now())
                        services.record_success(control, ctx, tool, self.config.now())
                        await self._send(send, *_json_response(200, payload))
                finally:
                    control.close()
            finally:
                await self.limiter.release(grant.id, grant.workspace_id)
        except _access.AuthorizationError as exc:
            await self._send(send, *self._service_error(exc))
        except _conn.GrantError as exc:
            if exc.status == 401:
                await self._send(send, *_unauthorized(self.config))
            elif exc.status == 404:
                await self._send(send, *_not_found())
            else:
                await self._send(send, *_forbidden(exc.code, str(exc)))
        except (asyncio.CancelledError, ClientGone):
            raise
        except Exception as exc:
            self._log_unhandled(exc, scope.get("method", ""), scope.get("path", ""))
            await self._send(send, *_json_response(
                500, {"error": {"code": "INTERNAL",
                                "message": "internal error",
                                "status": 500, "details": {}}}))

    def _audit(self, grant, ctx, tool: str, outcome: str, bytes_out: int,
               started: float) -> None:
        import time as _time
        self.store.audit(
            request_id=ctx.request_id if ctx else _new_request_id(),
            grant_id=grant.id if grant else "",
            principal_id=grant.member_id if grant else "",
            workspace_id=grant.workspace_id if grant else "",
            operation=tool, outcome=outcome, bytes_out=bytes_out,
            latency_ms=(_time.monotonic() - started) * 1000.0,
            now=self.config.now())

    async def _connections(self, send, headers: dict) -> None:
        from . import services, db
        try:
            grant = self._auth(headers)
            ctx = self._status_context(grant)
            control = self.store._connect()
            try:
                with db.transaction(control, write=True):
                    services.fenced_authority_check(self.store, self.config.data_dir, ctx,
                        operation="connections-status", control=control, now=self.config.now())
                    live = self.store._row_to_grant(control.execute(
                        "SELECT * FROM connection_grants WHERE id=?", (grant.id,)).fetchone())
                    response = _json_response(200, {"connections": [{
                        "id": live.id, "client_id": live.client_id, "workspace_id": live.workspace_id,
                        "scopes": sorted(live.scopes),
                        "status": _conn.status_of(self.store, live.id, now=self.config.now()),
                        "last_used_at": live.last_used_at, "expires_at": live.expires_at}]})
                    await self._send(send, *response)
            finally:
                control.close()
        except (_conn.GrantError, _access.AuthorizationError) as exc:
            await self._send(send, *self._service_error(exc))

    def _status_context(self, grant):
        from . import services, collaboration
        ctx = _conn.build_context(grant, _new_request_id())
        con, entry = services.open_customer_workspace(
            self.store, grant.workspace_id, self.config.data_dir, ctx, readonly=True)
        try:
            ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
            ctx.registry_policy = services.registry_policy(entry)
            ctx.admitted_role = "owner" if entry["is_owner"] else collaboration.get_role(
                con, entry["row_namespace"], ctx.principal_id)
        finally:
            con.close()
        return ctx

    async def _revoke(self, send, headers: dict, grant_id: str) -> None:
        try:
            grant = self._auth(headers)
        except _conn.GrantError as exc:
            await self._send(send, *_auth_error(self.config, exc))
            return
        if grant_id != grant.id:
            await self._send(send, *_not_found())
            return
        self.store.revoke_grant(grant_id, "owner revoke", self.config.now())
        await self._send(send, *_json_response(200, {"revoked": grant_id}))

    async def _audit_stream(self, scope: dict, receive, send, headers: dict,
                            grant_id: str) -> None:
        try:
            grant = self._auth(headers)
        except _conn.GrantError as exc:
            await self._send(send, *_auth_error(self.config, exc))
            return
        if grant_id != grant.id:
            await self._send(send, *_not_found())
            return

        from . import services, db
        try:
            ctx = self._status_context(grant)
        except (_conn.GrantError, _access.AuthorizationError) as exc:
            await self._send(send, *self._service_error(exc))
            return

        import sqlite3
        con = sqlite3.connect(self.store.path)
        try:
            rows = con.execute("SELECT request_id, operation, outcome,"
                               " recorded_at FROM agent_audit WHERE grant_id=?"
                               " ORDER BY seq DESC LIMIT 50",
                               (grant_id,)).fetchall()
        finally:
            con.close()
        lines = [json.dumps({"request_id": r[0], "operation": r[1],
                             "outcome": r[2], "at": r[3]}) + "\n"
                 for r in rows]

        async def body_iter():
            for line in lines:
                yield line.encode()

        async def admitted_chunks():
            async for chunk in self._chunked(body_iter()):
                control = self.store._connect()
                try:
                    with db.transaction(control, write=True):
                        services.fenced_authority_check(self.store, self.config.data_dir, ctx,
                            operation="audit-stream", control=control, now=self.config.now())
                        yield chunk
                finally:
                    control.close()
        await self._send_stream(send, admitted_chunks())

    @staticmethod
    async def _chunked(source):
        pending = b""
        async for piece in source:
            pending += piece
            while len(pending) >= CHUNK_BYTES:
                yield pending[:CHUNK_BYTES]
                pending = pending[CHUNK_BYTES:]
        if pending:
            yield pending

    async def _send_stream(self, send, chunks) -> None:
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"private, no-store")]})
        try:
            async for piece in chunks:
                await send({"type": "http.response.body", "body": piece,
                            "more_body": True})
        except (_conn.GrantError, _access.AuthorizationError):
            pass
        await send({"type": "http.response.body", "body": b"",
                    "more_body": False})

    async def _mcp(self, scope: dict, receive, send, headers: dict) -> None:
        try:
            grant = self._auth(headers)
        except _conn.GrantError as exc:
            await self._send(send, *_auth_error(self.config, exc))
            return
        if scope.get("method") != "POST":
            # Stateless JSON mode has no server-to-client stream. The SDK
            # would hold a GET open until the client leaves, and the
            # response is buffered, so it is refused instead.
            await self._send(send, *_json_response(
                405, {"error": {"code": "METHOD_NOT_ALLOWED",
                                "message": "use POST",
                                "status": 405, "details": {}}},
                (("Allow", "POST"),)))
            return
        wait = await self.limiter.admit(grant.id, grant.workspace_id,
                                        self.config.now(),
                                        watch=_RECEIVE_WATCH.get())
        if wait > 0:
            await self._send(send, *_json_response(
                429, {"error": {"code": "RATE_LIMITED",
                                "message": "slow down and retry",
                                "status": 429, "details": {}}},
                (("Retry-After", str(max(1, int(wait + 0.999)))),)))
            return
        try:
            from . import services, db
            ctx = _conn.build_context(grant, _new_request_id())
            con, entry = services.open_customer_workspace(
                self.store, ctx.workspace_id, self.config.data_dir, ctx, readonly=True)
            ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
            ctx.registry_policy = services.registry_policy(entry)
            ctx.control_store = self.store
            ctx.admitted_role = "owner" if entry["is_owner"] else \
                services._collaboration.get_role(con, entry["row_namespace"], ctx.principal_id)
            try:
                # Entitlement gate (off unless configured), before the MCP
                # app can run any tool for this workspace.
                services._require_entitlement(con, entry, "mcp")
            finally:
                con.close()
            messages = []
            async def buffer(message):
                messages.append(message)
            # One endpoint, served at MCP_PATH itself (no mount prefix).
            # The SDK app is built per request, so its session manager is
            # started and stopped around the call; without run() it raises
            # "Task group is not initialized".
            mcp_app = self._mcp_app(ctx)
            # The response is buffered, so no stream can be served. Offer the
            # SDK JSON only: it then answers 406 to any method that needs an
            # event stream (2026-07-28 subscriptions/listen), whatever the
            # method list grows to, instead of holding the call open.
            inner = dict(scope)
            inner["headers"] = [(k, v) for k, v in scope.get("headers", [])
                                if k != b"accept"] + [
                (b"accept", b"application/json")]

            async def serve() -> None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    await mcp_app(inner, receive, buffer)
            try:
                await asyncio.wait_for(serve(), MCP_CALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                await self._send(send, *_json_response(
                    504, {"error": {"code": "TIMEOUT",
                                    "message": "the call took too long",
                                    "status": 504, "details": {}}}))
                return
            control = self.store._connect()
            try:
                with db.transaction(control, write=True):
                    services.fenced_authority_check(
                        self.store, self.config.data_dir, ctx, operation="mcp",
                        control=control, now=self.config.now())
                    for operation in ctx.completed_operations:
                        services.record_success(control, ctx, operation, self.config.now())
                    for message in messages:
                        await send(message)
            finally:
                control.close()
        except _access.AuthorizationError as exc:
            await self._send(send, *self._service_error(exc))
        except _conn.GrantError as exc:
            if exc.status == 401:
                await self._send(send, *_unauthorized(self.config))
            elif exc.status == 404:
                await self._send(send, *_not_found())
            else:
                await self._send(send, *_forbidden(exc.code, str(exc)))
        finally:
            await self.limiter.release(grant.id, grant.workspace_id)

    async def _static(self, send, path: str, if_none_match: str | None = None) -> None:
        import os
        rel = path[len("/app/"):]
        if not rel:
            rel = "index.html"
        safe = os.path.normpath(rel)
        if safe.startswith("..") or safe.startswith("/") or safe.startswith("\\"):
            await self._public(send, "404.html", status=404)
            return
        # Strict allowlist: customer shell files plus the operator tree.
        # Anything else (private paths, unknown names) is the 404 document.
        if safe.startswith("operator/"):
            tail = safe[len("operator/"):]
            if not tail or tail not in APP_STATIC:
                await self._public(send, "404.html", status=404)
                return
            ctype = APP_STATIC[tail]
        elif safe not in APP_STATIC:
            await self._public(send, "404.html", status=404)
            return
        else:
            ctype = APP_STATIC[safe]
        await self._send_file(send, safe, ctype, status=200,
                              if_none_match=if_none_match or "")

    async def _redirect(self, send, dest: str, status: int = 302) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"location", dest.encode()),
                                (b"content-security-policy",
                                 STATIC_CSP.encode()),
                                (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def _public(self, send, name: str, status: int = 200) -> None:
        ctype = PUBLIC_DOCS.get(name, PUBLIC_ASSETS.get(name))
        if ctype is None:
            await self._public(send, "404.html", status=404)
            return
        await self._send_file(send, "public/" + name, ctype, status=status,
                              missing_status=404,
                              render=self._render_pricing if name == PRICING_DOC else None)

    def _render_pricing(self, body: bytes) -> bytes:
        import os
        base = os.path.realpath(self.config.app_dir) if self.config.app_dir else ""
        return _public_pricing.render(body, os.path.join(base, "public"))

    async def _send_file(self, send, rel: str, ctype: str, status: int,
                         missing_status: int | None = None,
                         render=None, if_none_match: str | None = None) -> None:
        """if_none_match is None: no validator (public pages). A string,
        even empty: the answer carries a strong ETag (content hash) and a
        matching If-None-Match gets 304 (BL-051: /app/ files go out with
        Cache-Control no-cache from Caddy, so a repeat visit revalidates
        instead of downloading everything again)."""
        import os
        safe = os.path.normpath(rel)
        if safe.startswith("..") or os.path.isabs(safe):
            await self._send(send, *_not_found())
            return
        base = os.path.realpath(self.config.app_dir) if self.config.app_dir else ""
        full = os.path.join(base, safe) if base else ""
        body: bytes | None = None
        if full and os.path.isfile(full) \
                and os.path.realpath(full).startswith(base + os.path.sep):
            with open(full, "rb") as handle:
                body = handle.read()
        if body is None:
            if missing_status == 404 and rel != "public/404.html":
                await self._public(send, "404.html", status=404)
                return
            await self._send(send, *_not_found())
            return
        if render is not None:
            body = render(body)
        extra = []
        if if_none_match is not None and status == 200:
            import hashlib
            etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            if _etag_matches(if_none_match, etag):
                await send({"type": "http.response.start", "status": 304,
                            "headers": [(b"etag", etag.encode()),
                                        (b"content-security-policy",
                                         STATIC_CSP.encode())]})
                await send({"type": "http.response.body", "body": b""})
                return
            extra = [(b"etag", etag.encode())]
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype.encode()),
                                (b"content-length", str(len(body)).encode()),
                                (b"content-security-policy",
                                 STATIC_CSP.encode())] + extra})
        await send({"type": "http.response.body", "body": body})

    # -- Browser surface: sessions + hosted customer product ---------------
    #
    # Same /api/v1/customer paths as the loopback owner server; the
    # operation's workspace is the authenticated grant's or the session's
    # chosen workspace, resolved through the operator-controlled
    # registry. Both adapters call operation-specific authorized
    # services; the hosted path never proxies into the loopback server.

    @staticmethod
    def _cookies(headers: dict) -> dict:
        out = {}
        for chunk in headers.get("cookie", "").split(";"):
            name, _, value = chunk.partition("=")
            name = name.strip()
            if name:
                out[name] = value.strip()
        return out

    def _browser_config(self):
        override = getattr(self, "browser_config_override", None)
        if override is not None:
            return override
        from earwitness.app import browser_auth as _ba
        return _ba.config_from_env()

    def _local_issuer(self) -> str | None:
        """The local sign-in issuer, or None while local sign-in is off."""
        if not self.config.signin_local:
            return None
        from earwitness.app import local_accounts as _la
        try:
            return _la.local_issuer(self.config.public_base_url)
        except ValueError:
            return None

    def _signin_methods(self, oidc: bool) -> list:
        return (["password"] if self._local_issuer() is not None else []) + \
            (["oidc"] if oidc else [])

    def _signin_state(self, available: bool, reason: str = "") -> tuple:
        # available: the OIDC provider is configured. Local sign-in adds
        # the password method; either makes sign-in available.
        methods = self._signin_methods(available)
        return _json_response(401, {
            "error": {"code": "SIGNIN_REQUIRED",
                      "message": "browser sign-in is required",
                      "status": 401, "details": {}},
            "signin": {"available": bool(methods),
                       "reason": "" if methods else reason,
                       "login_url": "/api/v1/session/login" if available else None,
                       "methods": methods}})

    def _service_error(self, exc) -> tuple:
        from earwitness.app import ops as _ops
        from earwitness.app import search as _search
        from . import citations
        if isinstance(exc, citations.RevisionUnavailable):
            return _json_response(404, {"ok": False, "error": {
                "code": "SOURCE_UNAVAILABLE", "message": "requested source is unavailable",
                "status": 404, "details": {}}})
        if isinstance(exc, citations.CitationError):
            return _json_response(400, {"ok": False, "error": {
                "code": "INVALID_ARGUMENT", "message": str(exc), "status": 400, "details": {}}})
        if isinstance(exc, _conn.GrantError):
            return _auth_error(self.config, exc)
        if isinstance(exc, _access.AuthorizationError):
            error = {"code": "FORBIDDEN", "message": str(exc),
                     "status": exc.status, "details": getattr(exc, "details", None) or {}}
            # DEMO2 pass 15: a refusal because the caller lost access to the
            # workspace says so; role and feature refusals do not.
            if getattr(exc, "access_lost", False):
                error["access"] = "lost"
            # DEMO2 pass 17: a role that changed during the request says so
            # with a stable marker; the message stays as it was.
            if getattr(exc, "role_changed", False):
                error["reason"] = "role_changed"
            # K6 (RA-R3-02): a refused CSRF token, the same marker as the
            # direct answers (_csrf_refused).
            if getattr(exc, "csrf_invalid", False):
                error["reason"] = CSRF_INVALID
            # FREEZE r2: the source content policy's refusal says so with a
            # stable marker; status, code and message stay as they were.
            if getattr(exc, "refusal", None) == "disclosure":
                error["refusal"] = "disclosure"
            return _json_response(exc.status, {"error": error})
        if isinstance(exc, _ops.EpisodeReviewConflict):
            return _json_response(409, {
                "error": {"code": "CONFLICT", "message": str(exc),
                          "status": 409,
                          "details": {"current_revision": exc.current_revision}}})
        if isinstance(exc, _ops.Conflict):
            return _json_response(412, {
                "error": {"code": "CONFLICT", "message": str(exc),
                          "status": 412,
                          "details": {"current_revision": exc.current_revision}}})
        if isinstance(exc, _ops.MissingPrecondition):
            return _json_response(428, {
                "error": {"code": "PRECONDITION_REQUIRED", "message": str(exc),
                          "status": 428, "details": {}}})
        from earwitness.app import customer_contracts as _cc
        from earwitness.app import customer as _customer_mod
        if isinstance(exc, _customer_mod.WindowChanged):
            return _json_response(409, {
                "error": {"code": "VIEW_CHANGED", "message": str(exc),
                          "status": 409, "details": {}}})
        if isinstance(exc, _cc.ExportTooLarge):
            return _json_response(422, {
                "error": {"code": "EXPORT_TOO_LARGE", "message": str(exc),
                          "status": 422, "details": {}}})
        if isinstance(exc, _cc.SelectorConflict):
            return _json_response(409, {
                "error": {"code": "VIEW_CHANGED", "message": str(exc),
                          "status": 409, "details": {}}})
        if isinstance(exc, _cc.SelectorUnavailable):
            return _json_response(404, {
                "error": {"code": "SCOPE_UNAVAILABLE",
                          "message": "saved scope is unavailable",
                          "status": 404, "details": {}}})
        from earwitness.app import exports as _exports
        if isinstance(exc, _exports.DigestMismatch):
            return _json_response(409, {
                "error": {"code": "VIEW_CHANGED", "message": str(exc),
                          "status": 409, "details": {}}})
        if isinstance(exc, _ops.Archived):
            return _json_response(409, {
                "error": {"code": "CAMPAIGN_ARCHIVED", "message": str(exc),
                          "status": 409, "details": {}}})
        if isinstance(exc, _ops.NotFound):
            return _json_response(404, {
                "error": {"code": "NOT_FOUND", "message": str(exc),
                          "status": 404, "details": {}}})
        from earwitness.app import corrections as _corrections
        if isinstance(exc, _corrections.RateLimited):
            return _json_response(429, {
                "error": {"code": "RATE_LIMITED", "message": str(exc),
                          "status": 429, "details": {}}})
        if isinstance(exc, (_ops.BadInput, _search.FilterRejected, ValueError)):
            return _json_response(400, {
                "error": {"code": "INVALID_ARGUMENT", "message": str(exc),
                          "status": 400, "details": {}}})
        self._log_unhandled(exc)
        return _json_response(500, {
            "error": {"code": "INTERNAL", "message": "internal error",
                      "status": 500, "details": {}}})

    def _log_unhandled(self, exc, method: str = "", path: str = "") -> None:
        """One request.error line (type and where, never values) plus the
        error-report hook, for a 500 a handler answers itself. Without
        it a handler that swallows the exception leaves only a
        request.complete 500 line with no cause."""
        fields = {"error_type": type(exc).__name__,
                  "where": _oplog.error_location(exc)}
        if method:
            fields["method"] = method
        if path:
            fields["path"] = path
        _oplog.log_event("request.error", level="error", **fields)
        self.error_reporter.report(exc, method=method, path=path)

    @staticmethod
    def _query_criteria(qs: dict):
        from . import customer_contracts
        if set(qs) - {"query", "group_by"} or any(len(values) != 1 for values in qs.values()):
            raise ValueError("customer research URL carries one Query2 JSON query")
        raw = _parse_json_body((qs.get("query") or ["{}"])[0].encode("utf-8"))
        return customer_contracts.normalize_query(raw)


    @staticmethod
    def _body_query2(body: dict):
        from . import customer_contracts
        return customer_contracts.normalize_query(body.get("query"))

    @staticmethod
    def _deny_tracker_authority_fields(body) -> None:
        # Client verification authority is never accepted on trackers;
        # deny before any write (K08). ValueError maps to 400.
        if not isinstance(body, dict):
            return
        for key in ("verification_context", "verified_via"):
            if key in body:
                raise ValueError(f"unsupported tracker field: {key}")
        delivery = body.get("delivery")
        if isinstance(delivery, dict):
            for key in ("verification_context", "verified_via"):
                if key in delivery:
                    raise ValueError(f"unsupported tracker field: {key}")

    async def _browser_surface(self, scope: dict, receive, send,
                               headers: dict, method: str, path: str,
                               query: dict) -> None:
        from earwitness.app import browser_auth as _ba
        config = self._browser_config()
        if path == "/api/v1/session" and method == "GET":
            await self._session_status(send, headers, config)
            return
        if path == "/api/v1/session/login" and method == "POST":
            await self._session_login(scope, receive, send, headers, config)
            return
        if path == "/api/v1/session/callback" and method == "GET":
            await self._session_callback(scope, send, headers, config, query)
            return
        if path == "/api/v1/session/logout" and method == "POST":
            await self._session_logout(scope, send, headers)
            return
        if path == "/api/v1/session/password" and method == "POST":
            await self._session_password(scope, receive, send, headers)
            return
        if path == "/api/v1/session/password/change" and method == "POST":
            await self._session_password_change(scope, receive, send, headers)
            return
        if path == "/api/v1/session/workspace" and method == "POST":
            await self._session_workspace(scope, receive, send, headers)
            return
        if path == AGENT_TOKENS_PATH or path.startswith(AGENT_TOKENS_PATH + "/"):
            await self._agent_tokens_route(scope, receive, send, headers,
                                           method, path)
            return
        if path.startswith("/api/v1/customer/"):
            await self._customer_route(scope, receive, send, headers,
                                       method, path, query)
            return
        if path.startswith("/api/v1/billing/"):
            # POST .../webhook never reaches here: _dispatch routes it
            # before any body read.
            await self._billing_route(scope, receive, send, headers,
                                      method, path, query)
            return
        if path == "/api/v1/workspaces" or path.startswith("/api/v1/workspaces/") \
                or path == "/api/v1/shares" or path.startswith("/api/v1/shares/") \
                or path.startswith("/api/v1/shared/"):
            await self._collab_route(scope, receive, send, headers,
                                     method, path, query)
            return
        await self._send(send, *_not_found())

    def _session_marker(self, session: dict, workspace_id: str) -> dict:
        """A marker_memo for _session_payload: the workspace's readiness
        marker (customer.session_contract_marker), on connections of its
        own and outside any control transaction. Empty when the workspace
        does not open for this session; the fenced projection then decides
        exactly as before. Runs in a worker thread."""
        from . import browser_auth as _ba
        from . import services, db, customer
        if not workspace_id:
            return {}
        prelim = _ba.build_session_context(session, workspace_id, "viewer", "session-status")
        try:
            con, entry = services.open_customer_workspace(
                self.store, workspace_id, self.config.data_dir, prelim, readonly=True)
        except (_conn.GrantError, _access.AuthorizationError):
            return {}
        try:
            with db.transaction(con, write=False):
                return {"identity": (workspace_id, entry["db_identity"]),
                        "marker": customer.session_contract_marker(con)}
        finally:
            con.close()

    def _session_payload(self, control, session: dict, *, include_ok=False,
                         marker_memo: dict | None = None) -> dict:
        """Canonical session projection under the caller's serialized control fence.
        marker_memo lets a workspace switch run the frontend_contract readiness
        pass (it rescans every saved definition) once: the pre-commit
        projection computes it, so a readiness failure still rolls the switch
        back, and the response fence reuses it for the same workspace database.
        The marker reads only that workspace database; the switch writes only
        the control session row. Every authority check runs on both passes."""
        from . import browser_auth as _ba
        from . import services, db, collaboration, customer
        workspace_id = session.get("active_workspace") or ""
        context_token, marker, ctx, role = None, None, None, None
        if workspace_id:
            prelim = _ba.build_session_context(session, workspace_id, "viewer", "session-status")
            try:
                con, entry = services.open_customer_workspace(
                    self.store, workspace_id, self.config.data_dir, prelim,
                    readonly=True, control=control)
            except (_conn.GrantError, _access.AuthorizationError):
                pass  # A valid no-member session still has account/bootstrap status.
            else:
                try:
                    role = "owner" if entry["is_owner"] else collaboration.get_role(
                        con, entry["row_namespace"], prelim.principal_id)
                    ctx = _ba.build_session_context(
                        session, workspace_id, role, "session-status",
                        scopes=("customer:read",) if role == "viewer"
                        else ("customer:read", "customer:write"))
                    ctx.admitted_role = role
                    ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
                    ctx.registry_policy = services.registry_policy(entry)
                    context_token = services.customer_view_context(ctx, entry)
                    identity = (workspace_id, entry["db_identity"])
                    if marker_memo is not None and marker_memo.get("identity") == identity:
                        marker = marker_memo["marker"]
                    else:
                        with db.transaction(con, write=False):
                            marker = customer.session_contract_marker(con)
                        if marker_memo is not None:
                            marker_memo.update(identity=identity, marker=marker)
                finally:
                    con.close()
        workspace_names = self._authorized_workspace_names(session, control=control)
        workspaces = list(workspace_names)
        fresh = control.execute("SELECT * FROM browser_sessions WHERE session_hash=?",
                                (session["session_hash"],)).fetchone()
        if fresh is None or fresh["revoked_at"] is not None or self.config.now() >= fresh["expires_at"] \
                or any(fresh[key] != session[key] for key in (
                    "issuer", "subject", "active_workspace", "email", "email_verified",
                    "csrf_token", "workspace_switch_revision")):
            raise _access.AccessLost("session state changed")
        if ctx is not None:
            services.fenced_authority_check(self.store, self.config.data_dir, ctx,
                operation="session-status", control=control, now=self.config.now())
        ask_available, ask_paused = self._ask_session(ctx, control)
        payload = {
            "authenticated": True, "principal": _ba.session_principal(session),
            "issuer": session["issuer"], "subject": session["subject"],
            "email_verified": bool(session["email_verified"]), "csrf_token": session["csrf_token"],
            "active_workspace": workspace_id, "workspaces": workspaces,
            "workspace_names": workspace_names, "role": role,
            "workspace_context": context_token, "frontend_contract": marker,
            "workspace_switch_revision": session["workspace_switch_revision"],
            # Pilot switches are one per deployment, the same for every
            # caller: the browser hides what is off (agent access).
            "features": {name: _rights.feature_enabled(self.config, name)
                         for name in _rights.FEATURES},
            # Ask design S8: the switch, the ledger, the key and the
            # workspace's model addendum, read again on every request.
            "ask_available": ask_available,
            # Meaning alerts: the switch, the alerts ledger, the key and the
            # workspace's model addendum, read again on every request.
            "meaning_alerts_available": self._meaning_available(ctx, control),
            # The live brief check: both brief-check switches, the alerts
            # ledger, the key, the workspace's addendum at Ask's level and
            # the tables, read again on every request.
            "brief_check_live_available": self._brief_check_available(ctx, control),
            # How this session signed in: the account page offers Change
            # password only to a local (password) session. Not named
            # "signin": the browser reads that key as the signed-out state.
            "signin_method": "password" if session["issuer"] == self._local_issuer()
            else "oidc"}
        if ask_paused:
            # Lead U3-17: Ask stays in view at the spend stop. Sent only
            # when true, so every other session payload is unchanged.
            payload["ask_paused"] = True
        if payload["signin_method"] == "password" and session["email_verified"] \
                and session["email"]:
            # Sol FIX-1: the account page names a local account by its email,
            # never its derived acct- subject. Provider sessions are unchanged.
            payload["email"] = session["email"]
        if include_ok:
            payload["ok"] = True
        return payload

    async def _session_status(self, send, headers: dict, config, *, include_ok=False) -> None:
        session = self._lookup_browser(headers)
        if session is None:
            await self._send(send, *self._signin_state(
                config.configured,
                "" if config.configured else "browser sign-in is not configured"))
            return
        from . import db
        # The readiness marker can rescan saved definitions (seconds on a
        # corpus after a research change): computed first, off the event
        # loop and outside the control write lock; the fenced projection
        # reuses it. One session runs one of these checks at a time, so a
        # browser cannot queue many on the shared pool (READINESS-CACHE).
        try:
            marker_memo = await self._session_read_marker(session)
        except (_conn.GrantError, _access.AuthorizationError) as exc:
            await self._send(send, *self._service_error(exc))
            return
        control = self.store._connect()
        try:
            with db.transaction(control, write=True):
                payload = self._session_payload(control, session, include_ok=include_ok,
                                                marker_memo=marker_memo)
                await self._send(send, *_json_response(200, payload))
        except (_conn.GrantError, _access.AuthorizationError) as exc:
            await self._send(send, *self._service_error(exc))
        finally:
            control.close()

    async def _session_read_marker(self, session: dict, workspace_id=None) -> dict:
        """_session_marker for workspace_id (default: the session's active
        workspace) on the readiness pool, one at a time per session, for
        session reads and workspace switches alike: further checks of the
        same session wait here, on the event loop, holding no thread. The
        gate is dropped when its last waiter leaves."""
        if workspace_id is None:
            workspace_id = session.get("active_workspace") or ""
        key = session["session_hash"]
        gate = self._session_read_gates.get(key)
        if gate is None:
            gate = self._session_read_gates[key] = [asyncio.Lock(), 0]
        gate[1] += 1
        try:
            async with gate[0]:
                return await self._in_pool(self.session_marker_pool, self._session_marker,
                                           session, workspace_id)
        finally:
            gate[1] -= 1
            if gate[1] == 0:
                self._session_read_gates.pop(key, None)

    def _lookup_browser(self, headers: dict):
        from earwitness.app import browser_auth as _ba
        session_id = self._cookies(headers).get(_ba.SESSION_COOKIE, "")
        if not session_id:
            return None
        con = self.store.auth_connection()
        try:
            return _ba.lookup_session(con, session_id)
        finally:
            con.close()

    def _authorized_workspaces(self, session: dict, *, control=None) -> list:
        return list(self._authorized_workspace_names(session, control=control))

    def _authorized_workspace_names(self, session: dict, *, control=None) -> dict:
        """Authorized workspace IDs (registry order) mapped to display names.
        Membership already opens each registered database; the name is read
        on that same connection, so names cost no extra opens."""
        from earwitness.app import browser_auth as _ba
        from earwitness.app import collaboration as _collab
        from earwitness.app import customer as _customer
        principal = _ba.session_principal(session)
        out = {}
        for workspace_id in self.store.list_registered_workspaces(control=control):
            try:
                con, entry = _conn.open_registered_db(
                    self.store, workspace_id, self.config.data_dir, readonly=True, control=control)
            except _conn.GrantError:
                continue
            try:
                if entry["is_owner"]:
                    if principal == (f"oidc:{entry.get('bound_issuer') or ''}:"
                                     f"{entry.get('bound_subject') or ''}") \
                            and entry.get("bound_issuer"):
                        out[workspace_id] = _customer.workspace_display_name(
                            con, entry["row_namespace"]) or workspace_id
                elif _collab.is_member(con, entry["row_namespace"], principal):
                    out[workspace_id] = _customer.workspace_display_name(
                        con, entry["row_namespace"]) or workspace_id
            finally:
                con.close()
        return out

    @staticmethod
    def _signin_unavailable() -> tuple:
        return _json_response(503, {
            "error": {"code": "UNAVAILABLE",
                      "message": "sign-in provider is unavailable",
                      "status": 503, "details": {}}})

    @staticmethod
    def _signin_limited(wait: float) -> tuple:
        return _json_response(429, {
            "error": {"code": "RATE_LIMITED",
                      "message": "slow down and retry",
                      "status": 429, "details": {}}},
            (("Retry-After", str(max(1, int(wait + 0.999)))),))

    async def _signin_admit(self, scope: dict, send) -> dict | None:
        """Charge one anonymous sign-in attempt to the caller's address.
        Return the attempt record, or None after answering 429."""
        client = (scope or {}).get("client") or ("", 0)
        key = "signin:" + _signin_address(client[0])
        wait = self.signin_limiter.admit_address(key, self.config.now())
        if wait > 0:
            await self._send(send, *self._signin_limited(wait))
            return None
        return {"key": key, "pending": [], "provider": False}

    def _signin_provider(self, attempt: dict) -> float:
        """Take a provider slot for this attempt.
        Return 0 when taken, or seconds to wait. Never awaits."""
        wait = self.signin_limiter.admit_provider(self.config.now())
        if wait <= 0:
            attempt["provider"] = True
        return wait

    async def _signin_release(self, attempt: dict) -> None:
        """Free the attempt's slots only when its provider threads ended:
        a reply cut off by the backstop still holds its slot until its
        own deadline closes the socket."""
        if attempt["pending"]:
            await asyncio.gather(*attempt["pending"], return_exceptions=True)
        if attempt.get("pair") is not None:
            # Password sign-in: the pair reservation completes once, after
            # its hash thread ended, with the attempt's outcome (none when
            # no hash ran, a failure unless a session was committed).
            self.pair_limiter.complete(attempt["pair"], attempt.get("outcome"),
                                       self.config.now())
        if attempt["provider"]:
            self.signin_limiter.release_provider()
        self.signin_limiter.release_address(attempt["key"])

    def _signin_provider_done(self, attempt: dict) -> None:
        """Free the provider slot early, once every provider thread of the
        attempt has ended: the workspace binding after a committed sign-in
        (QA7-R1-05) uses no provider. _signin_release then frees only the
        address slot and completes the pair reservation, as before."""
        if attempt["provider"] and all(task.done() for task in attempt["pending"]):
            self.signin_limiter.release_provider()
            attempt["provider"] = False

    async def _signin_io(self, pending: list, fn, *args):
        """Run blocking provider I/O off the event loop. Each provider
        request ends within PROVIDER_TOTAL_S; the backstop covers the two
        requests an exchange may make plus name resolution."""
        from earwitness.app import browser_auth as _ba
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        pending.append(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task),
                                          2 * _ba.PROVIDER_TOTAL_S + 1.0)
        except asyncio.TimeoutError:
            raise _ba.ProviderError("sign-in provider timed out") from None

    async def _session_login(self, scope: dict, receive, send,
                             headers: dict, config) -> None:
        from earwitness.app import browser_auth as _ba
        if not config.configured:
            await self._send(send, *_json_response(503, {
                "error": {"code": "UNAVAILABLE",
                          "message": "browser sign-in is not configured",
                          "status": 503, "details": {}}}))
            return
        attempt = await self._signin_admit(scope, send)
        if attempt is None:
            return
        try:
            await self._session_login_admitted(receive, send, headers,
                                               config, attempt)
        finally:
            await self._signin_release(attempt)

    async def _session_login_admitted(self, receive, send, headers: dict,
                                      config, attempt: dict) -> None:
        from earwitness.app import browser_auth as _ba
        try:
            length = int(headers.get("content-length", "0") or "0")
            raw = await self._read_body(receive, length)
            body = _parse_json_body(raw) if raw else {}
        except (ValueError, _conn.GrantError) as exc:
            await self._send(send, *self._service_error(exc))
            return
        return_to = str((body or {}).get("return_to") or "/")
        if not _ba.allowed_return(return_to):
            await self._send(send, *self._service_error(
                ValueError("invalid return route")))
            return
        try:
            full = _ba.cached_discovery(config, self.signin_cache)
            if full is None:
                wait = self._signin_provider(attempt)
                if wait > 0:
                    await self._send(send, *self._signin_limited(wait))
                    return
                full = await self._signin_io(attempt["pending"],
                                             _ba.discover, config,
                                             self.signin_cache)
        except ValueError as exc:
            await self._send(send, *self._service_error(exc))
            return
        except _ba.ProviderError:
            await self._send(send, *self._signin_unavailable())
            return
        con = self.store.auth_connection()
        try:
            try:
                started = _ba.start_login(con, full, return_to)
                con.commit()
            except ValueError as exc:
                await self._send(send, *self._service_error(exc))
                return
        finally:
            con.close()
        # 200 with the provider URL: the browser navigates there itself
        # (a real redirect), which keeps the cross-origin authorize hop
        # out of fetch/CORS handling. The provider returns through the
        # callback route below via real navigation. The pre-auth secret
        # binds the transaction to this browser; it never appears in the
        # authorize URL or any response body.
        host = headers.get("host", "")
        tx_cookie = _ba.tx_cookie_value(full, started["browser_proof"],
                                        host=host)
        await self._send(send, *_json_response(
            200, {"authorize_url": started["authorize_url"]},
            (("Set-Cookie", tx_cookie),)))

    async def _session_callback(self, scope: dict, send, headers: dict,
                                config, query: dict) -> None:
        if _browser_navigation(headers):
            # A person's browser lands here from the provider: a failure
            # goes to a plain page, never raw JSON (L25). API callers keep
            # the JSON answers.
            send = _callback_page_send(self, send)
        if not config.configured:
            await self._send(send, *_json_response(503, {
                "error": {"code": "UNAVAILABLE",
                          "message": "browser sign-in is not configured",
                          "status": 503, "details": {}}}))
            return
        attempt = await self._signin_admit(scope, send)
        if attempt is None:
            return
        try:
            await self._session_callback_admitted(send, headers, config,
                                                  query, attempt)
        finally:
            await self._signin_release(attempt)

    async def _session_callback_admitted(self, send, headers: dict, config,
                                         query: dict, attempt: dict) -> None:
        from earwitness.app import browser_auth as _ba
        params = {k: (v or [""])[0] for k, v in query.items()}
        # State, code and the pre-auth cookie are checked against the
        # stored transaction first: a malformed, unknown or replayed
        # callback is answered without any provider request and spends
        # only its address's budget. A refused claim is not committed.
        con = self.store.auth_connection()
        try:
            try:
                claim = _ba.claim_login(
                    con, config, params,
                    browser_proof=self._cookies(headers).get(
                        _ba.TX_COOKIE, ""))
            except ValueError as exc:
                await self._send(send, *self._service_error(exc))
                return
            # No await between the claim and its commit: when every
            # provider slot is busy the claim rolls back, so the browser can
            # retry the same callback.
            wait = self._signin_provider(attempt)
            if wait > 0:
                limited = self._signin_limited(wait)
            else:
                limited = None
                con.commit()
        finally:
            con.close()
        if limited is not None:
            await self._send(send, *limited)
            return
        try:
            full = _ba.cached_discovery(config, self.signin_cache)
            if full is None:
                full = await self._signin_io(attempt["pending"], _ba.discover,
                                             config, self.signin_cache)
            identity = await self._signin_io(attempt["pending"],
                                             _ba.exchange_code, full, claim,
                                             self.signin_cache)
        except ValueError as exc:
            await self._send(send, *self._service_error(exc))
            return
        except _ba.ProviderError:
            await self._send(send, *self._signin_unavailable())
            return
        con = self.store.auth_connection()
        try:
            try:
                finished = _ba.open_session(con, full, identity, claim)
                con.commit()
            except ValueError as exc:
                await self._send(send, *self._service_error(exc))
                return
        finally:
            con.close()
        self._signin_provider_done(attempt)
        await self._signin_bind_one_workspace(finished["session_id"])
        host = headers.get("host", "")
        cookie = _ba.session_cookie_value(full, finished["session_id"],
                                          host=host)
        dest = finished["return_to"] if _ba.allowed_return(
            finished["return_to"]) else "/"
        await self._send(send, *_json_response(
            302, {"return_to": dest},
            (("Location", dest), ("Set-Cookie", cookie),
             ("Set-Cookie", _ba.clear_tx_cookie()))))

    @staticmethod
    def _signin_failed() -> tuple:
        return _json_response(401, {
            "error": {"code": "SIGNIN_FAILED",
                      "message": "email or password is incorrect",
                      "status": 401, "details": {}}})

    @staticmethod
    def _bad_request(message: str) -> tuple:
        return _json_response(400, {
            "error": {"code": "INVALID_ARGUMENT", "message": message,
                      "status": 400, "details": {}}})

    def _password_headers_ok(self, headers: dict) -> bool:
        """Login CSRF (T8): JSON only, so no simple form type gets
        through; a sent Origin must be allowed; a sent Sec-Fetch-Site
        must be same-origin."""
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            return False
        if "origin" in headers and headers["origin"] not in self.config.allowed_origins:
            return False
        if "sec-fetch-site" in headers and headers["sec-fetch-site"] != "same-origin":
            return False
        return True

    async def _password_body(self, scope: dict, receive, headers: dict,
                             send) -> dict | None:
        """Read and parse a password route body. Called after the route's
        cheap refusals and the L20 admission, before any hash slot or pair
        reservation, and before any control-store write. The read is the
        pre-routing reader (deadline, shared budget, disconnect) bounded at
        PASSWORD_BODY_MAX: 400 for a larger body, without reading past it.
        Answers 400 and returns None when it is not one JSON object.
        Messages never echo a field value."""
        try:
            length = int(headers.get("content-length", "0") or "0")
        except ValueError:
            length = 0
        try:
            raw = await self._read_request_body(
                scope, receive, length, limit=PASSWORD_BODY_MAX,
                too_large=("request body is too large", 400))
        except _conn.GrantError as exc:
            await self._send(send, *_json_response(exc.status, {
                "error": {"code": exc.code, "message": str(exc),
                          "status": exc.status, "details": {}}}))
            return None
        except _BodyRefused as exc:
            await self._send(send, *exc.response)
            return None
        except _BodyDisconnected:
            return None  # nobody is listening
        try:
            return _parse_json_body(raw) if raw else {}
        except ValueError:
            await self._send(send, *self._bad_request("request body must be one JSON object"))
            return None

    def _password_lookup(self, issuer: str, key: str, password: str) -> tuple:
        """Worker thread: read the account row by email key, close the
        connection, then run exactly one scrypt-v1 (the row's hash, or the
        dummy record). Returns (matched, subject, the hash read)."""
        from earwitness.app import local_accounts as _la
        con = self.store.auth_connection()
        try:
            row = _la.find_by_key(con, issuer, key)
        finally:
            con.close()
        stored = row["password_hash"] if row is not None else None
        matched = _la.verify(stored, password)
        return bool(matched and row is not None), \
            (row["subject"] if row is not None else None), stored

    async def _session_password(self, scope: dict, receive, send,
                                headers: dict) -> None:
        """POST /api/v1/session/password (PW design 5.1)."""
        issuer = self._local_issuer()
        if issuer is None:
            await self._send(send, *_json_response(503, {
                "error": {"code": "UNAVAILABLE",
                          "message": "browser sign-in is not configured",
                          "status": 503, "details": {}}}))
            return
        if not self._password_headers_ok(headers):
            await self._send(send, *_forbidden("FORBIDDEN", "invalid sign-in request"))
            return
        attempt = await self._signin_admit(scope, send)
        if attempt is None:
            return
        try:
            await self._session_password_admitted(scope, receive, send, headers, issuer, attempt)
        finally:
            await self._signin_release(attempt)

    async def _session_password_admitted(self, scope: dict, receive, send, headers: dict,
                                         issuer: str, attempt: dict) -> None:
        from earwitness.app import browser_auth as _ba
        from earwitness.app import local_accounts as _la
        from earwitness.app import admission as _admission
        from . import db
        body = await self._password_body(scope, receive, headers, send)
        if body is None:
            return
        password = body.get("password")
        raw = _la.password_bytes(password)
        if raw is None or not 1 <= len(raw) <= _la.PRESENTED_MAX_BYTES:
            await self._send(send, *self._bad_request(
                f"password must be 1 to {_la.PRESENTED_MAX_BYTES} bytes"))
            return
        return_to = body.get("return_to") or "/"
        if not isinstance(return_to, str) or not _ba.allowed_return(return_to):
            await self._send(send, *self._bad_request("invalid return route"))
            return
        # The pair key is computed from the email the same way whether or
        # not the account exists (T4).
        key = _la.lookup_key(body.get("email"))
        handle, wait = self.pair_limiter.reserve(
            self.pair_limiter.key(key, attempt["key"]), self.config.now())
        if handle is None:
            await self._send(send, *self._signin_limited(wait))
            return
        attempt["pair"], attempt["outcome"] = handle, None
        wait = self._signin_provider(attempt)
        if wait > 0:
            await self._send(send, *self._signin_limited(wait))
            return
        attempt["outcome"] = False
        try:
            matched, subject, verified = await self._signin_io(
                attempt["pending"], self._password_lookup, issuer, key, password)
        except _ba.ProviderError:
            await self._send(send, *self._signin_unavailable())
            return
        finished = None
        if matched:
            # Commit fence (5.4): the row must still hold the hash that was
            # verified, admission is decided again, and the cookie's old
            # session is revoked with the new one inserted, in one write.
            old = self._cookies(headers).get(_ba.SESSION_COOKIE, "")
            con = self.store.auth_connection()
            try:
                with db.transaction(con, write=True):
                    row = con.execute("SELECT email, password_hash FROM local_accounts"
                                      " WHERE issuer=? AND subject=?",
                                      (issuer, subject)).fetchone()
                    if row is not None and row["password_hash"] == verified:
                        now = time.time()
                        if old:
                            con.execute("UPDATE browser_sessions SET revoked_at=?"
                                        " WHERE session_hash=? AND revoked_at IS NULL",
                                        (now, _ba._hash_presented(old)))
                        finished = _ba.open_issuer_session(
                            con, issuer, {"subject": subject, "email": row["email"],
                                          "email_verified": True},
                            {"ts": now, "return_to": return_to})
            except _admission.AdmissionDenied as exc:
                await self._send(send, *self._service_error(exc))
                return
            finally:
                con.close()
        if finished is None:
            _oplog.log_event("signin.password_failed", address=attempt["key"])
            await self._send(send, *self._signin_failed())
            return
        attempt["outcome"] = True
        self._signin_provider_done(attempt)
        await self._signin_bind_one_workspace(finished["session_id"])
        cookie = _ba.session_cookie_value(self._browser_config(), finished["session_id"],
                                          host=headers.get("host", ""))
        await self._send(send, *_json_response(
            200, {"return_to": return_to}, (("Set-Cookie", cookie),)))

    def _password_change_work(self, issuer: str, subject: str, current: str,
                              new: str) -> tuple:
        """Worker thread, one sign-in slot for both hashes: verify the
        current password (one scrypt-v1), then hash the new one only when
        it matched. Returns (matched, the hash read, the new hash)."""
        from earwitness.app import local_accounts as _la
        con = self.store.auth_connection()
        try:
            row = con.execute("SELECT password_hash FROM local_accounts WHERE issuer=?"
                              " AND subject=?", (issuer, subject)).fetchone()
        finally:
            con.close()
        stored = row["password_hash"] if row is not None else None
        if not (_la.verify(stored, current) and row is not None):
            return False, stored, None
        return True, stored, _la.hash_password(new)

    async def _session_password_change(self, scope: dict, receive, send,
                                       headers: dict) -> None:
        """POST /api/v1/session/password/change (PW design 5.3)."""
        issuer = self._local_issuer()
        if issuer is None:
            await self._send(send, *_json_response(503, {
                "error": {"code": "UNAVAILABLE",
                          "message": "browser sign-in is not configured",
                          "status": 503, "details": {}}}))
            return
        session = self._lookup_browser(headers)
        if session is None:
            await self._send(send, *self._signin_state(self._browser_config().configured))
            return
        if not self._session_csrf_ok(scope, headers, session):
            await self._send(send, *_csrf_refused())
            return
        if session["issuer"] != issuer:
            await self._send(send, *_forbidden(
                "FORBIDDEN", "this account does not sign in with a password"))
            return
        attempt = await self._signin_admit(scope, send)
        if attempt is None:
            return
        try:
            await self._session_password_change_admitted(
                scope, receive, send, headers, issuer, session, attempt)
        finally:
            await self._signin_release(attempt)

    async def _session_password_change_admitted(self, scope: dict, receive, send, headers: dict,
                                                issuer: str, session: dict,
                                                attempt: dict) -> None:
        from earwitness.app import browser_auth as _ba
        from earwitness.app import local_accounts as _la
        from earwitness.app import admission as _admission
        from . import db
        body = await self._password_body(scope, receive, headers, send)
        if body is None:
            return
        current, new = body.get("current_password"), body.get("new_password")
        raw = _la.password_bytes(current)
        if raw is None or not 1 <= len(raw) <= _la.PRESENTED_MAX_BYTES:
            await self._send(send, *self._bad_request(
                f"current password must be 1 to {_la.PRESENTED_MAX_BYTES} bytes"))
            return
        key = _la.lookup_key(session["email"])
        problem = _la.new_password_problem(new, current=current, email_key=key)
        if problem is not None:
            await self._send(send, *self._bad_request(problem))
            return
        handle, wait = self.pair_limiter.reserve(
            self.pair_limiter.key(key, attempt["key"]), self.config.now())
        if handle is None:
            await self._send(send, *self._signin_limited(wait))
            return
        attempt["pair"], attempt["outcome"] = handle, None
        wait = self._signin_provider(attempt)
        if wait > 0:
            await self._send(send, *self._signin_limited(wait))
            return
        attempt["outcome"] = False
        try:
            matched, verified, new_hash = await self._signin_io(
                attempt["pending"], self._password_change_work, issuer,
                session["subject"], current, new)
        except _ba.ProviderError:
            await self._send(send, *self._signin_unavailable())
            return
        if not matched:
            _oplog.log_event("signin.password_change_failed", address=attempt["key"])
            await self._send(send, *_json_response(401, {
                "error": {"code": "PASSWORD_INCORRECT",
                          "message": "current password is incorrect",
                          "status": 401, "details": {}}}))
            return
        # Commit fence (5.4): the initiating session is still live with the
        # same token, the account is admitted, and the row still holds the
        # verified hash. The hash is replaced only from that value, and
        # every other session of the account is revoked in the same write.
        session_id = self._cookies(headers).get(_ba.SESSION_COOKIE, "")
        changed = False
        con = self.store.auth_connection()
        try:
            with db.transaction(con, write=True):
                _admission.require_admitted(issuer, session["subject"], session["email"], True,
                                            seats=True)
                now = time.time()
                fresh = _ba.lookup_session(con, session_id, now=now)
                # The csrf_token and session_hash equalities are kept although
                # equivalent today (cookie-keyed lookup, no CSRF rotation; Astra PWB-R1-03).
                if fresh is not None and all(fresh[k] == session[k] for k in (
                        "issuer", "subject", "csrf_token", "session_hash")):
                    done = con.execute(
                        "UPDATE local_accounts SET password_hash=?, password_set_at=?"
                        " WHERE issuer=? AND subject=? AND password_hash=?",
                        (new_hash, now, issuer, session["subject"], verified))
                    if done.rowcount == 1:
                        con.execute("UPDATE browser_sessions SET revoked_at=? WHERE issuer=?"
                                    " AND subject=? AND session_hash<>? AND revoked_at IS NULL",
                                    (now, issuer, session["subject"], session["session_hash"]))
                        changed = True
        except _admission.AdmissionDenied as exc:
            attempt["outcome"] = None
            await self._send(send, *self._service_error(exc))
            return
        finally:
            con.close()
        if not changed:
            attempt["outcome"] = None
            await self._send(send, *_json_response(409, {
                "error": {"code": "PASSWORD_CHANGED",
                          "message": "Your password was changed elsewhere. Sign in again.",
                          "status": 409, "details": {}}}))
            return
        attempt["outcome"] = True
        await self._send(send, *_json_response(200, {"ok": True}))

    def _session_csrf_ok(self, scope: dict, headers: dict, session: dict) -> bool:
        """Same-origin token gate for browser-session mutations."""
        from earwitness.app import browser_auth as _ba
        return bool(_ba.validate_csrf(
            session, headers.get(_ba.CSRF_HEADER, ""),
            origin=headers.get("origin", ""),
            host=headers.get("host", ""),
            scheme=(scope or {}).get("scheme", "http")))

    async def _session_logout(self, scope: dict, send, headers: dict) -> None:
        from earwitness.app import browser_auth as _ba
        session = self._lookup_browser(headers)
        if session is not None and not self._session_csrf_ok(scope, headers, session):
            await self._send(send, *_csrf_refused())
            return
        session_id = self._cookies(headers).get(_ba.SESSION_COOKIE, "")
        if session_id:
            con = self.store.auth_connection()
            try:
                _ba.revoke_session(con, session_id)
                con.commit()
            finally:
                con.close()
        await self._send(send, *_json_response(
            200, {"ok": True}, (("Set-Cookie", _ba.clear_session_cookie()),)))

    def _bind_session_workspace(self, control, session_id: str, session: dict,
                                workspace_id: str, revision: int, marker_memo: dict,
                                *, csrf_ok=None) -> dict:
        """The one session-to-workspace binding, for the workspace switch
        POST and the one-workspace sign-in (QA7-ONEWS). In its own control
        write transaction: the session must be unchanged and live, csrf_ok
        (the switch's request token check) must pass when given, the
        workspace must be one this account may switch into, one switch
        epoch is consumed, and the pre-commit projection runs (any failure,
        readiness included, rolls the binding back). Returns the committed
        session."""
        from earwitness.app import browser_auth as _ba
        from . import db
        with db.transaction(control, write=True):
            fresh = _ba.lookup_session(control, session_id, now=self.config.now())
            if fresh is None or any(fresh[key] != session[key] for key in (
                    "issuer", "subject", "email", "email_verified", "csrf_token")):
                raise _access.AccessLost("session state changed")
            if csrf_ok is not None and not csrf_ok(fresh):
                raise _csrf_error()
            if workspace_id not in self._authorized_workspaces(fresh, control=control):
                raise _access.AccessLost("workspace is not available to this account")
            _ba.set_active_workspace(control, session_id, workspace_id, revision,
                                     now=self.config.now())
            committed = _ba.lookup_session(control, session_id, now=self.config.now())
            if committed is None:
                raise _access.AccessLost("session unavailable")
            # Pre-commit projection: any failure, readiness included, rolls
            # the binding back. Its marker is reused by the response fence.
            self._session_payload(control, committed, include_ok=True,
                                  marker_memo=marker_memo)
        return committed

    async def _signin_bind_one_workspace(self, session_id: str) -> None:
        """QA7-ONEWS: a new session whose account has exactly one admitted
        workspace starts in it, for password and OIDC sign-in alike.
        Admitted means a workspace the switch would accept (owner binding
        or a live membership; a revoked membership or a pending invitation
        is none) that the admission file does not retire. Zero or several
        leave the session unbound (the chooser), as before. The binding is
        the switch's own (_bind_session_workspace). Any failure while
        choosing or binding (the lookup, the count, the binding) leaves
        sign-in succeeding with the session unbound (QA7-R1-01)."""
        try:
            await self._signin_choose_and_bind(session_id)
        except Exception as exc:  # the chooser, as before; sign-in itself stands
            _oplog.log_event("signin.workspace_unbound", level="warning",
                             reason=type(exc).__name__)

    async def _signin_choose_and_bind(self, session_id: str) -> None:
        from earwitness.app import browser_auth as _ba
        from earwitness.app import admission as _admission
        from . import customer
        con = self.store.auth_connection()
        try:
            session = _ba.lookup_session(con, session_id, now=self.config.now())
        finally:
            con.close()
        if session is None or session.get("active_workspace"):
            return
        workspaces = self._authorized_workspaces(session)
        # One admission read per sign-in (QA7-R1-02): an unreadable file
        # binds nothing rather than retiring some workspaces and not others.
        retired = _admission.retired_workspace_ids()
        if retired is None:
            return
        admitted = [w for w in workspaces if w not in retired]
        if len(admitted) != 1:
            return
        workspace_id = admitted[0]
        control = self.store.auth_connection()
        try:
            marker_memo = await self._session_read_marker(session, workspace_id)
            # A workspace the app cannot open yet (the frontend_contract
            # readiness the browser gate checks) stays unbound, so the
            # person gets the chooser with Change password (QA7-R1-03).
            if marker_memo.get("marker") != customer.SESSION_CONTRACT:
                return
            self._bind_session_workspace(control, session_id, session, workspace_id,
                                         session["workspace_switch_revision"], marker_memo)
        finally:
            control.close()

    async def _session_workspace(self, scope: dict, receive, send,
                                 headers: dict) -> None:
        from earwitness.app import browser_auth as _ba
        session = self._lookup_browser(headers)
        if session is None:
            await self._send(send, *self._signin_state(
                self._browser_config().configured))
            return
        if not self._session_csrf_ok(scope, headers, session):
            await self._send(send, *_csrf_refused())
            return
        try:
            length = int(headers.get("content-length", "0") or "0")
            raw = await self._read_body(receive, length)
            body = _parse_json_body(raw) if raw else {}
            if set(body) - {"workspace_id", "expected_workspace_switch_revision"}:
                raise ValueError("workspace switch holds workspace_id/expected_workspace_switch_revision only")
            if "expected_workspace_switch_revision" not in body:
                await self._send(send, *_json_response(428, {
                    "ok": False, "error": {"code": "PRECONDITION_REQUIRED",
                    "message": "expected_workspace_switch_revision is required",
                    "status": 428, "details": {}}}))
                return
            revision = body["expected_workspace_switch_revision"]
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise ValueError("expected_workspace_switch_revision must be a nonnegative integer")
        except (ValueError, _conn.GrantError) as exc:
            await self._send(send, *self._service_error(exc))
            return
        workspace_id = str(body.get("workspace_id") or "")
        from . import db
        control = self.store.auth_connection()
        try:
            # The target workspace's readiness marker, off the loop and
            # outside the control lock (see _session_status); a readiness
            # failure here still refuses the switch before anything is written.
            marker_memo = await self._session_read_marker(session, workspace_id)
            session_id = self._cookies(headers).get(_ba.SESSION_COOKIE, "")
            committed = self._bind_session_workspace(
                control, session_id, session, workspace_id, revision, marker_memo,
                csrf_ok=lambda fresh: self._session_csrf_ok(scope, headers, fresh))
            # The write committed before bytes are released. Reuse this same
            # connection for the final response fence; an intervening switch
            # or authority change fails rather than acknowledging stale state.
            with db.transaction(control, write=True):
                if workspace_id not in self._authorized_workspaces(committed, control=control):
                    raise _access.AccessLost("workspace is not available to this account")
                payload = self._session_payload(control, committed, include_ok=True,
                                                marker_memo=marker_memo)
                await self._send(send, *_json_response(200, payload))
        except _ba.WorkspaceSwitchConflict as exc:
            await self._send(send, *_json_response(409, {
                "ok": False, "workspace_switch_revision": exc.current_revision,
                "error": {"code": "WORKSPACE_SWITCH_CONFLICT",
                          "message": str(exc), "status": 409,
                          "details": {"workspace_switch_revision": exc.current_revision}}}))
        except (_conn.GrantError, _access.AuthorizationError) as exc:
            await self._send(send, *self._service_error(exc))
        finally:
            control.close()

    async def _with_workspace(self, scope: dict, receive, send,
                              headers: dict, method: str, handler,
                              guest_share_id: str | None = None,
                              invite_accept_workspace: str | None = None,
                              guest_workspace: str | None = None,
                              pass_control: bool = False,
                              audio_volume: bool = False,
                              offload=None, offload_pool=None
                              ) -> None:
        """Authenticate (bearer grant or browser session), resolve the
        operation workspace through the registry, open and authorize it,
        then run handler(ctx, con, entry, via_session, session).

        Member mode enforces workspace membership/binding. Guest mode
        (share_id given) requires a session and resolves exact
        share-guest authority instead; forged or foreign shares fail
        closed. Invite-accept mode (workspace id given) admits an
        authenticated browser session without membership: the accept
        service binds the exact pending invitation to the principal.
        Session mutations require a same-origin CSRF token.

        pass_control=True also hands the handler this request's control
        connection as control=; on a mutation it is inside the control
        write transaction, which commits only after the final fence and
        record_success, before any buffered reply is sent.

        offload(ctx, entry), on a workspace-only write, runs in a worker
        thread of offload_pool (default: the export pool) after the first
        authority check and before the workspace write transaction, on its
        own read-only connection; the handler reads its result. It holds no
        lock and leaves the event loop free.

        audio_volume=True (the media route, audio clips design 4.7): a
        bearer credential is refused before the volume guard or any
        workspace is opened; a browser session meets the guard before any
        workspace is opened; and the guard runs again at the release of the
        buffered response, after the authority fence. A failed guard is the
        route's "unavailable" refusal and no audio byte."""
        from earwitness.app import browser_auth as _ba
        from earwitness.app import media as _media
        from earwitness.app import collaboration as _collab
        from earwitness.app import ops as _ops
        from earwitness.app import search as _search
        from earwitness.app import services as _services
        from . import citations, corrections, customer_contracts, customer, exports
        if guest_share_id and _bearer(headers):
            await self._send(send, *_json_response(403, {
                "error": {"code": "FORBIDDEN",
                          "message": "share reads ride browser sessions",
                          "status": 403, "details": {}}}))
            return
        request_id = _new_request_id()
        session = None
        via_session = False
        token = _bearer(headers)
        # One wait bound for every limiter stage of this request.
        watch = _RECEIVE_WATCH.get()
        deadline = asyncio.get_running_loop().time() + self.limiter.wait_s
        if token:
            try:
                grant = self._auth(headers)
            except _conn.GrantError as exc:
                await self._send(send, *_auth_error(self.config, exc))
                return
            throttle_id = grant.id
            wait = await self.limiter.admit(throttle_id, grant.workspace_id,
                                            self.config.now(), watch=watch,
                                            deadline=deadline)
            if wait > 0:
                await self._send(send, *_json_response(429, {
                    "error": {"code": "RATE_LIMITED",
                              "message": "slow down and retry",
                              "status": 429, "details": {}}},
                    (("Retry-After", str(max(1, int(wait + 0.999)))),)))
                return
            try:
                ctx = _conn.build_context(grant, request_id)
                if audio_volume and _media.audio_gated(ctx):
                    ctx.require_audio_credential()   # before the guard or the workspace
                readonly = method not in ("POST", "PUT", "DELETE") or scope.get("path", "") == RESEARCH_QUERY_PATH
                workspace_id = grant.workspace_id
                con, entry = _services.open_customer_workspace(
                    self.store, workspace_id, self.config.data_dir, ctx, readonly=readonly)
            except (_conn.GrantError, _access.AuthorizationError) as exc:
                await self.limiter.release(throttle_id, grant.workspace_id)
                await self._send(send, *self._service_error(exc))
                return
        else:
            session = self._lookup_browser(headers)
            if session is None:
                await self._send(send, *self._signin_state(
                    self._browser_config().configured,
                    "" if self._browser_config().configured
                    else "browser sign-in is not configured"))
                return
            via_session = True
            # CSRF and Origin come first, before any workspace is looked
            # up or opened: a failed check must answer the same for a real
            # and a made-up workspace.
            if method in ("POST", "PUT", "DELETE") and not _ba.validate_csrf(
                    session, headers.get(_ba.CSRF_HEADER, ""),
                    origin=headers.get("origin", ""),
                    host=headers.get("host", ""),
                    scheme=(scope or {}).get("scheme", "http")):
                await self._send(send, *_csrf_refused())
                return
            workspace_id = session.get("active_workspace") or ""
            if guest_share_id:
                workspace_id = guest_workspace or workspace_id
            if invite_accept_workspace:
                # Operation-specific admission: the invited subject may
                # hold no membership yet, so the workspace comes from
                # the accept path itself, never session state.
                workspace_id = invite_accept_workspace
            if not workspace_id:
                await self._send(send, *_json_response(409, {
                    "error": {"code": "WORKSPACE_REQUIRED",
                              "message": "choose a workspace first",
                              "status": 409, "details": {}},
                    "workspaces": self._authorized_workspaces(session)}))
                return
            if audio_volume and not guest_share_id \
                    and _media.import_volume_code() is not None:
                # The expected volume before any workspace is opened. A
                # share guest is refused audio by its credential instead.
                await self._send(send, *self._service_error(
                    citations.RevisionUnavailable("recording is unavailable")))
                return
            # One throttle identity for admit and release: without the
            # shared key the admit bucket would never drain and every
            # later request from this browser would 429.
            throttle_id = ("session:" + _ba._hash_presented(
                self._cookies(headers).get(_ba.SESSION_COOKIE, "")))
            # The workspace named here is not yet shown to be the
            # caller's: a guest-share or invite path takes it from the
            # request, and a session's active workspace may have been
            # revoked. Charging it now would let any account spend a
            # victim's budget (and grow one bucket per made-up id). Until
            # authorization passes, the caller is charged to a bucket of
            # its own; the workspace is charged afterwards (admit_workspace).
            limit_key = "caller:" + throttle_id
            # A clip's audio spends the session's media budget (AUDIO-429).
            wait = await self.limiter.admit(
                throttle_id, limit_key, self.config.now(), session=True,
                media=audio_volume, watch=watch, deadline=deadline)
            if wait > 0:
                await self._send(send, *_json_response(429, {
                    "error": {"code": "RATE_LIMITED",
                              "message": "slow down and retry",
                              "status": 429, "details": {}}},
                    (("Retry-After", str(max(1, int(wait + 0.999)))),)))
                return
            if guest_share_id:
                try:
                    con, entry, ctx = _services.open_shared_workspace(
                        self.store, workspace_id, self.config.data_dir,
                        session, guest_share_id)
                except (_conn.GrantError, _access.AuthorizationError,
                        _ops.BadInput, ValueError) as exc:
                    await self.limiter.release(throttle_id, limit_key)
                    await self._send(send, *self._service_error(exc))
                    return
                except (RuntimeError, sqlite3.Error):
                    # E.g. a workspace whose collaboration tables are not
                    # prepared: answer as an unknown object, free the slot.
                    await self.limiter.release(throttle_id, limit_key)
                    await self._send(send, *_not_found())
                    return
            else:
                prelim = _ba.build_session_context(session, workspace_id,
                                                   "viewer", request_id)
                con = None
                try:
                    if invite_accept_workspace:
                        con, entry = _conn.open_registered_db(
                            self.store, workspace_id, self.config.data_dir,
                            readonly=method not in ("POST", "PUT", "DELETE"))
                    else:
                        con, entry = _services.open_customer_workspace(
                            self.store, workspace_id, self.config.data_dir, prelim,
                            readonly=method not in ("POST", "PUT", "DELETE") or scope.get("path", "") == RESEARCH_QUERY_PATH)
                    role = _collab.get_role(con, entry["row_namespace"],
                                            _ba.session_principal(session))
                    if role is None and entry["is_owner"]:
                        role = "owner"
                except (_conn.GrantError, _access.AuthorizationError) as exc:
                    if con is not None:
                        con.close()
                    await self.limiter.release(throttle_id, limit_key)
                    await self._send(send, *self._service_error(exc))
                    return
                except (RuntimeError, sqlite3.Error):
                    if con is not None:
                        con.close()
                    await self.limiter.release(throttle_id, limit_key)
                    await self._send(send, *(
                        _not_found() if invite_accept_workspace else
                        _json_response(500, {"error": {
                            "code": "INTERNAL", "message": "internal error",
                            "status": 500, "details": {}}})))
                    return
                if role is None and invite_accept_workspace is None:
                    con.close()
                    await self.limiter.release(throttle_id, limit_key)
                    await self._send(send, *_json_response(403, {
                        "error": {"code": "FORBIDDEN",
                                  "message": "workspace membership required",
                                  "access": "lost",
                                  "status": 403, "details": {}}}))
                    return
                if role is None:
                    role = "viewer"
                ctx = _ba.build_session_context(
                    session, workspace_id, role, request_id,
                    scopes=("customer:read",) if role == "viewer"
                    else ("customer:read", "customer:write"))
                if invite_accept_workspace:
                    ctx.authority_kind = "bootstrap"
                    ctx.scopes = frozenset({"invite:accept"})
        release_key = ((grant.id, workspace_id) if token
                       else (throttle_id, limit_key))
        ctx.control_store = self.store
        charged_workspace = None
        media_turn = False
        if via_session and not invite_accept_workspace:
            # Authorized by now (member role known, or share audience
            # verified): charge the workspace's own budget and gate.
            try:
                # A clip read waits for a media turn before its slot
                # (PFC-R1-04); the turn is given back with the slot.
                wait = await self.limiter.admit_workspace(
                    workspace_id, self.config.now(), media=audio_volume,
                    watch=watch, deadline=deadline,
                    media_turns=self.media_threads if audio_volume else 0)
            except BaseException:  # ClientGone or cancelled: free what it holds
                con.close()
                await self.limiter.release(*release_key)
                raise
            if wait > 0:
                # Retry-After after any charge: admit spent the session's
                # token, so the obeying retry must find one there too.
                wait = max(wait, self.limiter.grant_wait(
                    throttle_id, self.config.now(), session=True,
                    media=audio_volume))
                con.close()
                await self.limiter.release(*release_key)
                await self._send(send, *_json_response(429, {
                    "error": {"code": "RATE_LIMITED",
                              "message": "slow down and retry",
                              "status": 429, "details": {}}},
                    (("Retry-After", str(max(1, int(wait + 0.999)))),)))
                return
            charged_workspace = workspace_id
            media_turn = audio_volume
        try:
            # Inside the guarded block: a failure here still closes con and
            # frees the caller and workspace slots.
            ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
            ctx.registry_policy = _services.registry_policy(entry)
            ctx.admitted_role = None if invite_accept_workspace or guest_share_id else \
                ("owner" if entry["is_owner"] else _collab.get_role(
                    con, entry["row_namespace"], ctx.principal_id))
            from . import db
            messages = []
            async def buffered_send(message):
                messages.append(message)

            async def release():
                # The guard again at the release, after the authority work
                # and before the first buffered message (CLIP4-SEC-R1-01).
                if audio_volume and _media.import_volume_code() is not None:
                    await self._send(send, *self._service_error(
                        citations.RevisionUnavailable("recording is unavailable")))
                    return
                for message in messages:
                    await send(message)
            operation = scope.get("path", "")
            mutation = method in ("POST", "PUT", "DELETE") and \
                operation != RESEARCH_QUERY_PATH
            outward = mutation and bool(OUTWARD_SEND_PATH.fullmatch(operation)
                                        or ASK_OWN_WRITE_PATH.fullmatch(operation)
                                        or MEANING_OWN_WRITE_PATH.fullmatch(operation)
                                        or BRIEF_CHECK_OWN_WRITE_PATH.fullmatch(operation))
            control = self.store._connect()
            extra = {"control": control} if pass_control else {}
            try:
                if mutation:
                    if ctx.is_reader_credential:
                        raise _access.AuthorizationError("reader credentials are read-only", 403)
                    def admit():
                        """The current authority and switch revision; the
                        caller holds the control write transaction."""
                        current_entry = _services._current_authority(control, ctx, now=self.config.now())
                        current_session = current_entry.get("_session", session)
                        if via_session and not invite_accept_workspace and not guest_share_id:
                            if sum(key.lower() == b"x-earwitness-workspace-switch-revision"
                                   for key, _ in scope.get("headers", [])) > 1:
                                raise ValueError("workspace switch revision header must occur exactly once")
                            revision_header = headers.get("x-earwitness-workspace-switch-revision")
                            if revision_header is None:
                                raise _ops.MissingPrecondition("workspace switch revision header is required")
                            if not re.fullmatch(r"0|[1-9][0-9]{0,17}", revision_header):
                                raise ValueError("workspace switch revision header must be a canonical nonnegative decimal integer")
                            if int(revision_header) != current_session["workspace_switch_revision"] or \
                                    (current_session.get("active_workspace") or "") != workspace_id:
                                raise _ba.WorkspaceSwitchConflict(current_session["workspace_switch_revision"])
                        return current_session
                    # A handler that is not handed the control connection
                    # writes only its workspace: the control write lock is
                    # held for the authority checks, released for the
                    # handler (its reads may be long: a research export ran
                    # 26 min, retime 130b8e80), and taken again for the
                    # final checks, so sign-ins never wait on a handler.
                    # Lock order on this path: the workspace, then control
                    # (the long path takes control, then the workspace); the
                    # control wait is bounded by its busy timeout, after
                    # which the write rolls back (request-limits.md).
                    short_lock = not pass_control and operation not in EXTERNAL_CALL_PATHS \
                        and not outward
                    with db.transaction(control, write=True):
                        current_session = admit()
                        if pass_control and operation not in EXTERNAL_CALL_PATHS and not outward:
                            with db.transaction(con, write=True):
                                _services._workspace_authority(
                                    con, ctx, entry, bootstrap=bool(invite_accept_workspace))
                                await handler(ctx, con, entry, via_session, current_session, buffered_send, **extra)
                                if any(m.get("status", 200) >= 400 for m in messages):
                                    raise _AbortBuffered(messages)
                                _services.fenced_authority_check(
                                    self.store, self.config.data_dir, ctx, operation=operation,
                                    control=control, now=self.config.now(),
                                    bootstrap=bool(invite_accept_workspace))
                                for source_key, field, exporting in ctx.disclosures:
                                    ctx.authorize_disclosure(con, source_key, [field], export=exporting)
                                _services.record_success(control, ctx, operation, self.config.now())
                    if short_lock:
                        if offload is not None:
                            await self._in_pool(offload_pool or self.export_pool,
                                                offload, ctx, entry)
                        # The workspace write commits only after the final
                        # checks (authority and switch revision again, the
                        # fence, the success row) pass under the control lock,
                        # and before control commits, as on the long path.
                        with db.transaction(con, write=True):
                            _services._workspace_authority(
                                con, ctx, entry, bootstrap=bool(invite_accept_workspace))
                            await handler(ctx, con, entry, via_session, current_session, buffered_send, **extra)
                            if any(m.get("status", 200) >= 400 for m in messages):
                                raise _AbortBuffered(messages)
                            with db.transaction(control, write=True):
                                admit()
                                _services.fenced_authority_check(
                                    self.store, self.config.data_dir, ctx, operation=operation,
                                    control=control, now=self.config.now(),
                                    bootstrap=bool(invite_accept_workspace))
                                for source_key, field, exporting in ctx.disclosures:
                                    ctx.authorize_disclosure(con, source_key, [field], export=exporting)
                                _services.record_success(control, ctx, operation, self.config.now())
                                con.commit()
                    if outward:
                        with db.transaction(con, write=False):
                            _services._workspace_authority(con, ctx, entry)
                        await handler(ctx, con, entry, via_session, current_session, buffered_send, **extra)
                        if any(m.get("status", 200) >= 400 for m in messages):
                            raise _AbortBuffered(messages)
                        with db.transaction(control, write=True):
                            _services.fenced_authority_check(
                                self.store, self.config.data_dir, ctx, operation=operation,
                                control=control, now=self.config.now())
                            _services.record_success(control, ctx, operation, self.config.now())
                    if operation in EXTERNAL_CALL_PATHS:
                        with db.transaction(con, write=False):
                            _services._workspace_authority(con, ctx, entry)
                            await handler(ctx, con, entry, via_session, current_session, buffered_send, **extra)
                        if any(m.get("status", 200) >= 400 for m in messages):
                            raise _AbortBuffered(messages)
                        with db.transaction(control, write=True):
                            _services.fenced_authority_check(
                                self.store, self.config.data_dir, ctx, operation=operation,
                                control=control, now=self.config.now())
                            _services.record_success(control, ctx, operation, self.config.now())
                    await release()
                else:
                    with db.transaction(con, write=False):
                        await handler(ctx, con, entry, via_session, session, buffered_send, **extra)
                    if any(m.get("status", 200) >= 400 for m in messages):
                        raise _AbortBuffered(messages)
                    with db.transaction(control, write=True):
                        _services.fenced_authority_check(
                            self.store, self.config.data_dir, ctx, operation=operation,
                            control=control, now=self.config.now(),
                            bootstrap=bool(invite_accept_workspace))
                        # The guard again just before the success row: a
                        # refusal rolls this transaction back, so no
                        # "success" row for a 404 (CLIP4-R2-03).
                        if audio_volume and _media.import_volume_code() is not None:
                            raise citations.RevisionUnavailable("recording is unavailable")
                        _services.record_success(control, ctx, operation, self.config.now())
                    await release()
            except _AbortBuffered as exc:
                for message in exc.messages:
                    await send(message)
            finally:
                control.close()
        except _ba.WorkspaceSwitchConflict as exc:
            await self._send(send, *_json_response(409, {
                "ok": False, "workspace_switch_revision": exc.current_revision,
                "error": {"code": "WORKSPACE_SWITCH_CONFLICT",
                          "message": str(exc), "status": 409,
                          "details": {"workspace_switch_revision": exc.current_revision}}}))
        except (_conn.GrantError, _access.AuthorizationError,
                _ops.Conflict, _ops.MissingPrecondition, _ops.BadInput,
                _search.FilterRejected, ValueError, TypeError, citations.CitationError,
                customer_contracts.SelectorConflict, customer_contracts.SelectorUnavailable,
                customer.WindowChanged, exports.DigestMismatch,
                corrections.RateLimited) as exc:
            await self._send(send, *self._service_error(exc))
        except Exception as exc:
            self._log_unhandled(exc, method, scope.get("path", ""))
            await self._send(send, *_json_response(500, {
                "error": {"code": "INTERNAL", "message": "internal error",
                          "status": 500, "details": {}}}))
        finally:
            con.close()
            await self.limiter.release(*release_key)
            if charged_workspace is not None:
                await self.limiter.release_workspace(charged_workspace)
                if media_turn:
                    await self.limiter.release_media_turn(charged_workspace)

    async def _customer_route(self, scope: dict, receive, send,
                              headers: dict, method: str, path: str,
                              query: dict) -> None:
        if path == _rights.ASK_PATH or path.startswith(_rights.ASK_PATH + "/"):
            await self._ask_route(scope, receive, send, headers, method, path)
            return
        if path == _rights.MEANING_PATH or path.startswith(_rights.MEANING_PATH + "/"):
            await self._meaning_route(scope, receive, send, headers, method, path)
            return
        if path == _rights.BRIEF_CHECK_PATH or path.startswith(_rights.BRIEF_CHECK_PATH + "/"):
            await self._brief_check_route(scope, receive, send, headers, method, path)
            return
        prepared: dict = {}

        async def run(ctx, con, entry, _via_session, _session, response_send):
            from earwitness.app import services as _services
            if not _services.is_personal_token(ctx):
                await self._dispatch_customer(response_send, receive, headers, method,
                                              path, query, ctx, con, entry,
                                              prepared=prepared.get("value"))
                return
            # Personal agent tokens: deny by default (AGT-SEC-R4-01). The
            # reply is buffered by _with_workspace; a route that answered
            # without passing the declared-scope gate in _require_member
            # is refused before anything is sent.
            passed = []
            reset = _services.READER_GATE.set(passed)
            try:
                await self._dispatch_customer(response_send, receive, headers, method,
                                              path, query, ctx, con, entry)
            finally:
                _services.READER_GATE.reset(reset)
            # The operation the entry gate checked must be the one the
            # route's service gated (the two tables cannot drift apart).
            declared = personal_token_route(method, path)
            if not passed or declared is None or declared[1] not in passed:
                raise _access.AuthorizationError("agent tokens cannot read this", 403)
        audio = method == "GET" and path.startswith("/api/v1/customer") and re.fullmatch(
            r"/records/(.+)/media", path[len("/api/v1/customer"):]) is not None
        offload = offload_pool = None
        if method == "POST" and path == "/api/v1/customer/research-export":
            # The export's match and render run off the event loop on a
            # read-only connection; the handler stores the result in a
            # short write (stall measurement: a 113 s export held every
            # other request of the worker).
            # The body is parsed in the worker, after sign-in, CSRF and the
            # first authority check (PBA-R3-03): bad JSON answers 400 only
            # to a caller who passed them.
            try:
                length = int(headers.get("content-length", "0") or "0")
                raw = await self._read_body(receive, length)
            except (ValueError, _conn.GrantError) as exc:
                await self._send(send, *self._service_error(exc))
                return
            receive = _replay_body(raw, receive)

            def offload(ctx, entry):
                from earwitness.app import services as _services
                prepared["value"] = _services.prepare_research_export(
                    self.store, self.config.data_dir, ctx, entry,
                    _parse_json_body(raw) if raw else {})
        elif method == "POST" and path == "/api/v1/customer/saved-items":
            # PERF-HOL: a brand bookmark resolves its brand off the event
            # loop first (seconds on a host-size workspace); the save runs
            # every check again and reuses that resolution when nothing
            # changed meanwhile (services.prepare_brand_bookmark).
            # The body is parsed after the checks, as for an export.
            try:
                length = int(headers.get("content-length", "0") or "0")
                raw = await self._read_body(receive, length)
            except (ValueError, _conn.GrantError) as exc:
                await self._send(send, *self._service_error(exc))
                return
            receive = _replay_body(raw, receive)

            def offload(ctx, entry):
                from earwitness.app import services as _services
                _services.prepare_brand_bookmark(
                    self.store, self.config.data_dir, ctx, entry,
                    _parse_json_body(raw) if raw else {})
            offload_pool = self.read_pool
        # A show cover rides the cover lane: every limiter key of the
        # request starts with ART_LANE (ART-PERF-FIX1).
        art = method == "GET" and re.fullmatch(
            r"/api/v1/customer/shows/[^/]+/artwork", path) is not None
        lane = _LIMIT_LANE.set(ART_LANE if art else "")
        try:
            await self._with_workspace(scope, receive, send, headers, method, run,
                                       audio_volume=audio, offload=offload,
                                       offload_pool=offload_pool)
        finally:
            _LIMIT_LANE.reset(lane)

    # ---------------------------------------------------------------- Ask

    def _ask_ledger(self):
        from . import ask as _ask
        from . import meta_client as _meta
        return self.ask_ledger() if self.ask_ledger else _ask.open_ledger(_meta.budget_dir())

    def _ask_client(self):
        from . import meta_client as _meta
        return self.ask_client() if self.ask_client else _meta.MetaClient(
            _meta.load_key(_meta.key_file_path()))

    def _ask_available(self, ctx, control=None) -> bool:
        """The switch, then ask.available (the ledger, the key file and the
        workspace's model addendum) for a browser session member; read on
        every call, never kept."""
        if ctx is None or not _rights.feature_enabled(self.config, "ask"):
            return False
        from . import ask as _ask
        if not _ask._member(ctx):
            return False
        try:
            ledger = self._ask_ledger()
        except Exception as exc:  # noqa: BLE001 - no ledger: not available
            self._log_unhandled(exc)
            return False
        return _ask.available(self.store, self.config.data_dir, control, ledger, ctx)

    def _ask_session(self, ctx, control=None) -> tuple:
        """The session's (`ask_available`, `ask_paused`) (lead U3-17): the
        switch and the member, then ONE ledger open and one read for both
        answers (ASKP-LOWS); ask.session_ask checks the addendum and the key.
        A ledger that cannot be opened is paused (RECOVERY). Read on every
        call, never kept; `ask_paused` admits nothing."""
        if ctx is None or not _rights.feature_enabled(self.config, "ask"):
            return False, False
        from . import ask as _ask
        if not _ask._member(ctx):
            return False, False
        try:
            ledger = self._ask_ledger()
        except Exception as exc:  # noqa: BLE001 - no ledger: not available; RECOVERY
            self._log_unhandled(exc)
            ledger = None
        return _ask.session_ask(self.store, self.config.data_dir, control, ledger, ctx)

    def _ask_button(self, ctx, display) -> bool:
        """A brand read's `ask_button`: Ask is available and the brand's
        button is shown (ask.brand_button_visible)."""
        if not self._ask_available(ctx):
            return False
        from . import ask as _ask
        return _ask.brand_button_visible(self.store, display)

    async def _ask_route(self, scope: dict, receive, send, headers: dict, method: str,
                         path: str) -> None:
        """The Ask routes (Meta Ask design 4.1, ASK-VIEW.md). _dispatch refused
        a switched-off request before any credential or body read. Browser
        session members only: a bearer is refused here; _with_workspace
        checks the session, CSRF and membership; _ask_answer refuses any
        other credential. Another member's turn or thread is 404."""
        if _bearer(headers):
            await self._send(send, *_forbidden(
                "FORBIDDEN", "Ask needs a member's signed-in browser session"))
            return
        found = ASK_TURN_PATH.fullmatch(path)
        if method == "POST" and path == _rights.ASK_PATH:
            name, ident = "start", None
        elif method == "GET" and path == _rights.ASK_PATH + "/threads":
            name, ident = "latest", None
        elif method == "GET" and found and found.group(3) is None:
            name, ident = found.group(1)[:-1], found.group(2)     # turn | thread
        elif method == "POST" and found and found.group(1) == "turns" and found.group(3):
            name, ident = found.group(3)[1:], found.group(2)      # cancel | report
        else:
            await self._send(send, *_not_found())
            return

        async def handler(ctx, con, entry, _via_session, _session, response_send):
            await self._send(response_send, *await self._ask_answer(
                ctx, con, receive, headers, name, ident))
        await self._with_workspace(scope, receive, send, headers, method, handler)

    async def _ask_answer(self, ctx, con, receive, headers: dict, name: str, ident) -> tuple:
        """One admitted Ask request's response (status, body, headers)."""
        from . import ask as _ask
        if not _ask._member(ctx):
            return _forbidden("FORBIDDEN", "Ask needs a member's signed-in browser session")
        try:
            if name == "turn":
                return _json_response(200, await self._in_pool(
                    self.read_pool, _ask.read_turn, self.store, ctx, ident))
            if name == "thread":
                return _json_response(200, await self._in_pool(
                    self.read_pool, _ask.read_thread, self.store, ctx, ident))
            if name == "latest":
                return _json_response(200, await self._in_pool(
                    self.read_pool, _ask.latest_thread, self.store, ctx))
            if name == "cancel":
                _ask.cancel_turn(self.store, ctx, ident)
                return _json_response(202, {"ok": True})
        except _ask.AskNotFound:
            return _not_found()
        raw = await self._read_body(receive, int(headers.get("content-length", "0") or 0))
        if name == "report":
            return self._ask_report(ctx, con, ident, raw)
        return await self._ask_start(ctx, con, raw)

    async def _ask_start(self, ctx, con, raw: bytes) -> tuple:
        """POST /api/v1/customer/ask: 202 {"turn_id", "thread_id"}, or a
        refusal by code only (ASK-VIEW.md). A question or prepared brand that
        start_turn refuses (length, a recording id, a hidden brand button)
        is 400 INVALID_QUESTION before any reservation."""
        from . import ask as _ask
        from . import meta_client as _meta
        try:
            body = _parse_json_body(raw)
            thread_id = body.get("thread_id")
            if set(body) - {"question", "thread_id", "prepared"} or (
                    thread_id is not None and not isinstance(thread_id, str)):
                raise ValueError("unknown request shape")
            ledger, client = self._ask_ledger(), self._ask_client()
            turn_id = await self._in_pool(self.read_pool, functools.partial(
                _ask.start_turn, self.store, self.config.data_dir, None, ctx,
                question=body.get("question"), thread_id=thread_id,
                prepared=body.get("prepared"), client=client, ledger=ledger))
        except (ValueError, _ask.AskError, _meta.MetaKeyError) as exc:
            return _ask_refusal(exc)
        row = con.execute("SELECT thread_id FROM ask_turns WHERE id=?", (turn_id,)).fetchone()
        return _json_response(202, {"turn_id": turn_id, "thread_id": row[0] if row else None})

    def _ask_report(self, ctx, con, turn_id, raw: bytes) -> tuple:
        """POST .../ask/turns/<id>/report: one ask_reports row, 201. The asker
        only (anyone else gets the unknown-turn 404), a reason of 1-500
        characters holding no recording id, at most 5 reports a turn. Runs in
        the request's workspace write transaction: a refusal stores nothing."""
        from . import ask as _ask
        from . import services as _services
        try:
            body = _parse_json_body(raw)
        except ValueError:
            body = None
        reason = body.get("reason") if isinstance(body, dict) and set(body) == {"reason"} \
            else None
        if not isinstance(reason, str):
            return _ask_invalid("a report holds reason only")
        text = reason.strip()
        if not 1 <= len(text) <= ASK_REPORT_MAX:
            return _ask_invalid(f"reason must be text of 1 to {ASK_REPORT_MAX} characters")
        if _rights.has_recording_id(text):
            return _ask_invalid(_rights.RECORDING_ID_REFUSAL)
        row = con.execute("SELECT h.asker_principal FROM ask_turns t JOIN ask_threads h"
                          " ON h.id = t.thread_id WHERE t.id=?", (str(turn_id),)).fetchone()
        if row is None or row[0] != ctx.principal_id:
            return _not_found()
        if con.execute("SELECT COUNT(*) FROM ask_reports WHERE turn_id=?",
                       (str(turn_id),)).fetchone()[0] >= ASK_REPORTS_PER_TURN:
            return _json_response(429, {"error": {
                "code": "RATE_LIMITED", "message": "this answer already has 5 reports",
                "status": 429, "details": {}}})
        report_id = "r_" + secrets.token_hex(12)
        con.execute("INSERT INTO ask_reports (id, turn_id, reporter_principal, reason,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (report_id, str(turn_id), ctx.principal_id, text,
                     _ask._iso(self.config.now())))
        _services._refuse_stored(con, "ask_reports", ("reason",), "id=?", (report_id,))
        return _json_response(201, {"ok": True})

    # ---------------------------------------------------------------- meaning alerts

    def _meaning_ledger(self):
        from . import meaning as _meaning
        from . import meta_client as _meta
        return self.meaning_ledger() if self.meaning_ledger else _meaning.open_ledger(
            _meta.budget_dir())

    def _meaning_client(self):
        from . import meta_client as _meta
        return self.meaning_client() if self.meaning_client else _meta.MetaClient(
            _meta.load_key(_meta.key_file_path()))

    def _meaning_available(self, ctx, control=None) -> bool:
        if ctx is None or not _rights.feature_enabled(self.config, "meaning_alerts"):
            return False
        from . import meaning as _meaning
        if not _meaning._member(ctx):
            return False
        try:
            ledger = self._meaning_ledger()
        except Exception as exc:  # noqa: BLE001 - no ledger: not available
            self._log_unhandled(exc)
            return False
        return _meaning.available(self.store, self.config.data_dir, control, ledger, ctx)

    async def _meaning_route(self, scope: dict, receive, send, headers: dict, method: str,
                             path: str) -> None:
        """The meaning-alert routes (docs/ALERT-VIEW.md). _dispatch refused a
        switched-off request before any credential or body read. Browser
        session members only: a bearer is refused here; _with_workspace
        checks the session, CSRF and membership; _meaning_answer refuses any
        other credential."""
        if _bearer(headers):
            await self._send(send, *_forbidden(
                "FORBIDDEN", "meaning alerts need a member's signed-in browser session"))
            return
        found = MEANING_PATH.fullmatch(path)
        name = ident = None
        if found:
            action, run, report, alert, sub = found.groups()
            if method == "POST" and action:
                name = action
            elif method == "GET" and run:
                name, ident = "run", run
            elif method == "POST" and report:
                name, ident = "report", report
            elif alert and sub and method == "POST":
                name, ident = sub, alert
            elif alert and not sub and method == "GET":
                name, ident = "alert", alert
            elif alert and not sub and method == "DELETE":
                name, ident = "delete", alert
            elif alert is None and run is None and report is None and action is None \
                    and method == "POST":
                name = "save"
        if name is None:
            await self._send(send, *_not_found())
            return

        async def handler(ctx, con, entry, _via_session, _session, response_send):
            await self._send(response_send, *await self._meaning_answer(
                ctx, con, entry, receive, headers, name, ident))
        await self._with_workspace(scope, receive, send, headers, method, handler)

    async def _meaning_answer(self, ctx, con, entry, receive, headers: dict, name: str,
                              ident) -> tuple:
        """One admitted meaning-alert request's response (status, body, headers).
        The writes on the request's connection gate inside a
        customer_query.writer_scope, as services writers do: a class is the
        writer only once its write changed rows there (RC-R3-02, PFC-R1-02)."""
        from . import customer_query as _query
        from . import meaning as _meaning
        from . import meta_client as _meta
        from . import services as _services
        if not _meaning._member(ctx):
            return _forbidden("FORBIDDEN", "meaning alerts need a member's browser session")
        try:
            if name == "run":
                _services._require_member(ctx, con, entry, "customer_alerts")
                return _json_response(200, await self._in_pool(
                    self.read_pool, _meaning.read_run, self.store, ctx, ident))
            if name == "alert":
                _services._require_member(ctx, con, entry, "customer_alerts")
                return _json_response(200, _meaning.read_alert(
                    self.store, con, ctx, ident, available=self._meaning_available(ctx)))
            if name == "delete":
                # M8: the maker or an owner, in this request's write
                # transaction (services.customer_delete_alert's gate).
                with _query.writer_scope():
                    _services._require_customer_role(con, entry, ctx, "organize",
                                                     "customer_alert_lifecycle")
                    _meaning.delete_alert(con, ctx, ident)
                return _json_response(200, {"ok": True})
            raw = await self._read_body(receive, int(headers.get("content-length", "0") or 0))
            try:
                body = _parse_json_body(raw)
            except ValueError:
                return _meaning_error(400, "INVALID_ALERT")
            if name == "report":
                if set(body) != {"reason"} or not isinstance(body["reason"], str):
                    return _meaning_error(400, "INVALID_ALERT")
                ref = _meaning.report_ref(con, ctx, ident, self.store)
                _services.customer_save_mistake(ctx, con, entry, ref, body["reason"])
                return _json_response(201, {"ok": True})
            if name == "read":
                # READ-PP: the caller's own marks, as on a word alert.
                with _query.writer_scope():
                    _services._require_customer_role(con, entry, ctx, "organize",
                                                     "customer_alert_read")
                    # A bad body is mark_read's 400, after its unknown-alert 404.
                    return _json_response(200, _meaning.mark_read(
                        con, ctx, ident,
                        body["match_ids"] if set(body) == {"match_ids"} else None))
            # Terms, preview and Check pass this gate too; they write on
            # connections of their own, so their note marks nothing.
            with _query.writer_scope():
                _services._require_customer_role(con, entry, ctx, "organize",
                                                 "customer_save_alert")
                if name == "lifecycle":
                    if set(body) != {"lifecycle"}:
                        return _meaning_error(400, "INVALID_ALERT")
                    _meaning.set_lifecycle(con, ctx, ident, body["lifecycle"])
                    return _json_response(200, _meaning.read_alert(
                        self.store, con, ctx, ident, available=self._meaning_available(ctx)))
                if name == "save":
                    if set(body) - MEANING_BODY_KEYS - {"preview_run_id"}:
                        return _meaning_error(400, "INVALID_ALERT")
                    alert_id = _meaning.save_alert(
                        self.store, con, ctx, sentence=body.get("sentence"),
                        terms=body.get("terms"), shows=body.get("shows"), brand=body.get("brand"),
                        preview_run_id=body.get("preview_run_id"),
                        clock=self.config.now)
                    return _json_response(201, {"alert_id": alert_id})
            ledger, client = self._meaning_ledger(), self._meaning_client()
            if name == "terms":
                if set(body) - {"sentence", "brand"}:
                    return _meaning_error(400, "INVALID_ALERT")
                return _json_response(200, await self._in_pool(self.read_pool, functools.partial(
                    _meaning.suggest_terms, self.store, self.config.data_dir, None, ctx,
                    sentence=body.get("sentence"), brand=body.get("brand"), client=client,
                    ledger=ledger)))
            if name == "preview":
                if set(body) - MEANING_BODY_KEYS:
                    return _meaning_error(400, "INVALID_ALERT")
                run_id = await self._in_pool(self.read_pool, functools.partial(
                    _meaning.start_preview, self.store, self.config.data_dir, None, ctx,
                    sentence=body.get("sentence"), terms=body.get("terms"),
                    shows=body.get("shows"), brand=body.get("brand"), client=client,
                    ledger=ledger))
                return _json_response(202, {"run_id": run_id})
            if name == "check":
                if body:
                    return _meaning_error(400, "INVALID_ALERT")
                run_id = await self._in_pool(self.read_pool, functools.partial(
                    _meaning.start_check, self.store, self.config.data_dir, None, ctx, ident,
                    client=client, ledger=ledger))
                return _json_response(202, {"run_id": run_id})
        except (ValueError, _meaning.MeaningError, _meta.MetaKeyError) as exc:
            return _meaning_refusal(exc)
        return _not_found()

    # ---------------------------------------------------------------- live brief check

    def _brief_check_switches(self) -> bool:
        return _rights.feature_enabled(self.config, "brief_check_live") \
            and _rights.feature_enabled(self.config, "brief_check")

    def _brief_check_available(self, ctx, control=None) -> bool:
        if ctx is None or not self._brief_check_switches():
            return False
        from . import ask as _ask
        from . import brief_check_live as _live
        if not _ask._member(ctx):
            return False
        try:
            ledger = self._meaning_ledger()
        except Exception as exc:  # noqa: BLE001 - no ledger: not available
            self._log_unhandled(exc)
            return False
        return _live.available(self.store, self.config.data_dir, control, ledger, ctx)

    def _brief_check_draft(self, con, result):
        """With model_checks in the draft, it also names the shown live-run
        rows whose placement's requirements changed since their run
        (live_requirements_changed; the page marks each box stale). With
        brief_check_live off, that one field comes only when a row is stale."""
        draft = result.get("draft") if isinstance(result, dict) else None
        if isinstance(draft, dict) and "model_checks" in draft:
            from . import brief_check_live as _live
            stale = _live.requirements_changed(con, draft["model_checks"])
            if stale or self._brief_check_switches():
                draft["live_requirements_changed"] = stale
        return result

    async def _brief_check_route(self, scope: dict, receive, send, headers: dict, method: str,
                                 path: str) -> None:
        """POST /api/v1/customer/brief-check (202 {run_id}) and GET
        .../brief-check/runs/<id>. _dispatch refused a switched-off request
        before any credential or body read. Browser session members only;
        the press needs the right to change the campaign."""
        if _bearer(headers):
            await self._send(send, *_forbidden(
                "FORBIDDEN", "the brief check needs a member's signed-in browser session"))
            return
        run = BRIEF_CHECK_RUN_PATH.fullmatch(path)
        if method == "POST" and path == _rights.BRIEF_CHECK_PATH:
            name, ident = "press", None
        elif method == "GET" and run:
            name, ident = "run", run.group(1)
        else:
            await self._send(send, *_not_found())
            return

        async def handler(ctx, con, entry, _via_session, _session, response_send):
            await self._send(response_send, *await self._brief_check_answer(
                ctx, con, entry, receive, headers, name, ident))
        await self._with_workspace(scope, receive, send, headers, method, handler)

    async def _brief_check_answer(self, ctx, con, entry, receive, headers: dict, name: str,
                                  ident) -> tuple:
        from . import ask as _ask
        from . import brief_check_live as _live
        from . import meta_client as _meta
        from . import services as _services
        if not _ask._member(ctx):
            return _forbidden("FORBIDDEN", "the brief check needs a member's browser session")
        try:
            if name == "run":
                _services._require_member(ctx, con, entry, "customer_campaigns")
                return _json_response(200, await self._in_pool(
                    self.read_pool, _live.read_run, self.store, ctx, ident))
            raw = await self._read_body(receive, int(headers.get("content-length", "0") or 0))
            try:
                body = _parse_json_body(raw)
            except ValueError:
                return _brief_check_error(400, "INVALID_BRIEF_CHECK")
            if not isinstance(body, dict) or set(body) != BRIEF_CHECK_BODY_KEYS \
                    or not isinstance(body["campaign_id"], int) \
                    or isinstance(body["campaign_id"], bool):
                return _brief_check_error(400, "INVALID_BRIEF_CHECK")
            _live.require_editor(con, entry, ctx, body["campaign_id"])
            if not self._brief_check_switches():
                raise _live.BriefCheckUnavailable("SWITCH_OFF")
            ledger, client = self._meaning_ledger(), self._meaning_client()
            run_id = await self._in_pool(self.read_pool, functools.partial(
                _live.start, self.store, self.config.data_dir, None, ctx, body["campaign_id"],
                body["placement_id"], brief=body["brief"], client=client, ledger=ledger))
            return _json_response(202, {"run_id": run_id})
        except (ValueError, _live.BriefCheckError, _meta.MetaKeyError) as exc:
            return _brief_check_refusal(exc)

    async def _agent_tokens_route(self, scope: dict, receive, send,
                                  headers: dict, method: str, path: str) -> None:
        """Personal agent tokens (design 4.2). Guards, in order: a bearer is
        refused, the agent_access switch must be on, and (inside the
        handler) only editors and owners create."""
        if _bearer(headers):
            await self._send(send, *_forbidden("FORBIDDEN", "agent tokens ride browser sessions"))
            return
        if not _rights.feature_enabled(self.config, "agent_access"):
            await self._send(send, *_forbidden("FEATURE_DISABLED",
                                               "agent access is off for this pilot"))
            return
        revoke = re.fullmatch(re.escape(AGENT_TOKENS_PATH) + r"/([^/]+)/revoke", path)
        if method == "GET" and path == AGENT_TOKENS_PATH:
            handler = self._agent_tokens_list
        elif method == "POST" and path == AGENT_TOKENS_PATH:
            async def handler(ctx, con, entry, via_session, session, response_send, *, control):
                await self._agent_tokens_create(ctx, con, entry, receive, headers,
                                                response_send, control=control)
        elif method == "POST" and revoke:
            grant_id = revoke.group(1)  # ASGI already decoded the path

            async def handler(ctx, con, entry, via_session, session, response_send, *, control):
                await self._agent_tokens_revoke(ctx, grant_id, response_send, control=control)
        else:
            await self._send(send, *_not_found())
            return
        await self._with_workspace(scope, receive, send, headers, method, handler,
                                   pass_control=True)

    async def _agent_tokens_list(self, ctx, con, entry, via_session, session,
                                 send, *, control) -> None:
        """Owners see every member's tokens; others see their own. Never a
        token or a hash."""
        now = self.config.now()
        from earwitness.app import services as _services
        member = None if ctx.admitted_role == "owner" else ctx.principal_id
        emails = _services._visible_member_emails(ctx, con, entry["row_namespace"])
        items = []
        for item in _conn.list_member_grants(control, ctx.workspace_id, member):
            items.append({**item, "own": item["member_id"] == ctx.principal_id,
                          "member_email": emails.get(item["member_id"], ""),
                          "status": _conn.status_of(self.store, item["id"], now=now)})
        await self._send(send, *_json_response(200, {
            "ok": True, "tokens": items, "mcp_url": self._mcp_url(),
            "can_create": ctx.admitted_role in ("editor", "owner"),
            "scopes": list(_conn.PERSONAL_TOKEN_SCOPES),
            "default_scopes": list(_conn.PERSONAL_TOKEN_DEFAULT_SCOPES),
            "days": list(_conn.PERSONAL_TOKEN_DAYS),
            "default_days": _conn.PERSONAL_TOKEN_DEFAULT_DAYS,
            "cap": _conn.PERSONAL_TOKEN_CAP}))

    def _mcp_url(self) -> str:
        if self.config.resource_uri:
            return self.config.resource_uri
        base = (self.config.public_base_url or "").rstrip("/")
        return base + MCP_PATH

    async def _agent_tokens_create(self, ctx, con, entry, receive, headers: dict, send, *,
                                   control) -> None:
        if ctx.admitted_role not in ("editor", "owner"):
            await self._send(send, *_forbidden(
                "FORBIDDEN", "Only editors and owners can create agent tokens."))
            return
        raw = await self._read_body(receive, int(headers.get("content-length", "0") or 0))
        body = _parse_json_body(raw) if raw else {}
        if not isinstance(body, dict):
            raise ValueError("the body must be a JSON object")
        if set(body) - {"label", "scopes", "expires_in_days"}:
            raise ValueError("agent token requests hold label, scopes and expires_in_days only")
        label = body.get("label", _conn.PERSONAL_LABEL_DEFAULT)
        if not _conn._personal_label_ok(label):
            raise ValueError("label must be 1-40 letters, digits, spaces, dots, dashes or underscores")
        from . import rights_admission as _rights
        if _rights.has_recording_id([label]):
            raise ValueError(_rights.RECORDING_ID_REFUSAL)
        scopes = body.get("scopes")
        if not isinstance(scopes, list) or not scopes or \
                any(not isinstance(s, str) or s not in _conn.PERSONAL_TOKEN_SCOPES for s in scopes):
            raise ValueError("scopes must name at least one read scope from the list")
        days = body.get("expires_in_days")
        if isinstance(days, bool) or days not in _conn.PERSONAL_TOKEN_DAYS:
            raise ValueError("expires_in_days must be 1, 7 or 14")
        now = self.config.now()
        try:
            expires_at = _conn.personal_token_expiry(now, days, self.config.pilot_end)
        except _conn.GrantError as exc:
            await self._send(send, *_json_response(exc.status, {"error": {
                "code": exc.code, "message": str(exc), "status": exc.status, "details": {}}}))
            return
        if _conn.count_active_tokens(control, ctx.workspace_id, ctx.principal_id, now) \
                >= _conn.PERSONAL_TOKEN_CAP:
            await self._send(send, *_json_response(409, {"error": {
                "code": "TOKEN_LIMIT",
                "message": f"You already have {_conn.PERSONAL_TOKEN_CAP} active agent tokens."
                           " Revoke one first.",
                "status": 409, "details": {}}}))
            return
        issued = _conn.issue_personal_token(
            control, member_id=ctx.principal_id, member_role=ctx.admitted_role,
            workspace_id=ctx.workspace_id, label=label, scopes=scopes,
            expires_at=expires_at, now=now)
        self.store.record_consent(issued["grant_id"], scopes=scopes, content_categories=(),
                                  policy_rev=1, now=now, control=control)
        self.store.audit(request_id=ctx.request_id, grant_id=issued["grant_id"],
                         principal_id=ctx.principal_id, workspace_id=ctx.workspace_id,
                         operation="token_issued", outcome="issued", now=now,
                         control=control)
        # The workspace issuance record (AGT-SEC-R2-01). The workspace
        # commits first; if the control commit then fails, the record
        # names a grant that does not exist and is harmless.
        _conn.record_personal_issuance(con, entry["row_namespace"], issued["grant_id"],
                                       ctx.principal_id)
        # Buffered: _with_workspace sends it only after the control
        # transaction commits, so no token leaves a failed commit.
        await self._send(send, *_json_response(200, {
            "ok": True, "id": issued["grant_id"], "token": issued["token"],
            "mcp_url": self._mcp_url(), "expires_at": expires_at,
            "scopes": sorted(set(scopes))}))

    async def _agent_tokens_revoke(self, ctx, grant_id: str, send, *, control) -> None:
        row = _conn.personal_grant(control, ctx.workspace_id, grant_id)
        own = row is not None and row["member_id"] == ctx.principal_id
        if row is None or not (own or ctx.admitted_role == "owner"):
            await self._send(send, *_not_found())
            return
        self.store.revoke_grant(row["id"], "member revoke" if own else "owner revoke",
                                now=self.config.now(), control=control)
        await self._send(send, *_json_response(200, {"ok": True, "revoked": row["id"]}))

    # -- Owner ad review (BRAND-CONFIRM-ROUTE) ------------------------------
    #
    # The loopback operator's Ad review for the hosted workspace owner:
    # the queue, any read by its record id, and Confirm / Type (this read
    # only) / Not an ad / Undo through corrections.act and corrections.undo,
    # with the signed-in person's principal as the actor. Guards, in order:
    # a bearer is refused before any lookup; a browser session is required;
    # then _with_workspace (CSRF and Origin on writes before any workspace
    # lookup, membership, the switch revision fence); then the owner role;
    # then the record. The page's files go through the same guards and
    # answer the 404 page to anyone but the owner.

    async def _owner_review(self, scope: dict, receive, send,
                            headers: dict, method: str, path: str) -> None:
        page = path.startswith(OWNER_PAGE_PREFIX)
        if page:  # OBR-R1-06: the owner's 200 and everyone's 404 never share a cache
            send = _private_page(send)
        session = None if _bearer(headers) else self._lookup_browser(headers)
        if session is None:
            if page:
                await self._public(send, "404.html", status=404)
            else:
                await self._send(send, *_forbidden(
                    "FORBIDDEN", "sign in as the workspace owner"))
            return
        # The allow-list, before any workspace is opened (OBR-R1-02).
        if str(session["active_workspace"] or "") not in _owner_review_workspaces():
            if page:
                await self._public(send, "404.html", status=404)
            else:
                await self._send(send, *_not_found())
            return
        if page:
            if method != "GET" or path not in OWNER_PAGE_FILES:
                await self._public(send, "404.html", status=404)
                return
            rel, ctype = OWNER_PAGE_FILES[path]

            async def serve(ctx, con, entry, via_session, session, response_send):
                self._require_review_owner(ctx, entry)
                await self._send_file(response_send, rel, ctype, status=200)
            sent = []

            async def keep(message):
                sent.append(message)
            await self._with_workspace(scope, receive, keep, headers, method, serve)
            start = next((m for m in sent if m["type"] == "http.response.start"), None)
            if start is None or start["status"] != 200:
                await self._public(send, "404.html", status=404)
                return
            for message in sent:
                await send(message)
            return
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
        record = re.fullmatch(re.escape(OWNER_REVIEW_PATH) + r"/([^/]+)(/action|/undo)?", path)
        if method == "GET" and path == OWNER_REVIEW_PATH:
            kind, evidence_id = "queue", None
        elif record and (method, record.group(2)) in (
                ("GET", None), ("POST", "/action"), ("POST", "/undo")):
            kind = (record.group(2) or "/item")[1:]
            evidence_id = record.group(1)  # the server already decoded the path
        else:
            await self._send(send, *_not_found())
            return

        async def handler(ctx, con, entry, via_session, session, response_send):
            await self._owner_review_answer(ctx, con, entry, receive, headers, query, kind,
                                            evidence_id, response_send)
        await self._with_workspace(scope, receive, send, headers, method, handler)

    @staticmethod
    def _require_review_owner(ctx, entry) -> None:
        """The signed-in owner of this workspace, through a browser
        session: no editor, viewer, share guest or reader credential; and
        the opened workspace is still on the allow-list."""
        from earwitness.app import ops as _ops
        if str(entry.get("workspace_id") or "") not in _owner_review_workspaces():
            raise _ops.NotFound("not found")
        if ctx.credential_kind != "browser_session" or ctx.authority_kind != "membership" \
                or ctx.principal_kind != "owner" or ctx.admitted_role != "owner" \
                or not ctx.principal_id:
            raise _access.AuthorizationError("only a workspace owner can review ads", 403)

    @staticmethod
    def _owner_undo_allowed(con, evidence_id: str, record_key) -> None:
        """Undo here reverses only a save this route can make: the read's
        newest label is confirm, type or not_ad and made no corpus-wide
        rule. Anything else is 400 before undo writes anything."""
        from earwitness.app import corrections as _corrections
        from earwitness.app import ops as _ops
        if not _corrections.present(con):
            return  # undo itself reports the missing tables
        label = con.execute(
            "SELECT action, answer FROM ad_labels WHERE evidence_id=?"
            " OR (record_key IS NOT NULL AND record_key=?) ORDER BY id DESC LIMIT 1",
            (evidence_id, record_key)).fetchone()
        if label is None:
            return  # undo itself says there is nothing to undo
        try:
            made = json.loads(label[1] or "{}").get("rules") or []
            kinds = [m[0] for m in made]
        except (ValueError, TypeError, AttributeError, IndexError, KeyError):
            kinds = None
        if label[0] not in OWNER_REVIEW_ACTIONS or kinds is None \
                or any(k in OWNER_UNDO_REFUSED_RULES for k in kinds):
            raise _ops.BadInput("the last save on this read was made on the operator page;"
                                " undo it there")

    async def _owner_review_answer(self, ctx, con, entry, receive, headers: dict, query: dict,
                                   kind: str, evidence_id, send) -> None:
        from earwitness.app import citations as _citations
        from earwitness.app import corrections as _corrections
        from earwitness.app import customer as _customer
        from earwitness.app import db as _db
        from earwitness.app import ops as _ops
        from earwitness.app import services as _services
        self._require_review_owner(ctx, entry)
        _services._require_entitlement(con, entry, "owner_ad_review")
        if kind == "queue":
            single = lambda k, d="": (query.get(k) or [d])[0]
            try:
                limit = max(1, min(1000, int(single("limit", "200"))))
            except ValueError:
                raise _ops.BadInput("limit must be an integer") from None
            try:
                with _db.transaction(con, write=False):
                    out = _corrections.queue(con, show=single("show"), brand=single("brand"),
                                             reason=single("reason"), limit=None)
                    # OBR-R2-01: list only what the item route would open:
                    # rows customers see, on episodes the rights record shows.
                    items = out["items"]
                    where, args = _customer.customer_row_where(con)
                    ids = [i["evidence_id"] for i in items]
                    shown = {r[0] for r in con.execute(
                        "SELECT e.id FROM evidence e JOIN episodes ep ON ep.id=e.episode_id"
                        f" WHERE e.id IN ({','.join('?' * len(ids))}) AND {where}",
                        [*ids, *args])} if ids else set()
                    permitted = {ep["id"] for ep in _customer.permitted_episodes_by_id(
                        con, ctx, [i["episode_id"] for i in items])}
            except _corrections.NeedsUpgrade as exc:
                await self._send(send, *_json_response(409, {"ok": False, "error": {
                    "code": "UPGRADE_REQUIRED", "message": str(exc), "status": 409,
                    "details": {}}}))
                return
            items = [i for i in items
                     if i["evidence_id"] in shown and i["episode_id"] in permitted]
            facets = {"shows": _corrections._counts(i["show_title"] or "" for i in items),
                      "reasons": _corrections._counts(x for i in items for x in i["reasons"])}
            await self._send(send, *_json_response(200, {
                "ok": True, "items": items[:limit], "total": len(items), "facets": facets}))
            return
        # OBR-R1-01: only rows customers see as ads, on episodes the rights
        # record lets this person see; anything else is unknown here.
        where, args = _customer.customer_row_where(con)
        row = con.execute("SELECT e.episode_id, e.transcript_version_id, e.source_record_key"
                          " FROM evidence e"
                          " JOIN episodes ep ON ep.id=e.episode_id"
                          f" WHERE e.id=? AND {where}", [evidence_id, *args]).fetchone()
        if row is None or not _customer.permitted_episodes_by_id(con, ctx, [row[0]]):
            await self._send(send, *_not_found())
            return
        if kind == "item":
            # Transcript lines follow the rights record: at excerpt level
            # (Plan A) none; otherwise the disclosure layer must allow them.
            source_key = _citations._version_source_key(con, row[1]) or f"episode:{row[0]}"
            excerpt = _customer.excerpt_workspace(con, ctx)
            ctx.authorize_disclosure(con, source_key,
                                     ["metadata"] if excerpt else ["metadata", "transcript"])
            with _db.transaction(con, write=False):
                item = _corrections.queue_item(con, evidence_id)
            if excerpt:
                item["transcript"] = []
            await self._send(send, *_json_response(200, {"ok": True, "item": item}))
            return
        raw = await self._read_body(receive, int(headers.get("content-length", "0") or "0"))
        body = _parse_json_body(raw) if raw else {}
        actor = ctx.principal_id
        try:
            if not isinstance(body, dict):
                raise _ops.BadInput("the body must be a JSON object")
            if kind == "undo":
                self._owner_undo_allowed(con, evidence_id, row[2])
                out = _corrections.undo(con, evidence_id, actor, body.get("label_id"))
            else:
                if not isinstance(body, dict) or body.get("action") not in OWNER_REVIEW_ACTIONS:
                    raise _ops.BadInput("action must be one of " + ", ".join(OWNER_REVIEW_ACTIONS))
                if body.get("every_read", False) is not False:
                    raise _ops.BadInput("a typed brand saves this read only here")
                for field in ("brand", "heard"):
                    value = body.get(field)
                    if field in body and (
                            not isinstance(value, str) or OWNER_TEXT_REFUSED.search(value)
                            or len(value) > OWNER_TEXT_MAX
                            or (field == "brand" and not value)):
                        raise _ops.BadInput(f"{field} must be 1-{OWNER_TEXT_MAX} characters"
                                            " without control characters")
                action = {k: body[k] for k in ("action", "brand", "heard") if k in body}
                if action["action"] == "type":
                    action["every_read"] = False
                out = _corrections.act(con, evidence_id, action, actor)
        except (_corrections.ReadChanged, _corrections.ConfirmNeeded,
                _corrections.NeedsUpgrade) as exc:
            code = {_corrections.ReadChanged: "READ_CHANGED",
                    _corrections.ConfirmNeeded: "CONFIRM_REQUIRED"}.get(type(exc), "UPGRADE_REQUIRED")
            await self._send(send, *_json_response(409, {"ok": False, "error": {
                "code": code, "message": str(exc), "status": 409,
                "details": getattr(exc, "details", None) or {}}}))
            return
        await self._send(send, *_json_response(200, {"ok": True, **out}))

    async def _collab_route(self, scope: dict, receive, send,
                            headers: dict, method: str, path: str,
                            query: dict) -> None:
        import re
        if path == "/api/v1/workspaces" and method == "POST":
            from . import browser_auth, services
            from . import ops as _ops
            try:
                if _bearer(headers):
                    raise _access.AuthorizationError("workspace creation requires a browser session", 403)
                session = self._lookup_browser(headers)
                if session is None:
                    raise _access.AuthorizationError("browser session required", 401)
                if not browser_auth.validate_csrf(session, headers.get(browser_auth.CSRF_HEADER, ""),
                        origin=headers.get("origin", ""), host=headers.get("host", ""),
                        scheme=scope.get("scheme", "http")):
                    raise _csrf_error()
                body = _parse_json_body(await self._read_body(
                    receive, int(headers.get("content-length", "0") or 0)))
                if set(body) - {"workspace_id", "name"}:
                    raise ValueError("workspace creation holds workspace_id/name only")
                ctx = browser_auth.build_session_context(session, "", "viewer", _new_request_id())
                ctx.authority_kind, ctx.scopes = "bootstrap", frozenset({"workspace:create"})
                out = services.collab_create_workspace(
                    self.store, self.config.data_dir, ctx, body.get("workspace_id"),
                    body.get("name", ""), now=self.config.now)
                await self._send(send, *_json_response(200, {"ok": True, **out}))
            except (_access.AuthorizationError, _conn.GrantError, ValueError,
                    _ops.BadInput) as exc:
                await self._send(send, *self._service_error(exc))
            return
        feature = _rights.pilot_feature(method, path)
        if feature and not _rights.feature_enabled(self.config, feature):
            await self._send(send, *_forbidden("FEATURE_DISABLED",
                                               f"{feature} is off for this pilot"))
            return
        share_id = ""
        if path.startswith("/api/v1/shared/"):
            share_id = (query.get("share_id") or [""])[0]
            if not share_id:
                await self._send(send, *_json_response(400, {
                    "error": {"code": "INVALID_ARGUMENT",
                              "message": "share_id is required",
                              "status": 400, "details": {}}}))
                return

        async def run(ctx, con, entry, _via_session, session, response_send, *, control=None):
            await self._dispatch_collab(response_send, receive, headers, method,
                                        path, query, ctx, con, entry,
                                        session=session, control=control)
        accept = None
        # Invite accept and its read-only preflight (L25) take the
        # invitee's admission: the workspace comes from the path, and the
        # caller needs no membership yet.
        invitee_op = {"POST": "accept", "GET": "preflight"}.get(method)
        accept_match = invitee_op and re.fullmatch(
            r"/api/v1/workspaces/([^/]+)/invites/[^/]+/" + invitee_op, path)
        if accept_match:
            accept = accept_match.group(1)
        await self._with_workspace(scope, receive, send, headers, method,
                                   run,
                                   guest_share_id=share_id or None,
                                   invite_accept_workspace=accept,
                                   guest_workspace=(query.get("workspace_id") or [None])[0],
                                   pass_control=True)

    @staticmethod
    def _hosted_revision(headers: dict, body: dict):
        match = headers.get("if-match")
        if match is not None:
            value = match.strip().strip('"')
            if not re.fullmatch(r"-?[0-9]+", value):
                raise ValueError("If-Match must be an integer revision")
            return int(value)
        if isinstance(body, dict) and "expected_revision" in body:
            value = body["expected_revision"]
            # Exact integers only: never a boolean, a fraction or
            # non-ASCII digits coerced into a revision.
            if isinstance(value, bool) or not (
                    isinstance(value, int)
                    or isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value)):
                raise ValueError("expected_revision must be an integer")
            return int(value)
        return None

    async def _dispatch_customer(self, send, receive, headers: dict,
                                 method: str, path: str, query: dict,
                                 ctx, con, entry, prepared=None) -> None:
        import re
        from earwitness.app import services as _services
        base = path[len("/api/v1/customer"):]
        body: dict = {}
        if method in ("POST", "PUT", "DELETE"):
            length = int(headers.get("content-length", "0") or "0")
            raw = await self._read_body(receive, length)
            if raw:
                body = _parse_json_body(raw)

        async def off_loop(service, *args, pool=None, **kwargs):
            """PERF-HOL: service(ctx, ro, entry, ...) in the read pool (or
            pool), on a read-only snapshot of this request's database
            (services.readonly_snapshot), so a read that takes seconds on a
            host-size workspace never holds the event loop. The service runs
            its own checks there, in the same order; the final authority and
            disclosure checks follow on the loop as for any read."""
            return await self._in_pool(
                pool or self.read_pool, _services.readonly_snapshot, self.store,
                self.config.data_dir, ctx, entry,
                lambda ro: service(ctx, ro, entry, *args, **kwargs))

        async def ok(payload: dict):
            from earwitness.app import services as _services
            await self._send(send, *_json_response(
                200, {"ok": True,
                      "workspace_context": _services.customer_view_context(
                          ctx, entry),
                      **payload}))

        def _visible_campaign(campaign_id: int) -> int:
            """Customer campaign gate: explicitly synthetic/test campaigns
            stay out of customer lists, detail, placements, and reports.
            Unknown or synthetic ids read as unknown; operator routes
            elsewhere are unchanged and owner rows are preserved."""
            from earwitness.app import customer as _customer
            from earwitness.app import ops as _ops
            if con.execute("SELECT 1 FROM campaigns WHERE id=?",
                           (campaign_id,)).fetchone() is None:
                raise _ops.BadInput("unknown campaign")
            if not _customer.is_customer_campaign(con, campaign_id):
                raise _ops.BadInput("unknown campaign")
            return campaign_id

        if method == "GET" and base == "/overview":
            await ok({**await off_loop(_services.customer_overview),
                      "data_updated_at": _freshness.data_updated_at(
                          self.config.last_success_file, now=self.config.now())})
            return
        if method == "GET" and base == "/reader-status":
            await ok(_services.customer_reader_status(ctx, con, entry))
            return
        if method == "GET" and base == "/search":
            await ok(await off_loop(_services.customer_search,
                                    self._query_criteria(query)))
            return
        m = re.fullmatch(r"/records/(.+)/report", base)
        if method == "POST" and m:
            await ok(_services.customer_report_ad(
                ctx, con, entry, urllib.parse.unquote(m.group(1)), body or {}))
            return
        m = re.fullmatch(r"/records/(.+)/mistake", base)
        if method == "GET" and m:
            from earwitness.app import customer_saved
            _services._require_member(ctx, con, entry, "customer_mistake_read")
            await ok({"mistakes": customer_saved.list_notes(
                con, ref=urllib.parse.unquote(m.group(1)), ctx=ctx)["notes"]})
            return
        m = re.fullmatch(r"/records/(.+)/media", base)
        if method == "GET" and m:
            # In the media pool, off the event loop (A429-SEC-R2-01).
            served = await off_loop(_services.customer_recording,
                                    urllib.parse.unquote(m.group(1)), headers.get("range"),
                                    pool=self.media_pool)
            headers = [(b"content-type", served["mime"].encode()),
                       (b"content-length", str(len(served["payload"])).encode()),
                       (b"accept-ranges", b"bytes"), (b"cache-control", b"private, no-store"),
                       (b"x-content-type-options", b"nosniff")]
            if served["content_range"]:
                headers.append((b"content-range", served["content_range"].encode()))
            await send({"type": "http.response.start", "status": served["status"], "headers": headers})
            await send({"type": "http.response.body", "body": served["payload"]})
            return
        m = re.fullmatch(r"/records/(.+)", base)
        if method == "GET" and m:
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "100")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            try:
                start_s = float(single("start_s", "")) if single("start_s", "") else None
                end_s = float(single("end_s", "")) if single("end_s", "") else None
            except ValueError:
                raise ValueError("start_s/end_s must be numbers")
            await ok(_services.customer_record_window(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                start_s, end_s, single("cursor", "") or None, limit))
            return
        if method == "GET" and base == "/summary":
            await ok(await off_loop(_services.customer_summary, self._query_criteria(query),
                                    (query.get("group_by") or ["brand"])[0]))
            return
        if method == "GET" and base == "/brands":
            single = lambda k, d="": (query.get(k) or [d])[0]
            await ok(await off_loop(_services.customer_brands, single("q", "")))
            return
        m = re.fullmatch(r"/brands/([^/]+)", base)
        if method == "GET" and m:
            out = await off_loop(_services.customer_brand_detail,
                                 urllib.parse.unquote(m.group(1)))
            brand = out.get("brand")
            if isinstance(brand, dict):
                # A copy: the detail may be a shared cached read.
                out = {**out, "brand": {**brand, "ask_button": self._ask_button(
                    ctx, brand.get("display"))}}
            await ok(out)
            return
        if method == "GET" and base == "/shows":
            single = lambda k, d="": (query.get(k) or [d])[0]
            await ok(await off_loop(_services.customer_shows, single("q", "")))
            return
        m = re.fullmatch(r"/episodes/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_episode_reads(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/shows/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_show_detail(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/shows/([^/]+)/artwork", base)
        if method == "GET" and m:
            # Off the loop, in the cover pool: up to 24 covers load with a
            # page (ART-PERF, ART-PERF-FIX1).
            served = await off_loop(_services.customer_show_artwork,
                                    urllib.parse.unquote(m.group(1)),
                                    pool=self.art_pool)
            if served is None:
                await self._send(send, *_not_found())
                return
            import hashlib
            mime, raw = served
            etag = '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'   # ~0.1 ms a cover
            # The browser keeps a cover but asks again before each use
            # (no-cache), and the answer is a 304 only after this request's
            # access checks: a member who lost access gets the refusal, not
            # the kept copy (APS-R1-05). Vary: Cookie keeps one browser's
            # workspaces apart.
            kept = [(b"etag", etag.encode()), (b"cache-control", b"private, no-cache"),
                    (b"vary", b"Cookie"), (b"x-content-type-options", b"nosniff")]
            if_none_match = headers.get("if-none-match")
            if if_none_match is not None and _etag_matches(if_none_match, etag):
                await send({"type": "http.response.start", "status": 304, "headers": kept})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", mime.encode()),
                                    (b"content-length",
                                     str(len(raw)).encode()), *kept]})
            await send({"type": "http.response.body", "body": raw})
            return
        if method == "GET" and base == "/compare":
            single = lambda k, d="": (query.get(k) or [d])[0]
            import json as _json
            try:
                brands = _json.loads(single("brands", "[]"))
                raw_a = single("period_a", "")
                period_a = _json.loads(raw_a) if raw_a else None
                raw_b = single("period_b", "")
                period_b = _json.loads(raw_b) if raw_b else None
                raw_q = single("query", "")
                wire_query = _json.loads(raw_q) if raw_q else None
                raw_s = single("selector", "")
                wire_selector = _json.loads(raw_s) if raw_s else None
            except ValueError:
                raise ValueError("compare requires JSON brands/periods")
            await ok(await off_loop(_services.customer_compare2,
                                    brands, period_a, period_b, wire_query,
                                    wire_selector))
            return
        if method == "GET" and base == "/filter-values":
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "50")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            # SPEED-R1: in the read pool, never on the event loop (a cold
            # computation took 2.4 s and held every other request).
            await ok(await off_loop(_services.customer_filter_values, single("field", ""),
                                    single("q", ""), single("cursor", "") or None, limit))
            return
        if method == "POST" and base == "/research-query":
            await ok(await off_loop(_services.customer_research_query, body or {}))
            return
        if method == "POST" and base == "/research-export":
            out = _services.customer_research_export(
                ctx, con, entry, body or {}, prepared=prepared)
            await ok({k: v for k, v in out.items()
                      if k not in ("rows", "payload")})
            return
        if method == "GET" and base == "/archive":
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "20")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            campaign = single("campaign_id", "")
            await ok(_services.customer_archive(
                ctx, con, entry, kind=single("kind", "") or None,
                campaign_id=int(campaign) if campaign else None,
                q=single("q", ""), cursor=single("cursor", "") or None,
                limit=limit))
            return
        m = re.fullmatch(r"/exports/(\d+)", base)
        if method == "GET" and m:
            from earwitness.app import exports as _exports
            from earwitness.app import ops as _ops
            try:
                # Through the gated service: membership, then entitlement.
                out = _services.customer_export_bytes(
                    ctx, con, entry, int(m.group(1)))
            except _exports.DigestMismatch as exc:
                await self._send(send, *_json_response(409, {
                    "error": {"code": "VIEW_CHANGED", "message": str(exc),
                              "status": 409, "details": {}}}))
                return
            except _ops.NotFound as exc:
                await self._send(send, *_json_response(404, {
                    "error": {"code": "NOT_FOUND", "message": str(exc),
                              "status": 404, "details": {}}}))
                return
            except (_ops.BadInput, ValueError):
                await self._send(send, *_not_found())
                return
            raw = out["payload"].encode("utf-8")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [
                            (b"content-type",
                             out["content_type"].encode("latin-1")),
                            (b"content-length", str(len(raw)).encode()),
                            (b"content-disposition",
                             f'attachment; filename="earwitness-export-'
                             f'{out["export_id"]}.'
                             f'{"csv" if out["format"] == "csv" else "html" if out["format"] == "html" else "json"}"'.encode()),
                            (b"x-export-digest",
                             f'sha256={out["digest"]}'.encode()),
                            (b"cache-control", b"private, no-store")]})
            await send({"type": "http.response.body", "body": raw})
            return
        m = re.fullmatch(r"/creatives/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_creative_detail(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        if method == "GET" and base == "/saved-items":
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "20")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            await ok(await off_loop(
                _services.customer_saved_items, single("type", "") or None,
                single("q", ""), single("cursor", "") or None, limit))
            return
        m = re.fullmatch(r"/saved-items/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_saved_item(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        if method == "POST" and base == "/saved-items":
            await ok(_services.customer_save_item(
                ctx, con, entry, body or {}))
            return
        m = re.fullmatch(r"/saved-items/([^/]+)", base)
        if method == "PUT" and m:
            await ok(_services.customer_edit_item(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                (body or {}).get("item", body or {}),
                (body or {}).get("expected_revision",
                                 self._optional_revision(body or {}))))
            return
        m = re.fullmatch(r"/saved-items/([^/]+)", base)
        if method == "DELETE" and m:
            await ok(_services.customer_delete_item(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                self._hosted_revision(headers, body or {})))
            return
        if method == "GET" and base == "/mistakes":
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "20")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            await ok(_services.customer_note_collection(
                ctx, con, entry, single("q", ""),
                single("ref", "") or None,
                single("cursor", "") or None, limit))
            return
        m = re.fullmatch(r"/mistakes/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_note(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        m = re.fullmatch(r"/mistakes/(\d+)", base)
        if method == "PUT" and m:
            if set(body) - {"reason", "status", "expected_revision"}:
                raise ValueError("note metadata holds reason/status/expected_revision only")
            await ok(_services.customer_save_mistake(
                ctx, con, entry, None, body.get("reason", ""),
                body.get("status", "open"), int(m.group(1)),
                self._hosted_revision(headers, body)))
            return
        if method == "GET" and base == "/alerts":
            listed = await off_loop(_services.customer_alerts)
            if not _rights.feature_enabled(self.config, "meaning_alerts"):
                # customer.list_alerts joins meaning rows; only with their switch on.
                listed = dict(listed, alerts=[a for a in listed["alerts"]
                                              if a.get("kind") != "meaning"])
            await ok(listed)
            return
        if method == "POST" and base == "/alerts":
            self._deny_tracker_authority_fields(body)
            await ok(_services.customer_save_alert(
                ctx, con, entry, (body or {}).get("name", ""),
                self._body_query2(body or {}),
                (body or {}).get("origin_route", ""),
                (body or {}).get("event_types"),
                (body or {}).get("delivery", {}),
                (body or {}).get("selector"), None,
                (body or {}).get("expected_revision",
                                 self._optional_revision(body or {}))))
            return
        # At most 18 digits, so every id fits SQLite's 64-bit INTEGER; a
        # longer id is an unknown alert (404), not a 500 (RPP-SEC-R2-01).
        m = re.fullmatch(r"/alerts/(\d{1,18})", base)
        if method == "GET" and m:
            # summaries=0: callers that do not show event summaries (F-16).
            # page: the page of events summarized (PERF-BRAND-ALERT).
            from earwitness.app import customer as _customer
            page = _customer.alert_page_number((query.get("page") or [None])[0])
            await ok(await off_loop(
                _services.customer_alert, int(m.group(1)),
                summaries=(query.get("summaries") or ["1"])[0] != "0",
                page=page))
            return
        if method == "PUT" and m:
            self._deny_tracker_authority_fields(body)
            await ok(_services.customer_save_alert(
                ctx, con, entry, (body or {}).get("name", ""),
                self._body_query2(body or {}),
                (body or {}).get("origin_route", ""),
                (body or {}).get("event_types"),
                (body or {}).get("delivery", {}),
                (body or {}).get("selector"),
                int(m.group(1)),
                self._hosted_revision(headers, body or {})))
            return
        if method == "DELETE" and m:
            # M8: the maker or an owner deletes the alert and its events.
            await ok(_services.customer_delete_alert(ctx, con, entry, int(m.group(1))))
            return
        m = re.fullmatch(r"/alerts/(\d{1,18})/(check|read|lifecycle|backfill)", base)
        if method == "POST" and m:
            aid, action = int(m.group(1)), m.group(2)
            if action == "check":
                await ok(_services.customer_check_alert(ctx, con, entry, aid))
            elif action == "read":
                await ok(_services.customer_alert_read(
                    ctx, con, entry, aid, body.get("event_ids", [])))
            elif action == "lifecycle":
                await ok(_services.customer_alert_lifecycle(
                    ctx, con, entry, aid, body.get("lifecycle", ""),
                    self._hosted_revision(headers, body)))
            else:
                await ok(_services.customer_alert_backfill(
                    ctx, con, entry, aid, body.get("confirm") is True))
            return
        if method == "GET" and base == "/preferences":
            await ok(_services.customer_preferences(ctx, con, entry))
            return
        if method == "PUT" and base == "/preferences":
            prefs = {k: v for k, v in body.items() if k != "expected_revision"}
            await ok(_services.customer_save_preferences(
                ctx, con, entry, prefs,
                self._hosted_revision(headers, body)))
            return
        m = re.fullmatch(r"/records/(.+)/mistake", base)
        if method == "POST" and m:
            if set(body) - {"reason", "status", "expected_revision"}:
                raise ValueError("note creation holds reason/status/expected_revision only")
            await ok(_services.customer_save_mistake(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                body.get("reason", ""), body.get("status", "open"),
                None,
                body.get("expected_revision", self._optional_revision(body))))
            return
        if method == "GET" and base == "/campaigns":
            from earwitness.app import customer as _customer
            payload = _services.customer_campaigns(ctx, con, entry)
            payload["campaigns"] = [
                c for c in payload.get("campaigns", [])
                if isinstance(c, dict) and c.get("id") is not None
                and _customer.is_customer_campaign(con, c["id"])]
            await ok(payload)
            return
        if method == "POST" and base == "/campaigns":
            await ok(_services.customer_save_campaign(
                ctx, con, entry, body, None,
                body.get("expected_revision", self._optional_revision(body))))
            return
        m = re.fullmatch(r"/campaigns/(\d+)", base)
        if method == "GET" and m:
            await ok(_services.customer_campaign_detail(
                ctx, con, entry, _visible_campaign(int(m.group(1)))))
            return
        if method == "PUT" and m:
            raw = body or {}
            await ok(_services.customer_save_campaign(
                ctx, con, entry,
                {k: v for k, v in raw.items()
                 if k != "expected_revision"},
                _visible_campaign(int(m.group(1))),
                self._hosted_revision(headers, raw)))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/draft", base)
        if method == "GET" and m:
            # Seconds on a host-size workspace: in the read pool, off the
            # event loop (CAMPAIGN-DRAFT-SLOW). The gated service stays the
            # off_loop target (the billing wiring scan reads it there); the
            # stale-row marks (_brief_check_draft, one query over the shown
            # rows) follow on the loop, as on #7.
            draft = await off_loop(
                _services.customer_campaign_draft, _visible_campaign(int(m.group(1))),
                brief_check=_rights.feature_enabled(self.config, "brief_check"))
            await ok(self._brief_check_draft(con, draft))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/evidence-fit", base)
        if method == "GET" and m:
            await ok(_services.customer_evidence_fit(
                ctx, con, entry, _visible_campaign(int(m.group(1))),
                (query.get("ref") or [""])[0]))
            return
        if method == "POST" and re.fullmatch(r"/campaigns/(\d+)/placements",
                                             base):
            cid = _visible_campaign(int(re.fullmatch(
                r"/campaigns/(\d+)/placements", base).group(1)))
            await ok(_services.customer_save_placement(
                ctx, con, entry, cid, body or {}, None,
                (body or {}).get("expected_revision",
                                 self._optional_revision(body or {})),
                (body or {}).get("expected_campaign_revision")))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/placements/([^/]+)", base)
        if method == "PUT" and m:
            await ok(_services.customer_save_placement(
                ctx, con, entry, _visible_campaign(int(m.group(1))),
                body or {}, urllib.parse.unquote(m.group(2)),
                self._hosted_revision(headers, body or {}),
                (body or {}).get("expected_campaign_revision")))
            return
        if method == "GET" and base == "/briefs":
            single = lambda k, d="": (query.get(k) or [d])[0]
            raw_limit = single("limit", "20")
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = raw_limit
            await ok(_services.customer_briefs(
                ctx, con, entry, single("q", ""),
                single("cursor", "") or None, limit))
            return
        m = re.fullmatch(r"/briefs/([^/]+)/versions/([^/]+)", base)
        if method == "GET" and m:
            await ok(_services.customer_brief_version(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                urllib.parse.unquote(m.group(2))))
            return
        if method == "POST" and base == "/briefs":
            await ok(_services.customer_save_brief(
                ctx, con, entry, body or {}))
            return
        m = re.fullmatch(r"/briefs/([^/]+)/versions", base)
        if method == "POST" and m:
            raw = body or {}
            await ok(_services.customer_save_brief(
                ctx, con, entry,
                {k: v for k, v in raw.items()
                 if k != "expected_revision"},
                urllib.parse.unquote(m.group(1)),
                self._hosted_revision(headers, raw)))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/targets", base)
        if method == "GET" and m:
            await ok(_services.customer_campaign_targets(
                ctx, con, entry, _visible_campaign(int(m.group(1)))))
            return
        if method == "POST" and m:
            await ok(_services.customer_save_target(
                ctx, con, entry, _visible_campaign(int(m.group(1))),
                body or {}))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/allocation", base)
        if method == "POST" and m:
            out = _services.customer_allocate(
                ctx, con, entry, _visible_campaign(int(m.group(1))),
                body or {},
                (body or {}).get("expected_campaign_revision"))
            if isinstance(out, dict) and "conflict" in out:
                await self._send(send, *_json_response(
                    409, {"ok": False, **out}))
                return
            await ok(out)
            return
        m = re.fullmatch(r"/targets/([^/]+)", base)
        if method == "PUT" and m:
            from earwitness.app import campaigns as _camp
            tid = urllib.parse.unquote(m.group(1))
            cid = _camp.target_campaign(con, tid)
            await ok(_services.customer_save_target(
                ctx, con, entry, cid, body or {}, tid,
                self._hosted_revision(headers, body or {})))
            return
        if method == "DELETE" and m:
            from earwitness.app import campaigns as _camp
            tid = urllib.parse.unquote(m.group(1))
            cid = _camp.target_campaign(con, tid)
            await ok(_services.customer_delete_target(
                ctx, con, entry, cid, tid))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/targets/([^/]+)/evidence", base)
        if method in ("POST", "DELETE") and m:
            # POST keeps an observation on a research target; DELETE
            # removes that one link (G2-01), behind the same gate.
            ref = body.get("ref", "")
            if not isinstance(ref, str) or not ref:
                raise ValueError("ref must be a nonempty string")
            args = (ctx, con, entry, _visible_campaign(int(m.group(1))),
                    urllib.parse.unquote(m.group(2)), ref,
                    self._hosted_revision(headers, body))
            if method == "POST":
                await ok(_services.customer_attach_target_evidence(*args))
            else:
                await ok(_services.customer_detach_target_evidence(*args))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/placements/([^/]+)/episodes/([^/]+)/review", base)
        if m:
            if method != "POST":
                await self._send(send, *_not_found())
                return
            cid = _visible_campaign(int(m.group(1)))
            evidence_id, decision = body.get("evidence_id", ""), body.get("decision", "")
            if not isinstance(evidence_id, str) or not evidence_id:
                raise ValueError("evidence_id must be a nonempty string")
            if not isinstance(decision, str):
                raise ValueError("decision must be a string")
            await ok(_services.customer_review_episode(
                ctx, con, entry, cid, urllib.parse.unquote(m.group(2)),
                urllib.parse.unquote(m.group(3)), evidence_id, decision,
                self._hosted_revision(headers, body), body.get("code"),
                body.get("shown_code")))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/placements/([^/]+)/evidence", base)
        if m:
            cid = _visible_campaign(int(m.group(1)))
            pid = urllib.parse.unquote(m.group(2))
            if method == "POST":
                ref = body.get("ref", "")
                if not isinstance(ref, str) or not ref:
                    raise ValueError("ref must be a nonempty string")
                await ok(_services.customer_attach_evidence(
                    ctx, con, entry, cid, pid, ref,
                    self._hosted_revision(headers, body)))
            elif method == "DELETE":
                ref = body.get("ref", "")
                if not isinstance(ref, str) or not ref:
                    raise ValueError("ref must be a nonempty string")
                await ok(_services.customer_detach_evidence(
                    ctx, con, entry, cid, pid, ref,
                    self._hosted_revision(headers, body)))
            else:
                await self._send(send, *_not_found())
            return
        m = re.fullmatch(r"/campaigns/(\d+)/reports", base)
        if method == "GET" and m:
            await ok(_services.customer_reports(ctx, con, entry,
                                                _visible_campaign(int(m.group(1)))))
            return
        if method == "POST" and m:
            await ok(_services.customer_freeze_report(
                ctx, con, entry, _visible_campaign(int(m.group(1))),
                self._hosted_revision(headers, body), body.get("pins", {})))
            return
        m = re.fullmatch(r"/campaigns/(\d+)/reports/(\d+)/html", base)
        if method == "GET" and m:
            from earwitness.app import campaigns as _camp
            _services._require_member(ctx, con, entry, "customer_report_html")
            report_cid = _visible_campaign(int(m.group(1)))
            # Preview and download release the same bytes: both are exports.
            payload, _digest = _camp.get_customer_report_html(con, report_cid, int(m.group(2)), ctx=ctx)
            raw = payload.encode()
            # A new-freeze report also allows its own style block by hash.
            style_src = _camp.report_style_src(con, report_cid, int(m.group(2)), payload)
            report_headers = [(b"content-type", b"text/html; charset=utf-8"),
                              (b"content-length", str(len(raw)).encode()),
                              (b"cache-control", b"private, no-store"),
                              (b"content-security-policy",
                               b"default-src 'none'"
                               + (f"; {style_src}".encode() if style_src else b""))]
            if (query.get("download") or [""])[0] == "1":
                # The filename is built from the validated digits only.
                report_headers.append((
                    b"content-disposition",
                    f'attachment; filename="earwitness-report-{int(m.group(2))}.html"'.encode()))
            await send({"type": "http.response.start", "status": 200,
                        "headers": report_headers})
            await send({"type": "http.response.body", "body": raw})
            return
        m = re.fullmatch(r"/campaigns/(\d+)/reports/(\d+)", base)
        if method == "GET" and m:
            await ok(_services.customer_report(ctx, con, entry,
                                               _visible_campaign(int(m.group(1))),
                                               int(m.group(2))))
            return
        await self._send(send, *_not_found())

    @staticmethod
    def _optional_revision(body: dict):
        try:
            return body.get("expected_revision")
        except AttributeError:
            return None

    async def _billing_route(self, scope: dict, receive, send,
                             headers: dict, method: str, path: str,
                             query: dict) -> None:
        async def run(ctx, con, entry, _via_session, _session, response_send):
            await self._dispatch_billing(response_send, receive, headers, method,
                                         path, ctx, con, entry)
        await self._with_workspace(scope, receive, send, headers, method, run)

    async def _dispatch_billing(self, send, receive, headers: dict,
                                method: str, path: str, ctx, con,
                                entry) -> None:
        import re
        from earwitness.app import services as _services
        body: dict = {}
        if method in ("POST", "PUT"):
            length = int(headers.get("content-length", "0") or "0")
            raw = await self._read_body(receive, length)
            if raw:
                body = _parse_json_body(raw)

        async def ok(payload: dict):
            await self._send(send, *_json_response(200, {"ok": True,
                "workspace_context": _services.customer_view_context(ctx, entry), **payload}))

        base = path[len("/api/v1/billing"):]
        if method == "GET" and base == "/status":
            await ok(_services.billing_status(
                ctx, con, entry, seat=self._billing_seat_backed(
                    str(entry.get("workspace_id") or ""))))
            return
        if method == "POST" and base == "/checkout":
            if not _services._billing_config().inapp_checkout:
                # Buying happens on Stripe (design 4); no Stripe call here.
                await self._send(send, *_forbidden(
                    "FORBIDDEN", "in-app checkout is off"))
                return
            if entry.get("paid_use") is not True:
                await self._send(send, *self._evaluation_only(entry))
                return
            try:
                trial_days = int(body.get("trial_days", 0) or 0)
            except (TypeError, ValueError):
                raise ValueError("trial_days must be an integer")
            # Database reads run here; only the provider HTTP call leaves
            # the event loop, so a slow provider never stalls other requests.
            provider_call = _services.billing_checkout(
                ctx, con, entry, body.get("price_id", ""),
                body.get("success_url", ""), body.get("cancel_url", ""),
                trial_days)
            await self._billing_provider_answer(send, provider_call, ok)
            return
        if method == "POST" and base == "/portal":
            if entry.get("paid_use") is not True:
                # No portal for a workspace that is never paid: nothing to
                # upgrade. A subscription refused by the webhook is ended and
                # refunded by the operator in Stripe (refund runbook).
                await self._send(send, *self._evaluation_only(entry))
                return
            if not _services._billing_config().portal_configuration:
                raise ValueError("the billing portal is not configured")
            provider_call = _services.billing_portal(
                ctx, con, entry, body.get("return_url", ""))
            await self._billing_provider_answer(send, provider_call, ok)
            return
        await self._send(send, *_not_found())

    def _billing_seat_backed(self, workspace_id: str) -> bool:
        """True when a purchase seat Stripe keeps in step holds this
        workspace (purchases.WEBHOOK_STATES). A failed read is false: the
        page then shows no "Manage billing", never a wrong state."""
        from earwitness.app import purchases as _purchases
        try:
            control = self.store.auth_connection()
            try:
                row = _purchases.seat_for_workspace(control, workspace_id)
            finally:
                control.close()
        except sqlite3.Error:
            return False
        return row is not None and row["state"] in _purchases.WEBHOOK_STATES

    @staticmethod
    def _evaluation_only(entry) -> tuple:
        """RB1: the 403 of a paid billing path for a workspace whose rights
        record does not allow paid use (or could not be read)."""
        return _forbidden("EVALUATION_ONLY", "this workspace cannot serve a paid plan ("
                          + str(entry.get("paid_use_reason") or "rights record unavailable")
                          + ")")

    @staticmethod
    def _billing_busy() -> tuple:
        return _json_response(503, {
            "error": {"code": "UNAVAILABLE",
                      "message": "billing provider is busy or unavailable",
                      "status": 503, "details": {}}},
            (("Retry-After", "5"),))

    def _billing_slot_release(self, task) -> None:
        self.billing_slots_used -= 1
        if not task.cancelled():
            task.exception()  # retrieved: the awaiting request may be gone

    async def _billing_io(self, fn, *args):
        """Run one blocking Stripe call on a worker thread behind the
        non-waiting slots (design 8.5). Full slots raise ProviderBusy. The
        slot is freed when the thread's work ends, not when the request
        does: a cancelled request keeps it until then (the L20
        _signin_release pattern)."""
        from earwitness.app import billing as _billing
        if self.billing_slots_used >= self.billing_slots:
            raise _billing.ProviderBusy("billing provider slots are full")
        self.billing_slots_used += 1
        task = asyncio.ensure_future(asyncio.to_thread(fn, *args))
        task.add_done_callback(self._billing_slot_release)
        return await asyncio.shield(task)

    async def _billing_provider_answer(self, send, provider_call, ok) -> None:
        from earwitness.app import billing as _billing
        try:
            result = await self._billing_io(provider_call)
        except (_billing.ProviderBusy, _billing.ProviderError):
            await self._send(send, *self._billing_busy())
            return
        await ok(result)

    async def _billing_webhook(self, scope: dict, receive, send,
                               headers: dict, path: str) -> None:
        """POST /api/v1/billing/webhook: the one Stripe endpoint (design
        8.1-8.4). Unauthenticated; the signature is the auth. Routed by
        _dispatch before the generic body read.

        The retired per-workspace route and billing off: the fixed 404,
        with no body read. Then the body is read once, bounded to 64 KiB
        (declared or received: the fixed 400, no further read) inside the
        L21 deadline and shared budget; then signature, timestamp and
        parse, each failure the same fixed 400. A completed delivery is
        answered from billing_deliveries with no Stripe call and no
        workspace open. Otherwise the subscription's lock is taken (503
        when the lock table is full) and the journal is checked again
        before the delivery is worked."""
        from earwitness.app import billing as _billing
        if path != "/api/v1/billing/webhook":
            await self._send(send, *_not_found())  # retired (design 8.1)
            return
        config = _billing.config_from_env()
        if not config.webhook_secret:
            await self._send(send, *_not_found())
            return
        invalid = _BodyRefused(400, "INVALID_ARGUMENT",
                               "invalid webhook delivery")
        try:
            length = int(headers.get("content-length", "0") or "0")
        except ValueError:
            length = 0
        try:
            payload = await self._read_request_body(
                scope, receive, length, limit=_billing.WEBHOOK_MAX_BYTES,
                oversize=invalid)
        except _BodyRefused as exc:
            await self._send(send, *exc.response)
            return
        except _BodyDisconnected:
            return  # nobody is listening
        try:
            _billing.verify_webhook_signature(
                config, payload, headers.get("stripe-signature", ""))
            event = _billing.parse_webhook_event(config, payload)
        except ValueError:
            await self._send(send, *invalid.response)
            return
        event_id = str(event["id"])
        stored = self._billing_stored(event_id)
        if stored is not None:
            await self._send(send, *stored)
            return
        subscription_id = _billing.webhook_subscription(event)
        if not subscription_id:
            await self._send(send, *self._billing_complete(
                event_id, "ignored",
                "not a handled event type, or it names no subscription"))
            return
        try:
            async with self.billing_locks.hold(subscription_id):
                answer = await self._billing_delivery(config, event,
                                                      subscription_id)
        except _billing.ProviderBusy:
            answer = self._billing_retry(event_id, subscription_id,
                                         "lock_table_full")
        await self._send(send, *answer)

    @classmethod
    def _billing_retry(cls, event_id: str, subscription_id: str,
                       reason: str) -> tuple:
        """The retryable 503, with one warning naming only a fixed reason
        code, the event id and the subscription id (no body, customer data
        or secret)."""
        _oplog.log_event("billing.webhook.retry", level="warning",
                         event_id=event_id, subscription_id=subscription_id,
                         reason=reason)
        return cls._billing_busy()

    def _billing_stored(self, event_id: str) -> tuple | None:
        from earwitness.app import billing as _billing
        control = self.store.auth_connection()
        try:
            stored = _billing.stored_delivery(control, event_id)
        finally:
            control.close()
        return None if stored is None else (stored[0], stored[1], ())

    def _billing_complete(self, event_id: str, status: str,
                          reason: str) -> tuple:
        """The 200 answer of a finished delivery, recorded as completed."""
        from earwitness.app import billing as _billing
        answer = _json_response(200, {"ok": True, "event_id": event_id,
                                      "status": status, "reason": reason})
        control = self.store.auth_connection()
        try:
            _billing.complete_delivery(control, event_id, answer[0], answer[1])
        finally:
            control.close()
        return answer

    async def _billing_delivery(self, config, event: dict,
                                subscription_id: str) -> tuple:
        """One delivery under its subscription's lock (design 8.2, 8.4)."""
        from earwitness.app import billing as _billing
        from earwitness.app import services as _services
        event_id = str(event["id"])
        stored = self._billing_stored(event_id)
        if stored is not None:
            return stored
        if _billing.event_conflict(event):
            return _not_found()
        seats = _billing.PurchaseSeats(self.store)
        no_seat = ("no_seat", "no set-up workspace holds this subscription")
        try:
            # Only a definite absence completes as no seat; a transient
            # fault (SeatUnavailable) is a retryable 503.
            seat = seats.seat_for_subscription(subscription_id)
            if seat is None:
                return self._billing_complete(event_id, *no_seat)
            workspace_id, offer = seat
            opened = _billing.open_seat_db(self.store, workspace_id,
                                           self.config.data_dir)
        except _billing.SeatUnavailable:
            return self._billing_retry(event_id, subscription_id,
                                       "seat_unavailable")
        if opened is None:
            return self._billing_complete(event_id, *no_seat)
        con, entry = opened
        try:
            done = _billing.journaled_outcome(con, event_id)
            if done is not None:
                # Committed before a crash: complete it, no second read.
                return self._billing_complete(event_id, *done)
            if entry.get("paid_use") is not True:
                # RB-R1-01: never grant or update paid access for a workspace
                # whose rights record does not allow it. An unreadable record
                # is retried; an evaluation-only one completes as refused.
                if entry.get("paid_use_reason") == _rights.PAID_USE_UNREADABLE:
                    return self._billing_retry(event_id, subscription_id,
                                               "rights_unreadable")
                return self._billing_complete(event_id, "refused",
                                              entry.get("paid_use_reason") or "")
            try:
                provider = self.billing_provider \
                    or _billing.StripeSubscriptionProtocol(config)
                kind, resource = await self._billing_io(
                    _billing.fetch_current, subscription_id, provider)
            except _billing.ProviderBusy:
                return self._billing_retry(event_id, subscription_id,
                                           "provider_busy")
            except ValueError:
                return self._billing_retry(event_id, subscription_id,
                                           "provider_failed")
            if kind != "ok" and kind != "gone":
                return self._billing_retry(event_id, subscription_id,
                                           "provider_failed")
            if kind == "ok" and _billing.period_unreadable(resource):
                # No verified paid-period end: unreadable, retryable, and
                # nothing written (design 8.6; never an event-carried end).
                return self._billing_retry(event_id, subscription_id,
                                           "period_unreadable")
            if kind == "ok" and resource.get("price_id") \
                    not in _billing.offer_prices(config, offer):
                # The seat's price is its offer's (design 8.2 step 3).
                return _not_found()
            from earwitness.app import db as _db
            try:
                # The workspace connection is autocommit: the journal row
                # and the reconcile commit together or not at all.
                with _db.transaction(con, write=True):
                    out = _billing.apply_event(
                        con, entry["row_namespace"], event, config=config,
                        tenant=_services._billing_identity(entry),
                        fetched=(kind, resource))
            except _billing.TenantMismatch:
                return _not_found()
        finally:
            con.close()
        return self._billing_complete(event_id, out["status"], out["reason"])

    async def _dispatch_collab(self, send, receive, headers: dict,
                               method: str, path: str, query: dict,
                               ctx, con, entry, session=None, control=None) -> None:
        import re
        from earwitness.app import services as _services
        body: dict = {}
        if method in ("POST", "PUT", "DELETE"):
            length = int(headers.get("content-length", "0") or "0")
            raw = await self._read_body(receive, length)
            if raw:
                body = _parse_json_body(raw)

        async def ok(payload: dict):
            await self._send(send, *_json_response(200, {"ok": True, **payload}))

        if path == "/api/v1/workspaces" and method == "POST":
            await ok(_services.collab_create_workspace(
                self.store, self.config.data_dir, ctx,
                body.get("workspace_id", ""), body.get("name", "")))
            return
        def _scoped(match) -> bool:
            # The path workspace must be the authenticated one; anything
            # else is unknown here (no cross-workspace effect, no leak).
            return bool(match) and match.group(1) == entry.get("workspace_id")

        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/members", path)
        if m and method == "GET" and _scoped(m):
            await ok(_services.collab_members(ctx, con, entry))
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites", path)
        if m and method == "GET" and _scoped(m):
            await ok(_services.collab_invites(ctx, con, entry))
            return
        if m and method == "POST" and _scoped(m):
            await ok(_services.collab_invite_create(
                ctx, con, entry, body.get("member", ""),
                body.get("role", "")))
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites/([^/]+)/deliver",
                          path)
        if m and method == "POST" and _scoped(m):
            await ok(_services.collab_invite_deliver(
                ctx, con, entry, urllib.parse.unquote(m.group(2))))
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites/([^/]+)/send",
                          path)
        if m and method == "POST" and _scoped(m):
            # Claim and outcome run here; only the provider HTTP call
            # leaves the event loop, so a slow provider never stalls
            # other requests.
            pending = _services.collab_invite_send(
                ctx, con, entry, urllib.parse.unquote(m.group(2)),
                self.invite_mailer)
            if pending.needs_transmit:
                outcome = await asyncio.to_thread(pending.transmit)
                await ok(pending.finish(con, outcome))
            else:
                await ok(pending.result)
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites/([^/]+)/withdraw",
                          path)
        if m and method == "POST" and _scoped(m):
            await ok(_services.collab_invite_withdraw(
                ctx, con, entry, urllib.parse.unquote(m.group(2))))
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites/([^/]+)/preflight",
                          path)
        if m and method == "GET" and _scoped(m):
            verified = ""
            if isinstance(session, dict) and session.get("email_verified"):
                verified = session.get("email") or ""
            await ok(_services.collab_invite_preflight(
                ctx, con, entry, urllib.parse.unquote(m.group(2)), verified))
            return
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/invites/([^/]+)/accept",
                          path)
        if m and method == "POST" and _scoped(m):
            if "verified_email" in body:
                raise ValueError("verified_email is server-derived and"
                                 " cannot be supplied by the client")
            verified = ""
            if isinstance(session, dict) and session.get("email_verified"):
                verified = session.get("email") or ""
            await ok(_services.collab_invite_accept(
                ctx, con, entry, urllib.parse.unquote(m.group(2)), verified,
                control=control, now=self.config.now()))
            return
        # ASGI already decoded the path once; the terminal principal is opaque.
        m = re.fullmatch(r"/api/v1/workspaces/([^/]+)/members/(.+)", path, re.DOTALL)
        if m and method == "PUT" and _scoped(m):
            # Personal agent tokens of the member are revoked in the same
            # control transaction (agent tokens review AGT-R1-01).
            await ok(_services.collab_role_change(
                ctx, con, entry, m.group(2),
                body.get("role", ""),
                self._hosted_revision(headers, body),
                control=control, now=self.config.now()))
            return
        if m and method == "DELETE" and _scoped(m):
            await ok(_services.collab_member_remove(
                ctx, con, entry, m.group(2), control=control, now=self.config.now()))
            return
        if path == "/api/v1/shares" and method == "POST":
            await ok(_services.collab_share_create(
                ctx, con, entry, body.get("object_type", ""),
                body.get("object_id", ""), body.get("audience", ""),
                body.get("fields", []), body.get("download") is True,
                body.get("requested_expiry"),
                body.get("source_policy_expiry")))
            return
        if path == "/api/v1/shares" and method == "GET":
            await ok(_services.collab_shares(ctx, con, entry))
            return
        m = re.fullmatch(r"/api/v1/shares/([^/]+)", path)
        if m and method == "GET":
            await ok(_services.collab_share_state(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        if m and method == "POST":
            await ok(_services.collab_share_revoke(
                ctx, con, entry, urllib.parse.unquote(m.group(1)),
                self._hosted_revision(headers, body)))
            return
        # Share-guest reads (guest ctx only; members use member paths).
        if path == "/api/v1/shared/search" and method == "GET":
            single = lambda k, d="": (query.get(k) or [d])[0]
            import json as _json
            try:
                filt = _json.loads(single("filter", "{}"))
            except ValueError:
                raise ValueError("filter must be JSON")
            await ok(_services.shared_search(ctx, con, entry, filt))
            return
        m = re.fullmatch(r"/api/v1/shared/evidence/([^/]+)", path)
        if m and method == "GET":
            await ok(_services.shared_evidence(
                ctx, con, entry, urllib.parse.unquote(m.group(1))))
            return
        if path == "/api/v1/shared/transcript" and method == "GET":
            single = lambda k, d="": (query.get(k) or [d])[0]
            try:
                offset = int(single("offset", "0"))
            except ValueError:
                raise ValueError("offset must be an integer")
            await ok(_services.shared_transcript(
                ctx, con, entry, single("episode_id", ""), offset))
            return
        await self._send(send, *_not_found())

    def openapi(self) -> dict:
        from earwitness import agents as _agents
        paths = {}
        for method, route, tool in AGENT_ROUTES:
            spec = next(s for s in _agents.TOOL_DEFS if s["name"] == tool)
            paths.setdefault(route, {})[method.lower()] = {
                "operationId": tool, "description": spec["description"],
                "requestBody": {"content": {"application/json": {
                    "schema": spec["schema"]}}},
                "responses": {"200": {"description": "agent envelope"}}}
        return {"openapi": "3.1.0",
                "info": {"title": "EarWitness hosted agent API",
                         "version": "1.0.0"},
                "servers": [{"url": self.config.public_base_url or
                             self.config.resource_uri}],
                "components": {"securitySchemes": {
                    "bearer": {"type": "http", "scheme": "bearer"}}},
                "security": [{"bearer": []}],
                "paths": paths}


def _new_request_id() -> str:
    """The current request's ID (so audit rows and logs share it), or a new
    one outside a request."""
    return _oplog.current_request_id() or _oplog.new_request_id()


def create_app(config: HostedConfig | None = None, error_sink=None):
    """ASGI factory (uvicorn --factory). Reads env when config is None.

    Refuses to start (ConfigError) on an unsafe configuration; see
    health.check_config. In production mode it also holds the
    single-process lock beside the control store. Error reporting stays
    off unless a sink is passed or EARWITNESS_ERROR_SINK names one.
    """
    env_config = config is None
    config = config or config_from_env()
    _health.require_valid_config(config)
    from . import billing as _billing
    try:
        # Billing settings are read per request; refuse to start on a bad
        # one here, where the operator sees the setting name, instead of
        # failing customer requests later.
        _billing.config_from_env()
    except ValueError as exc:
        raise _health.ConfigError(f"refusing to start: {exc}") from exc
    from . import ad_reads as _ad_reads
    try:
        _ad_reads.set_clip_guard(_ad_reads.clip_guard_setting(
            {_ad_reads.CLIP_GUARD_ENV: config.clip_guard}))
    except ValueError as exc:
        raise _health.ConfigError(f"refusing to start: {exc}") from exc
    from . import import_sources as _import_sources
    try:
        # DETBUILD: customer.py reads this per call; refuse a bad value here,
        # naming the setting and not the value.
        _import_sources.ad_rules_det_enabled()
    except _import_sources.ImportError as exc:
        raise _health.ConfigError(f"refusing to start: {exc}") from exc
    lock = None
    if config.mode == _health.PRODUCTION:
        lock = _health.acquire_single_process_lock(config.grants_db)
    try:
        if error_sink is None and config.error_sink:
            error_sink = _oplog.load_sink(config.error_sink)
        app = HostedApp(config)
        if env_config:
            import os
            from . import invites_send as _invites_send
            app.invite_mailer = _invites_send.mailer_from_env(os.environ)
    except BaseException:
        if lock is not None:
            lock.close()
        raise
    app._process_lock = lock
    app.error_reporter = _oplog.ErrorReporter(sink=error_sink)
    _oplog.configure_logging()
    _oplog.log_event("app.start", mode=config.mode,
                     error_reporting=app.error_reporter.enabled)
    from . import ask as _ask
    _ask.on_startup(app.store)   # turns left by the last process: working: failed RESTART; reserving: failed NOT_STARTED
    from . import meaning as _meaning
    _meaning.on_startup(app.store)   # runs left running: failed, RESTART
    from . import brief_check_live as _brief_check_live
    _brief_check_live.on_startup(app.store)   # presses left running: failed, RESTART
    warm_session_checks(app)
    check_view_state_guards(app)
    if config.read_warm_s > 0 and config.data_dir:
        app.read_warmer = ReadWarmer(app.store, config.data_dir, config.read_warm_s)
        app.read_warmer.start()
    return app


class ReadWarmer:
    """READ-CACHE warm-up. The server runs with --lifespan off and imports
    commit from other processes, so it cannot be told about new data: one
    daemon thread checks every registered workspace's data state (the
    view state, every alert row and the UTC day; about 10 ms) every
    interval_s seconds, and when it changed (at start, after pilot_import
    or pilot_sync commits, or after any write) fills the reads a member's
    first pages make (services.warm_class_reads).

    Which classes (SPEED-R1): at start, and whenever the research data
    changed (an import, the nightly window, a reseed, a correction, a
    review, an owner's ad review: customer_query.research_data_state, the
    research data token and the transcript index state, moved; SPEED-FIX1),
    every role group (ROLE_GROUPS: editor, owner, viewer browser classes;
    the plan is the workspace's own, filled in by each read). A campaign,
    brief, comment, share or invite moves research_query_state but not
    research_data_state: after it, and after any other change, only the
    classes in use refill: those a live session read a remembered route of
    in the last 30 minutes (customer_query.classes_in_use; a refill does
    not end the use, RC-R2-02), the writer's first (RC-SCR-R1-01) (RC-R1-01:
    a judge's saved-item write never waits on owner and viewer refills
    nobody reads). A new UTC day refills every role group's first pages
    too: Overview and the Alerts list key on the day (SPEED-FIX2, screen
    review SPV-C-03). Classes in use fill before the other role groups, at
    most _CLASS_LIMIT in all. Each filled class logs app.read_warm
    (workspace ID, role, seconds, CPU seconds).

    Then the five customer tasks' reads (services.warm_five_task_reads,
    SPEED-FIX1) for each role group the class cut kept (SPEED-FIX2,
    SPC-C-03), each logging app.read_warm_tasks (workspace ID, role,
    seconds, CPU seconds): at start and when the transcript index changed
    (the nightly window) at once; after any other research data change
    (an owner's ad review, an import) once the research data has been
    quiet for TASKS_QUIET_S, or at the latest TASKS_MAX_WAIT_S after the
    first change still unfilled, so a burst of reviews fills them once
    (SPEED-FIX2, SPS-C-04). Fills read a read-only snapshot and write
    nothing; a request that arrives mid-fill waits on that read's single
    flight instead of computing it again.

    Failures (RC-R1-04): each class is filled on its own, so one failing
    class never skips the others. A class its gate refuses (403, or 402
    when billing is on) has nothing to warm and is not retried until the
    data changes again. Any other failure is logged (event
    app.read_warm_failed: workspace ID, error class, class role) and that
    workspace is retried after interval_s x 2^n seconds (n failures in a
    row, at most retry_cap_s); a success resets the delay. Other
    workspaces carry on. A workspace whose state cannot be read (a view
    state guard missing: view_state_key None) fills nothing and logs
    app.read_warm_skipped (workspace ID, the parts that were None) once
    per change of those parts, or SKIP_LOG_S apart (UPD3-SLOW)."""

    retry_cap_s = 300.0
    # SPEED-R1: the browser role groups filled at start and after a
    # research data change, judges (editors) first.
    ROLE_GROUPS = ("editor", "owner", "viewer")
    # SPEED-FIX2 (SPS-C-04): after a research data change other than the
    # transcript index, the five-task pass waits until the research data
    # has been quiet this long (a burst of an owner's ad reviews fills it
    # once), and at most TASKS_MAX_WAIT_S after the first unfilled change.
    TASKS_QUIET_S = 60.0
    TASKS_MAX_WAIT_S = 600.0
    # UPD3-SLOW: an unchanged skip is logged again at most this often.
    SKIP_LOG_S = 600.0

    def __init__(self, store, data_dir: str, interval_s: float = 2.0):
        self.store, self.data_dir, self.interval_s = store, data_dir, interval_s
        self.clock = time.monotonic
        self._seen: dict = {}
        self._groups: dict = {}      # workspace -> research state every role group was filled at
        self._tasks: dict = {}       # workspace -> research state the five-task pass was filled at
        self._changed: dict = {}     # workspace -> (research state, first seen, last change seen)
        self._days: dict = {}        # workspace -> UTC day every role group was filled on
        self._retry: dict = {}       # workspace -> (failures in a row, next try at)
        self._skipped: dict = {}     # workspace -> (parts None, logged at)
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="ew-read-warm", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop checking and wait (at most timeout_s) for a fill in
        progress: fills stop between classes (RC-R1-05). The thread is a
        daemon, so a fill still running then cannot block process exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - logged; the next check retries
                _oplog.log_event("app.read_warm_failed", level="warning", workspace="",
                                 error=type(exc).__name__)
            self._stop.wait(self.interval_s)

    def tick(self) -> list:
        """One check of every registered workspace; the IDs filled."""
        filled = []
        for workspace_id in self.store.list_registered_workspaces():
            if self._stop.is_set():
                break
            failures, retry_at = self._retry.get(workspace_id, (0, 0.0))
            if failures and self.clock() < retry_at:
                continue
            try:
                done, failed = self._fill(workspace_id)
            except Exception as exc:  # noqa: BLE001 - logged; retried after a back-off
                done, failed = False, True
                _oplog.log_event("app.read_warm_failed", level="warning",
                                 workspace=workspace_id, error=type(exc).__name__)
            if failed:
                failures += 1
                delay = min(self.retry_cap_s, self.interval_s * 2 ** min(failures, 30))
                self._retry[workspace_id] = (failures, self.clock() + delay)
            else:
                self._retry.pop(workspace_id, None)
            if done:
                filled.append(workspace_id)
        return filled

    def _fill(self, workspace_id: str) -> tuple:
        """(filled any class, a class failed); a failure leaves the state
        unrecorded, so the retry fills the failed classes again."""
        from . import customer, customer_query, db
        from . import services as _services
        con, entry = _conn.open_registered_db(self.store, workspace_id, self.data_dir,
                                              readonly=True)
        try:
            with db.transaction(con, write=False):
                # The view state too: the saved items list keys on it, and
                # alerts_state leaves saved-item writes out (READ-CACHE-2).
                view = db.view_state_key(con)
                alerts = customer.alerts_state(con, day=True)
                # The research data state too: an index-only rebuild moves
                # no view state (db.VIEW_STATE_UNREAD) but every research
                # query's key (SPEED-FIX1).
                research = customer_query.research_data_state(con)
                state = None if view is None or alerts is None else (view, alerts, research)
            if state is None:
                # Nothing can be remembered (UPD3-SLOW: a guard missing makes
                # every research read cold): say so, never silently.
                self._log_skipped(workspace_id, tuple(
                    name for name, value in (("view_state_key", view),
                                             ("alerts_state", alerts),
                                             ("research", research)) if value is None))
                return False, False
            self._skipped.pop(workspace_id, None)
            tasks_due = self._tasks_due(workspace_id, research)
            if self._seen.get(workspace_id) == state and not tasks_due:
                return False, False
            first_pages = self._seen.get(workspace_id) != state
            database = next((row[2] for row in con.execute("PRAGMA database_list")
                             if row[1] == "main"), "")
            # class fields without the plan: the classes in use first.
            classes: list = []
            for _digest, fields in customer_query.classes_in_use(database):
                fields.pop("plan", None)
                if fields not in classes:
                    classes.append(fields)
            # SPEED-FIX2 (screen review SPV-C-03): Overview and the Alerts
            # list key on the UTC day, so a new day refills every role
            # group's first pages too, not only the classes in use.
            day = db.utcnow()[:10]
            everyone = first_pages and (research is None
                                        or self._groups.get(workspace_id) != research
                                        or self._days.get(workspace_id) != day)
            groups = [self._browser_class(workspace_id, entry, kind) for kind in self.ROLE_GROUPS]
            if everyone or tasks_due:
                for fields in groups:
                    if fields not in classes:
                        classes.append(fields)
            # The memo keeps _CLASS_LIMIT classes per database: filling more
            # would drop the first ones filled.
            classes = classes[:customer_query._CLASS_LIMIT]
            done = failed = False
            # Every class's first pages first, then the five-task reads of
            # the role groups the cut kept (SPEED-FIX2, SPC-C-03).
            passes = [(fields, _services.warm_class_reads, "app.read_warm")
                      for fields in classes] if first_pages else []
            tasks = [(fields, _services.warm_five_task_reads, "app.read_warm_tasks")
                     for fields in groups if fields in classes] if tasks_due else []
            for fields, warm, event in passes + tasks:
                if self._stop.is_set():
                    return done, True
                if failed and warm is _services.warm_five_task_reads:
                    break   # the retry fills the first pages, then these
                ctx = customer_query.class_context("warm", fields)
                ctx.db_path, ctx.db_identity = entry["db_path"], entry["db_identity"]
                started, cpu = time.monotonic(), time.thread_time()
                try:
                    with db.transaction(con, write=False):
                        warm(ctx, con, entry)
                    done = True
                except _access.AuthorizationError:
                    continue   # the gate refuses this class: nothing to warm
                except Exception as exc:  # noqa: BLE001 - logged; retried after a back-off
                    failed = True
                    _oplog.log_event("app.read_warm_failed", level="warning",
                                     workspace=workspace_id, error=type(exc).__name__,
                                     role=fields.get("principal_kind"))
                    continue   # the retry fills it again
                _oplog.log_event(event, workspace=workspace_id,
                                 role=fields.get("principal_kind"),
                                 seconds=round(time.monotonic() - started, 3),
                                 cpu_seconds=round(time.thread_time() - cpu, 3))
            if not failed:
                self._seen[workspace_id] = state
                if everyone and research is not None:
                    self._groups[workspace_id] = research
                if everyone:
                    self._days[workspace_id] = day
                if tasks_due:
                    self._tasks[workspace_id] = research
                    self._changed.pop(workspace_id, None)
            return done, failed
        finally:
            con.close()

    def _log_skipped(self, workspace_id: str, missing: tuple) -> None:
        """app.read_warm_skipped (warning): the workspace's state could not
        be read, so nothing is filled. Once per workspace until the parts
        that are None change, or again after SKIP_LOG_S, so a check every
        interval_s seconds does not flood the journal."""
        now = self.clock()
        last = self._skipped.get(workspace_id)
        if last is not None and last[0] == missing and now - last[1] < self.SKIP_LOG_S:
            return
        self._skipped[workspace_id] = (missing, now)
        _oplog.log_event("app.read_warm_skipped", level="warning",
                         workspace=workspace_id, missing=list(missing))

    def _tasks_due(self, workspace_id: str, research) -> bool:
        """Whether the five-task pass runs now (SPEED-FIX2, SPS-C-04): at
        start, and when the transcript index changed, at once; after any
        other research data change once it has been quiet TASKS_QUIET_S,
        or TASKS_MAX_WAIT_S after the first unfilled change. When the
        research state cannot be read (missing triggers), at start only."""
        if workspace_id not in self._tasks:
            return True
        filled = self._tasks[workspace_id]
        if research is None:
            return False
        if filled is None or filled[1] != research[1]:
            return True
        if filled == research:
            self._changed.pop(workspace_id, None)
            return False
        now = self.clock()
        seen, first, last = self._changed.get(workspace_id, (None, now, now))
        if seen != research:
            last = now
            self._changed[workspace_id] = (research, first, last)
        return now - last >= self.TASKS_QUIET_S or now - first >= self.TASKS_MAX_WAIT_S

    @staticmethod
    def _browser_class(workspace_id: str, entry: dict, kind: str) -> dict:
        """The class fields of a member's browser session (as
        browser_auth.build_session_context and the workspace route build
        it): the plan is filled in by the read itself."""
        from . import browser_auth, customer_query
        ctx = browser_auth.build_session_context(
            {"issuer": "warm", "subject": kind}, workspace_id, kind, "read-warm",
            scopes=("customer:read",) if kind == "viewer"
            else ("customer:read", "customer:write"))
        ctx.admitted_role = kind
        return {key: getattr(ctx, key) for key in (
            "workspace_id", "principal_kind", "admitted_role", "content_policy",
            "credential_kind", "authority_kind", "share_grant_id", "policy_identity",
            "policy_revision")} | {"scopes": sorted(ctx.scopes)}


def check_view_state_guards(app) -> None:
    """Read only, at start (UPD3-SLOW): every registered workspace whose
    view state guards are missing or altered logs app.view_state_guards_missing
    (warning: workspace ID and the table names). Such a workspace remembers
    no research read, so every Overview and Search is computed cold, until
    db.ensure_view_state_guards repairs it; this check never repairs or
    writes. A workspace it cannot read logs app.view_state_guards_check_failed
    (error class only) and start-up continues."""
    from . import db
    if not app.config.data_dir:
        return
    for workspace_id in app.store.list_registered_workspaces():
        try:
            con, _entry = _conn.open_registered_db(app.store, workspace_id,
                                                   app.config.data_dir, readonly=True)
            try:
                with db.transaction(con, write=False):
                    problems = db.view_state_guard_problems(con)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - logged; start-up continues
            _oplog.log_event("app.view_state_guards_check_failed", level="warning",
                             workspace=workspace_id, error=type(exc).__name__)
            continue
        if problems["missing"] or problems["altered"]:
            _oplog.log_event("app.view_state_guards_missing", level="warning",
                             workspace=workspace_id, missing=problems["missing"],
                             altered=problems["altered"])


# Start-up session checks run side by side (warm_session_checks).
SESSION_WARM_THREADS = 4


def warm_session_checks(app) -> None:
    """Compute the session check (customer.session_contract_marker: source
    qualification and pending definitions) of every registered workspace
    once, after the start-up checks and before the factory returns, so
    before uvicorn opens the port (DEMO2 pass 10). The results land in the
    per-process view-state memo (customer_query.view_state_memo) that the
    session route reads; nothing is written and no response changes.

    A failure is logged (event app.warm_failed: workspace ID and error
    class, never a path or message) and start-up continues: a failed
    computation is never remembered, so the request path computes again
    and fails exactly as it did without the warm-up. One unreadable
    workspace does not take the others down.

    Workspaces are warmed side by side, at most SESSION_WARM_THREADS at a
    time, each on its own read-only connection, so start-up does not
    grow with one workspace's time per workspace (DEMO2-R9-03); the log
    lines keep the registration order."""
    from concurrent.futures import ThreadPoolExecutor
    from . import customer, db
    if not app.config.data_dir:
        return
    workspaces = list(app.store.list_registered_workspaces())
    if not workspaces:
        return

    def warm(workspace_id):
        started = time.monotonic()
        try:
            con, _entry = _conn.open_registered_db(app.store, workspace_id,
                                                   app.config.data_dir, readonly=True)
            try:
                with db.transaction(con, write=False):
                    customer.session_contract_marker(con)
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - logged; the request path re-raises
            return exc, None
        return None, time.monotonic() - started
    with ThreadPoolExecutor(min(SESSION_WARM_THREADS, len(workspaces)),
                            "ew-session-warm") as pool:
        results = list(pool.map(warm, workspaces))
    for workspace_id, (exc, seconds) in zip(workspaces, results):
        if exc is not None:
            _oplog.log_event("app.warm_failed", level="warning", workspace=workspace_id,
                             error=type(exc).__name__)
            continue
        _oplog.log_event("app.warm", workspace=workspace_id, seconds=round(seconds, 3))

