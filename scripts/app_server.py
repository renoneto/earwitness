#!/usr/bin/env python3
"""EarWitness local application server: stdlib + SQLite, loopback only.

Serves app/ and same-origin /api/v1. Mutable state lives in
.earwitness/workspace.sqlite3 (or --workspace). Never touches output/,
labels/, transcripts/, data/, experiments/, historical reports, or review/.

Usage: python3 scripts/app_server.py [--workspace .earwitness] [--port 8770]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import ipaddress
import inspect
import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earwitness.app import db, exports as exports_mod, import_sources as importer
from earwitness.app import agent_contracts as agent_mod
from earwitness.app import customer as customer_mod
from earwitness.app import customer_contracts as customer_contracts_mod
from earwitness.app import customer_query as customer_query_mod
from earwitness.app import citations as citations_mod
from earwitness.app import brand_suggest as brand_suggest_mod
from earwitness.app import corrections as corrections_mod
from earwitness.app import access as access_mod
from earwitness.app import media as media_mod
from earwitness.app import public_pricing as public_pricing_mod
from earwitness.app import ops
from earwitness.app import search as search_mod
from earwitness.pipeline import acquisition as _acquisition
from earwitness.pipeline import artifacts as _artifacts
from earwitness.pipeline import jobs as _jobs

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8770
BODY_LIMIT = 2_000_000
IMPORT_BODY_LIMIT = 8_000_000
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
STATIC = {"index.html": "text/html; charset=utf-8",
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
# Self-hosted IBM Plex (SIL OFL 1.1): exact file names only.
_PLEX = ("IBMPlexSans-400", "IBMPlexSans-500", "IBMPlexSans-600",
         "IBMPlexSans-700", "IBMPlexSans-Italic-400", "IBMPlexSerif-400",
         "IBMPlexSerif-500", "IBMPlexSerif-600", "IBMPlexSerif-Italic-400",
         "IBMPlexMono-400", "IBMPlexMono-500", "IBMPlexMono-600")
STATIC.update({f"fonts/{face}-{subset}.woff2": "font/woff2"
               for face in _PLEX for subset in ("latin", "latin-ext")})
# Separate dark public entry (owned assets live in app/public/, owned by
# the public worker). Customer code never serves these; public pages
# never boot the customer session.
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
PUBLIC_DIR = REPO / "app" / "public"
# One self-only static policy for every served document, script and
# style: no inline handlers, eval or CDN. Frozen report artifacts keep
# their own stricter sandbox below.
STATIC_CSP = ("default-src 'self'; style-src 'self';"
              " media-src 'self' https: blob:; connect-src 'self';"
              " object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

_state = {"workspace": REPO / ".earwitness", "port": DEFAULT_PORT,
          "csrf": secrets.token_hex(16)}


def workspace() -> Path:
    return _state["workspace"]


def open_db(*, readonly=False):
    return db.open_existing(workspace() / "workspace.sqlite3", readonly=readonly)


def local_view_context() -> str:
    """Opaque local view token bound to the open workspace directory."""
    return hashlib.sha256(
        f"local|{workspace()}|customer-view".encode("utf-8")).hexdigest()[:32]


class AppError(Exception):
    """HTTP failure with a named wire code shared with the hosted adapter.

    code defaults from the status when a raise site has no finer
    semantic; details carries extra named fields (current_revision).
    """
    def __init__(self, status: HTTPStatus, message: str, code: str | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code or _STATUS_CODES.get(status, "UNKNOWN")
        self.details = dict(details or {})


_STATUS_CODES = {
    HTTPStatus.BAD_REQUEST: "INVALID_ARGUMENT",
    HTTPStatus.UNAUTHORIZED: "UNAUTHENTICATED",
    HTTPStatus.FORBIDDEN: "FORBIDDEN",
    HTTPStatus.NOT_FOUND: "NOT_FOUND",
    HTTPStatus.CONFLICT: "CONFLICT",
    HTTPStatus.PRECONDITION_FAILED: "CONFLICT",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    HTTPStatus.UNPROCESSABLE_ENTITY: "INVALID_ARGUMENT",
    HTTPStatus.PRECONDITION_REQUIRED: "PRECONDITION_REQUIRED",
    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE: "OUT_OF_RANGE",
    HTTPStatus.TOO_MANY_REQUESTS: "RATE_LIMITED",
    HTTPStatus.INTERNAL_SERVER_ERROR: "INTERNAL",
}


EVIDENCE_ID_RE = r"[A-Za-z0-9:_.\-]{1,128}"
EPISODE_ID_RE = r"[A-Za-z0-9][A-Za-z0-9_:.\-]{0,127}"
# Customer campaign reads rewrite explicit synthetic ids to this
# never-real id so backends reproduce their exact unknown-id
# status/body. Route regexes match only (\d+), and SQLite
# AUTOINCREMENT rows are >= 1, so -1 can never be a real campaign.
_UNKNOWN_CAMPAIGN_SENTINEL = -1


def _seg(raw) -> str:
    """Decode one path parameter exactly once, at the route boundary."""
    if not isinstance(raw, str):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid path parameter")
    try:
        return unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid URL encoding")


def _evidence_id(raw) -> str:
    value = _seg(raw)
    if not re.fullmatch(EVIDENCE_ID_RE, value):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid evidence id")
    return value


def _episode_id(raw) -> str:
    value = _seg(raw)
    if not re.fullmatch(EPISODE_ID_RE, value):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid episode id")
    return value


def _create_customer_placement(con, cid: int, body: dict, actor: str = "owner") -> dict:
    """Operation-specific placement creation shared by the legacy and
    customer namespaces. Both adapters call this authorized service
    directly; the hosted path never proxies into the loopback server."""
    from earwitness.app import campaigns as _camp
    scope = body.get("scope", "show_window")
    show_id = body.get("show_id", "")
    episode_id = body.get("episode_id")
    date_from = body.get("date_from")
    date_to = body.get("date_to")
    tzname = body.get("timezone", "UTC")
    expected_count = body.get("expected_count", 1)
    requirements = body.get("requirements", {})
    provenance = body.get("provenance")
    priority = body.get("priority", 0)
    if scope not in ("episode", "show_window"):
        raise AppError(HTTPStatus.BAD_REQUEST, "unknown placement scope")
    if not isinstance(show_id, str) or len(show_id) > 200:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "show_id must be a string (max 200 chars)")
    if episode_id is not None and (
            not isinstance(episode_id, str) or not episode_id):
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "episode_id must be a non-empty string or null")
    for key in ("date_from", "date_to"):
        val = body.get(key)
        if val is not None and (
                not isinstance(val, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val)):
            raise AppError(HTTPStatus.BAD_REQUEST,
                           f"{key} must be YYYY-MM-DD or null")
    if not isinstance(tzname, str) or not tzname or len(tzname) > 64:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "timezone must be a non-empty IANA name")
    if not isinstance(requirements, dict):
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "requirements must be an object")
    if provenance is not None and not isinstance(provenance, dict):
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "provenance must be an object")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "priority must be an integer")
    try:
        return _camp.save_expected_placement(
            con, cid, scope, show_id, episode_id, date_from, date_to,
            tzname, expected_count, requirements,
            provenance or {}, priority, actor)
    except ops.Archived as exc:
        raise AppError(HTTPStatus.CONFLICT, str(exc), "CAMPAIGN_ARCHIVED")
    except ops.BadInput as exc:
        raise AppError(HTTPStatus.BAD_REQUEST, str(exc))


# -- P09 processing center (thin additive mapping; P00 integration review) --
# Owned by the P09P10 leaf. Purely additive: no existing route is modified.
# All run state flows through earwitness.pipeline.jobs (P01P04 ledger);
# URL shape vocabulary is shared with acquisition.validate_enclosure_url
# (no second vocabulary). The server never launches a worker.
P09_STAGES = ("fetch", "transcribe", "screen", "refine")
P09_SUPPORTED_PROFILES = ("local",)
P09_RUN_ID_RE = r"run_[0-9a-f]{12}"
P09_AUDIO_MIMES = {
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-wav": ".wav",
    "audio/wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac",
    "audio/aac": ".aac",
}
P09_AUDIO_MAX_BYTES = 256 * 1024 * 1024
P09_AUDIO_MAX_DURATION_S = 4 * 3600.0
P09_RUN_PAGE_LIMIT = 50


def _p09_job_errors(fn, *args, **kwargs):
    """Map pipeline ledger errors onto the product HTTP contract."""
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise AppError(HTTPStatus.NOT_FOUND, f"unknown run or job: {exc}")
    except _jobs.ConflictError as exc:
        text = str(exc)
        if "stale" in text or "revision" in text:
            raise AppError(HTTPStatus.PRECONDITION_FAILED, text)
        raise AppError(HTTPStatus.CONFLICT, text)
    except (_jobs.BlockedError, _jobs.LeaseExpired, _jobs.FencingMismatch,
            _jobs.CancelledError) as exc:
        raise AppError(HTTPStatus.CONFLICT, str(exc))
    except (_jobs.JobError, _acquisition.AcquisitionError,
            _artifacts.ArtifactError) as exc:
        raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
    except sqlite3.Error as exc:
        raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR,
                       f"database error: {exc}; run python3 scripts/app_workspace.py"
                       " upgrade --workspace <workspace> first")


def _p09_require(con):
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"p01_idempotency", "p01_job_deps", "p01_reconcile_log", "p01_job_profiles",
            "p01p04_holds", "p01p04_retention", "p09_feed_sources", "p09_run_episodes"} <= tables:
        raise AppError(HTTPStatus.CONFLICT, "processing schema requires explicit workspace setup")
    if "stage_key" not in {row[1] for row in con.execute("PRAGMA table_info(processing_jobs)")}:
        raise AppError(HTTPStatus.CONFLICT, "processing schema requires explicit upgrade")


def _p09_config_digest(profile: str, config) -> str:
    body = json.dumps({"profile": profile, "config": config or {}},
                      sort_keys=True)
    return hashlib.sha256(("p09-v1|" + body).encode()).hexdigest()


def _p09_stage_digest(episode_id: str, stage: str, config_digest: str) -> str:
    return hashlib.sha256(
        f"{episode_id}|{stage}|{config_digest}".encode()).hexdigest()


def _p09_episodes_or_400(con, episode_ids) -> list:
    if not isinstance(episode_ids, list) or not episode_ids \
            or len(episode_ids) > 200:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "episode_ids must be a non-empty list (max 200)")
    clean = []
    for raw in episode_ids:
        if not isinstance(raw, str) or not re.fullmatch(EPISODE_ID_RE, raw):
            raise AppError(HTTPStatus.BAD_REQUEST, f"invalid episode id: {raw!r}"[:120])
        clean.append(raw)
    rows = con.execute(
        "SELECT id FROM episodes WHERE id IN (%s)" % ",".join("?" * len(clean)),
        clean).fetchall()
    known = {r["id"] for r in rows}
    unknown = [e for e in clean if e not in known]
    if unknown:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       f"unknown episodes: {', '.join(unknown[:5])}")
    return clean


def _p09_profile_or_400(profile) -> str:
    if profile not in P09_SUPPORTED_PROFILES:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "unsupported profile %r: this build runs the local worker only "
                       "(supported: %s). No provider was switched; pick 'local' or "
                       "leave the run queued." % (profile, ", ".join(P09_SUPPORTED_PROFILES)))
    return profile


def _p09_reuse(con, stage: str, digest: str) -> bool:
    row = con.execute("SELECT 1 FROM processing_jobs WHERE workspace_id='local'"
                      " AND stage=? AND input_digest=? AND state='succeeded' LIMIT 1",
                      (stage, digest)).fetchone()
    return row is not None


def _p09_build_specs(con, episode_ids: list, profile: str, config_digest: str,
                     scope: str = ""):
    specs, estimate = [], []
    for i, episode_id in enumerate(episode_ids):
        parent = None
        for stage in P09_STAGES:
            job_id = f"{scope}e{i}-{stage}" if scope else f"e{i}-{stage}"
            digest = _p09_stage_digest(episode_id, stage, config_digest)
            reused = _p09_reuse(con, stage, digest)
            specs.append({"job_id": job_id, "stage": stage,
                          "input_digest": digest,
                          "parents": [] if parent is None else [parent],
                          "backend_profile": profile, "cost_estimate": 0.0,
                          "episode_id": episode_id})
            estimate.append({"episode_id": episode_id, "stage": stage,
                             "reused": reused})
            parent = job_id
    return specs, estimate


def _p09_estimate_payload(con, episode_ids: list, profile: str, config) -> dict:
    config_digest = _p09_config_digest(profile, config)
    specs, rows = _p09_build_specs(con, episode_ids, profile, config_digest)
    new = sum(1 for r in rows if not r["reused"])
    return {"episodes": episode_ids, "profile": profile,
            "config_digest": config_digest, "stages": list(P09_STAGES),
            "stage_count": len(specs), "reusable": len(specs) - new,
            "new_stages": new, "reserved_cost": 0.0,
            "cost_note": "Local profile performs no paid calls: reserved 0.00; "
                         "provider-reported usage stays unknown until a paid "
                         "profile is supported.",
            "retention_note": "Matching media is retained only where permitted; "
                              "uploads land as private staged artifacts.",
            "rows": rows}


def _p09_validate_source_url(url: str) -> str:
    """Shared scheme vocabulary + fail-closed network screening, no fetch."""
    try:
        cleaned = _acquisition.validate_enclosure_url(url)
    except _acquisition.AcquisitionError as exc:
        raise AppError(HTTPStatus.BAD_REQUEST, f"rejected source URL: {exc}")
    parts = urlparse(cleaned)
    if parts.username or parts.password:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "rejected source URL: credentials in URL are not permitted")
    host = parts.hostname or ""
    if host.lower() in ("metadata.google.internal", "metadata.google"):
        raise AppError(HTTPStatus.BAD_REQUEST,
                       "rejected source URL: metadata destination denied")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443,
                                   type=socket.SOCK_STREAM)
    except OSError:
        raise AppError(HTTPStatus.BAD_REQUEST,
                       f"rejected source URL: host does not resolve: {host}"[:120])
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise AppError(HTTPStatus.BAD_REQUEST,
                           "rejected source URL: unparseable resolved address")
        if not addr.is_global:
            raise AppError(HTTPStatus.BAD_REQUEST,
                           "rejected source URL: private/link-local/reserved "
                           "destinations are denied")
    return cleaned


def _p09_operator_command(profile: str) -> str:
    # NOTE (P09P10 integration flag): the product plan shows
    # `work --workspace <ws> --profile <profile>`, but the frozen P01P04 CLI
    # only parses --workspace as a global flag BEFORE the subcommand. The UI
    # shows the order that actually runs; the plan text needs the same fix.
    ws = workspace()
    try:
        rel = ws.relative_to(REPO)
    except ValueError:
        rel = ws
    return "python3 scripts/app_worker.py --workspace %s work --profile %s" % (
        shlex.quote(str(rel)), shlex.quote(profile))


def _p09_liveness(con, run_id: str) -> dict:
    """Newest heartbeat among live-claimed jobs vs the lease window (120s)."""
    rows = con.execute("SELECT a.heartbeat_at FROM job_attempts a JOIN "
                       "processing_jobs j ON j.id=a.job_id WHERE j.run_id=? AND "
                       "j.state='running' ORDER BY a.heartbeat_at DESC LIMIT 1",
                       (run_id,)).fetchone()
    if rows is None:
        live = con.execute("SELECT 1 FROM processing_jobs WHERE run_id=? AND "
                           "state IN ('queued','running','retry_wait') LIMIT 1",
                           (run_id,)).fetchone()
        return {"worker_live": False, "last_heartbeat": None,
                "queued_without_worker": bool(live)}
    try:
        last = _jobs._parse_ts(rows["heartbeat_at"])
    except (ValueError, TypeError):
        last = 0.0
    import time as _time
    age = _time.time() - last
    return {"worker_live": age <= _jobs.DEFAULT_LEASE_S,
            "last_heartbeat": rows["heartbeat_at"],
            "heartbeat_age_s": round(age, 1),
            "queued_without_worker": False}


def _p09_run_payload(con, run_id: str) -> dict:
    if not re.fullmatch(P09_RUN_ID_RE, run_id):
        raise AppError(HTTPStatus.BAD_REQUEST, "invalid run id")
    try:
        detail = _jobs.run_detail(con, run_id)
    except KeyError:
        raise AppError(HTTPStatus.NOT_FOUND, "unknown run")
    selection = detail.get("selection") or {}
    stages = selection.get("stages") or []
    profiles = sorted({s.get("backend_profile", "") for s in stages if s.get("backend_profile")})
    episodes = []
    for s in stages:
        row = con.execute("SELECT episode_id FROM p09_run_episodes WHERE run_id=? AND "
                          "job_id=?", (run_id, s.get("job_id", ""))).fetchone()
        if row is not None and row["episode_id"] not in episodes:
            episodes.append(row["episode_id"])
    jobs = detail.get("jobs") or []
    per_episode = {}
    for job in jobs:
        row = con.execute("SELECT episode_id FROM p09_run_episodes WHERE run_id=? AND "
                          "job_id=?", (run_id, job["id"])).fetchone()
        ep = row["episode_id"] if row else "unknown"
        per_episode.setdefault(ep, []).append(
            {"stage": job["stage"], "state": job["state"],
             "failure_class": job.get("failure_class")})
    succeeded_eps = [ep for ep, js in per_episode.items()
                     if js and all(j["state"] == "succeeded" for j in js)]
    gaps = [ep for ep in episodes if ep not in succeeded_eps]
    reconcile = []
    for op in detail.get("paid_operations") or []:
        if op.get("disposition") in ("unknown", "ambiguous",
                                     "reconciliation_required"):
            reconcile.append({
                "operation_id": op["id"], "job_id": op.get("job_id"),
                "disposition": op.get("disposition"),
                "action": "reconcile",
                "operator_command":
                    "python3 scripts/app_worker.py --workspace %s reconcile "
                    "--operation %s --disposition <confirmed_success|"
                    "confirmed_nonacceptance|still_unknown> "
                    "--expected-revision <rev> --idempotency-key <key> "
                    "--as <owner|operator> --receipt '<operation-linked evidence>'" % (
                        shlex.quote(str(workspace())),
                        shlex.quote(op["id"])),
                "note": "Owner/operator reconciliation with operation-linked "
                        "evidence is required; ordinary retry cannot release "
                        "this exposure."})
    liveness = _p09_liveness(con, run_id)
    required = profiles[0] if len(profiles) == 1 else ",".join(profiles)
    payload = {"run": detail["summary"], "jobs": jobs,
               "attempts": [{k: v for k, v in a.items()
                             if k != "lease_token"}
                            for a in detail.get("attempts") or []],
               "episodes": episodes, "per_episode": per_episode,
               "coverage_gaps": gaps, "required_profile": required or "local",
               "profiles": profiles, "paid_operations": detail.get("paid_operations") or [],
               "reconcile": reconcile, "worker": liveness,
               "operator_command": _p09_operator_command(required or "local")}
    return payload


def _p09_probe_duration(path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        value = float((out.stdout or "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


class Handler(BaseHTTPRequestHandler):
    server_version = "EarWitnessApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("app_server: " + fmt % args + "\n")

    # -- plumbing -----------------------------------------------------
    def send_response(self, code, message=None):
        self._response_status = int(code)
        return super().send_response(code, message)

    def _request_db(self):
        return self._product_connection if hasattr(self, "_product_connection") else open_db()

    def _close_db(self, con):
        if con is not getattr(self, "_product_connection", None):
            con.close()

    def _fresh_local_admission(self, ctx):
        from earwitness.app import access
        snapshot = open_db(readonly=True)
        try:
            with db.transaction(snapshot, write=False):
                identity = snapshot.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
                if identity is None or identity[0] != ctx.db_identity:
                    raise access.AuthorizationError("workspace identity changed", 403)
                for source_key, field, exporting in ctx.disclosures:
                    ctx.authorize_disclosure(snapshot, source_key, [field], export=exporting)
                ctx.authorize_recording_captures(snapshot)
        finally:
            snapshot.close()

    def _protected_dispatch(self, route, *args, write):
        """Own product commit and withhold complete HTTP bytes until fresh admission.

        This is explicit loopback operator trust, not browser/reader authority.
        There is no mutable credential in this lane; a write is admitted under
        its write lock, a read against a fresh committed snapshot taken after
        its route ran. Prepared reader control remains separate.
        """
        from earwitness.app import access
        class BufferedFailure(Exception):
            pass
        transport = self.wfile
        buffered = io.BytesIO()
        con = open_db(readonly=not write)
        identity = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        if identity is None:
            con.close()
            raise AppError(HTTPStatus.FORBIDDEN, "workspace identity unavailable")
        ctx = access.AccessContext.create(principal_id="owner", principal_kind="owner",
            workspace_id="local", credential_kind="local_operator", db_identity=identity[0])
        self._product_connection, self._request_ctx = con, ctx
        # Files the route created for this request: deleted unless the
        # transaction commits, and while the write lock is still held, so
        # no concurrent request can have adopted them (no orphan, and no
        # deleting another request's bytes).
        self._request_created, committed = [], False

        def drop_created():
            for path in self._request_created:
                path.unlink(missing_ok=True)
            self._request_created.clear()
        self.wfile = buffered
        try:
            try:
                with db.transaction(con, write=write):
                    try:
                        route(*args)
                        if getattr(self, "_response_status", 500) >= 400:
                            raise BufferedFailure()
                        if write:
                            self._fresh_local_admission(ctx)
                            for source_key, field, exporting in ctx.disclosures:
                                ctx.authorize_disclosure(con, source_key, [field], export=exporting)
                    except BaseException:
                        drop_created()
                        raise
                committed = True
                if not write:
                    # A read is admitted against the latest committed state
                    # (its own snapshot); it never waits for, or holds off,
                    # a writer: a long rule removal does not stall pages.
                    self._fresh_local_admission(ctx)
                    transport.write(buffered.getvalue())
                    return
            except BufferedFailure:
                pass
            transport.write(buffered.getvalue())
        except access.AuthorizationError as exc:
            raise AppError(HTTPStatus(exc.status), str(exc), "FORBIDDEN") from exc
        finally:
            if not committed and self._request_created:
                # Only a failed COMMIT gets here, after the lock was
                # released: delete under a fresh write lock, and only a
                # file no committed row references.
                try:
                    with db.transaction(con, write=True):
                        for path in self._request_created:
                            if con.execute("SELECT 1 FROM media_artifacts WHERE locator=?",
                                           ("staged/" + path.name,)).fetchone() is None:
                                path.unlink(missing_ok=True)
                except Exception:
                    pass
            self.wfile = transport
            del self._product_connection, self._request_ctx, self._request_created
            con.close()

    def _guard_host(self):
        host = (self.headers.get("Host") or "").strip()
        if host not in (f"127.0.0.1:{_state['port']}", f"localhost:{_state['port']}"):
            raise AppError(HTTPStatus.FORBIDDEN, "forbidden host")

    def _guard_mutation(self):
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, _state["csrf"]):
            raise AppError(HTTPStatus.FORBIDDEN, "missing or invalid CSRF token")
        origin = self.headers.get("Origin", "")
        if origin != "http://" + self.headers.get("Host", ""):
            raise AppError(HTTPStatus.FORBIDDEN, "bad origin")

    def _read_json(self, limit: int = BODY_LIMIT):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length <= 0:
            self._cached_body = {}
            return {}
        if length > limit:
            raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        try:
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=importer._no_dup_keys,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeDecodeError, ValueError, importer.ImportError):
            raise AppError(HTTPStatus.BAD_REQUEST, "invalid JSON body")
        if not isinstance(body, dict):
            raise AppError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        strings = ("name", "status", "brand", "date_from", "date_to", "message", "offer",
                   "notes", "manual_conclusion", "manual_reason", "manual_verdict", "judgment",
                   "evidence_id", "episode_id", "origin_note", "audio_url", "media_id",
                   "alignment", "format", "title", "page_density", "check_type", "show_title")
        for key in strings:
            if key in body and body[key] is not None and not isinstance(body[key], str):
                raise AppError(HTTPStatus.BAD_REQUEST, f"{key} must be a string or null")
        for key in ("campaign_id", "campaign_revision", "expected_revision", "transcript_version_id"):
            if key in body and body[key] is not None and (isinstance(body[key], bool) or not isinstance(body[key], int)):
                raise AppError(HTTPStatus.BAD_REQUEST, f"{key} must be an integer or null")
        self._cached_body = body
        return body

    def _send_json(self, payload, status=HTTPStatus.OK, download_name=None):
        if isinstance(payload, (bytes, str)):
            body = payload if isinstance(payload, bytes) else payload.encode()
            ctype = "application/octet-stream"
        else:
            if status == HTTPStatus.OK and isinstance(payload, dict) \
                    and payload.get("ok") is True \
                    and "workspace_context" not in payload \
                    and urlparse(self.path).path.startswith(
                        "/api/v1/customer/"):
                # Every successful customer response carries the
                # server-constructed view context; the server never
                # trusts a client-supplied tenant/context value.
                payload = {"workspace_context": local_view_context(),
                           **payload}
            body = json.dumps(payload, ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", STATIC_CSP)
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: HTTPStatus, message: str, code: str | None = None,
              details: dict | None = None):
        self._send_json({"ok": False,
                         "error": {"code": code or _STATUS_CODES.get(
                             status, "UNKNOWN"), "message": message,
                                   "status": int(status),
                                   "details": dict(details or {})}},
                        status=status)

    def _fail_internal(self, exc):
        """Last-resort JSON 500: no residual error may drop the connection.
        A database still locked by another change after the busy timeout is
        a 503 the page can retry."""
        try:
            if isinstance(exc, sqlite3.OperationalError) and any(
                    word in str(exc).lower() for word in ("locked", "busy")):
                return self._fail(HTTPStatus.SERVICE_UNAVAILABLE,
                                  "The workspace is busy with another change; try again in a moment.",
                                  code="BUSY")
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR,
                       "internal error", code="INTERNAL")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _op_errors(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ops.MissingPrecondition as exc:
            raise AppError(HTTPStatus.PRECONDITION_REQUIRED, str(exc))
        except ops.Conflict as exc:
            raise AppError(HTTPStatus.PRECONDITION_FAILED,
                           f"stale revision; current is {exc.current_revision}",
                           "CONFLICT",
                           {"current_revision": exc.current_revision})
        except ops.Archived as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "CAMPAIGN_ARCHIVED")
        except ops.NotFound as exc:
            raise AppError(HTTPStatus.NOT_FOUND, str(exc), "NOT_FOUND")
        except corrections_mod.ConfirmNeeded as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "CONFIRM_REQUIRED", exc.details)
        except corrections_mod.ReadChanged as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "READ_CHANGED", exc.details)
        except corrections_mod.NeedsUpgrade as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "UPGRADE_REQUIRED")
        except (ops.BadInput, importer.ImportError) as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, str(exc))

    def _customer_errors(self, fn, *args, **kwargs):
        try:
            if hasattr(self, "_request_ctx") and "ctx" in inspect.signature(fn).parameters:
                kwargs.setdefault("ctx", self._request_ctx)
            return fn(*args, **kwargs)
        except access_mod.AuthorizationError as exc:
            raise AppError(HTTPStatus(exc.status), str(exc), "FORBIDDEN")
        except ops.MissingPrecondition as exc:
            raise AppError(HTTPStatus.PRECONDITION_REQUIRED, str(exc))
        except ops.Conflict as exc:
            raise AppError(HTTPStatus.PRECONDITION_FAILED,
                           f"stale revision; current is {exc.current_revision}",
                           "CONFLICT",
                           {"current_revision": exc.current_revision})
        except customer_mod.WindowChanged as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "VIEW_CHANGED")
        except customer_contracts_mod.SelectorConflict as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "VIEW_CHANGED")
        except customer_contracts_mod.SelectorUnavailable as exc:
            raise AppError(HTTPStatus.NOT_FOUND, "saved scope is unavailable",
                           "SCOPE_UNAVAILABLE")
        except ops.Archived as exc:
            raise AppError(HTTPStatus.CONFLICT, str(exc), "CAMPAIGN_ARCHIVED")
        except ops.NotFound as exc:
            raise AppError(HTTPStatus.NOT_FOUND, str(exc), "NOT_FOUND")
        except citations_mod.RevisionUnavailable as exc:
            raise AppError(HTTPStatus.NOT_FOUND, "requested source is unavailable", "SOURCE_UNAVAILABLE")
        except citations_mod.CitationError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, str(exc), "INVALID_ARGUMENT")
        except corrections_mod.RateLimited as exc:
            raise AppError(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
        except (ops.BadInput, importer.ImportError, search_mod.FilterRejected) as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, str(exc))

    def _review_media(self, con, item: dict) -> dict | None:
        """The review page's audio: the read's evidence clip, played from
        the read start, else the episode's media (a remote stream)."""
        from earwitness.app import ad_reads as _ad
        clip = _ad.clip_for(con, item["evidence_id"])
        try:
            ref = citations_mod.current_source_ref(con, "evidence", item["evidence_id"]) \
                if clip else None
        except citations_mod.RevisionUnavailable:
            ref = None
        if clip and ref:
            return {"kind": "evidence_clip", "label": "Evidence clip",
                    "src": "/api/v1/customer/records/" + quote(ref, safe="") + "/media",
                    "source_offset_s": clip["start_s"], "trust": "exact"}
        ep = con.execute("SELECT * FROM episodes WHERE id=?", (item["episode_id"],)).fetchone()
        return media_mod.media_for_episode(dict(ep), REPO) if ep else None

    def _customer_visible_campaign(self, con, campaign_id: int) -> int:
        """Campaign id customer reads should hand to backends.

        Explicit synthetic campaigns are rewritten to a never-real id
        so every downstream unknown-id status/body is reproduced
        exactly. Unknown ids pass through untouched, so their natural
        backend errors are unchanged. Operator routes never call this.
        """
        try:
            visible = customer_mod.is_customer_campaign(con, campaign_id)
        except ops.BadInput:
            return campaign_id
        if visible:
            return campaign_id
        return _UNKNOWN_CAMPAIGN_SENTINEL

    def _customer_query_from_params(self, qs: dict) -> dict:
        from earwitness.app import customer_contracts
        if set(qs) - {"query", "group_by"} or any(len(values) != 1 for values in qs.values()):
            raise AppError(HTTPStatus.BAD_REQUEST, "customer research URL carries one Query2 JSON query")
        try:
            raw = json.loads((qs.get("query") or ["{}"])[0], object_pairs_hook=importer._no_dup_keys,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, importer.ImportError):
            raise AppError(HTTPStatus.BAD_REQUEST, "invalid Query2 JSON")
        return self._customer_errors(customer_contracts.normalize_query, raw)

    def _customer_query2_from_body(self, query) -> dict:
        from earwitness.app import customer_contracts
        return self._customer_errors(customer_contracts.normalize_query, query)

    def _deny_tracker_authority_fields(self, body) -> None:
        # Client verification authority is never accepted on trackers;
        # deny before any write (K08).
        if not isinstance(body, dict):
            return
        for key in ("verification_context", "verified_via"):
            if key in body:
                raise AppError(HTTPStatus.BAD_REQUEST,
                               f"unsupported tracker field: {key}")
        delivery = body.get("delivery")
        if isinstance(delivery, dict):
            for key in ("verification_context", "verified_via"):
                if key in delivery:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   f"unsupported tracker field: {key}")


    def _send_report_html(self, body: str, digest: str, download: bool,
                          report_no, style_src: str = "") -> None:
        data = body.encode()
        if hashlib.sha256(data).hexdigest() != digest:
            raise AppError(HTTPStatus.INTERNAL_SERVER_ERROR,
                           "stored report digest mismatch")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Frozen standalone artifact: sandboxed, no scripts or external refs.
        # A new-freeze report also allows its own style block by hash.
        self.send_header("Content-Security-Policy",
                         "sandbox; default-src 'none'; base-uri 'none';"
                         " form-action 'none'; frame-ancestors 'self'"
                         + (f"; {style_src}" if style_src else ""))
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="earwitness-report-{report_no}.html"')
        self.end_headers()
        self.wfile.write(data)

    # -- routing ------------------------------------------------------
    def do_GET(self):
        try:
            self._guard_host()
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path in CANONICAL_REDIRECTS:
                dest = CANONICAL_REDIRECTS[path]
                if parsed.query:
                    dest += "?" + parsed.query
                return self._redirect(dest)
            if path in PUBLIC_ROUTES:
                return self._public(PUBLIC_ROUTES[path])
            if path == "/favicon.ico":
                # No icon art: an empty answer stops the browser's
                # default request from logging a 404 on every page.
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Security-Policy", STATIC_CSP)
                self.end_headers()
                return
            # Exact names only, on the raw path (the hosted app matches the
            # decoded one); the file opened is always the table's own name.
            if path.startswith("/public/") and path[len("/public/"):] in PUBLIC_ASSETS:
                return self._public(path[len("/public/"):])
            if path == "/app/":
                return self._static("index.html")
            if path in ("/app/operator", "/app/operator/"):
                return self._static("operator/index.html")
            if path.startswith("/app/operator/"):
                name = "operator/" + path[len("/app/operator/"):]
                if name[len("operator/"):] in STATIC:
                    return self._static(name)
                return self._public("404.html",
                                    status=HTTPStatus.NOT_FOUND)
            if path.startswith("/app/"):
                name = path[len("/app/"):]
                if name in STATIC:
                    return self._static(name)
                return self._public("404.html",
                                    status=HTTPStatus.NOT_FOUND)
            if path.startswith("/media/"):
                return self._media(path[len("/media/"):])
            if not path.startswith("/api/v1"):
                return self._public("404.html",
                                    status=HTTPStatus.NOT_FOUND)
            return self._protected_dispatch(self._route_get, path[len("/api/v1"):], qs, write=False)
        except AppError as exc:
            self._fail(exc.status, str(exc), exc.code, exc.details)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail_internal(exc)

    def _write_guard(self):
        self._guard_host()
        self._guard_mutation()
        if not urlparse(self.path).path.startswith("/api/v1/"):
            raise AppError(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_POST(self):
        try:
            self._write_guard()
            parsed = urlparse(self.path)
            path = parsed.path[len("/api/v1"):]
            return self._protected_dispatch(self._route_post, path, None,
                                            write=path != "/customer/research-query")
        except AppError as exc:
            self._fail(exc.status, str(exc), exc.code, exc.details)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail_internal(exc)

    def do_PUT(self):
        try:
            self._write_guard()
            parsed = urlparse(self.path)
            return self._protected_dispatch(self._route_put, parsed.path[len("/api/v1"):], None, write=True)
        except AppError as exc:
            self._fail(exc.status, str(exc), exc.code, exc.details)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail_internal(exc)

    def do_DELETE(self):
        try:
            return self._protected_dispatch(self._delete_route, write=True)
        except AppError as exc:
            self._fail(exc.status, str(exc), exc.code, exc.details)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail_internal(exc)

    def _delete_route(self):
        try:
            self._write_guard()
            parsed = urlparse(self.path)
            path = parsed.path[len("/api/v1"):]
            # Read the JSON body once, as the hosted server does: DELETE
            # routes take expected_revision (and ref) from it or If-Match.
            body = self._read_json()
            m = re.fullmatch(r"/admin/rules/(\d+)", path)
            if m:
                # Undo one ad review rule; it re-applies at once. Count
                # first: without the count shown (expected_reads) it is a
                # dry run answered 409 CONFIRM_REQUIRED with the count.
                con = self._request_db()
                try:
                    out = self._op_errors(corrections_mod.remove_rule, con, m.group(1), "owner",
                                          expected_reads=body.get("expected_reads"))
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/evidence/([^/]+)/review", path)
            if m:
                con = self._request_db()
                try:
                    out = self._op_errors(ops.clear_review, con, _evidence_id(m.group(1)),
                                          self._expected_revision_from_body(), "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/watchlists/(\d+)", path)
            if m:
                con = self._request_db()
                try:
                    out = self._op_errors(ops.delete_watchlist, con, int(m.group(1)),
                                          self._expected_revision_from_body(), "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/saved-items/([^/]+)", path)
            if m:
                from earwitness.app import customer_saved as _saved
                con = self._request_db()
                try:
                    with db.transaction(con, write=True):
                        out = self._customer_errors(
                            _saved.delete_item, con, unquote(m.group(1)),
                            self._expected_revision_from_body(),
                            actor="owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/targets/([^/]+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                con = self._request_db()
                try:
                    with db.transaction(con, write=True):
                        tid = unquote(m.group(1))
                        cid = self._customer_errors(
                            _camp.target_campaign, con, tid)
                        out = self._customer_errors(
                            _camp.delete_target, con, cid, tid, "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/evidence/([^/]+)", path)
            if m:
                con = self._request_db()
                try:
                    out = self._op_errors(ops.detach_evidence, con, int(m.group(1)),
                                          _evidence_id(m.group(2)), self._expected_revision_from_body(), "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/placements/([^/]+)/evidence", path)
            if m:
                from earwitness.app import campaigns as _camp
                con = self._request_db()
                try:
                    cid = self._customer_visible_campaign(con, int(m.group(1)))
                    if not isinstance(body.get("ref"), str) or not body["ref"]:
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "ref must be a nonempty string")
                    out = self._customer_errors(
                        _camp.remove_placement_evidence, con, cid,
                        unquote(m.group(2)), body["ref"],
                        self._expected_revision_from_body(), "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/observations/([^/]+)/fields/([^/]+)", path)
            if m:
                from earwitness.app import observations as _obs
                con = self._request_db()
                try:
                    out = self._op_errors(
                        _obs.clear_observation_field, con, unquote(m.group(1)),
                        unquote(m.group(2)),
                        self._expected_revision_from_body(), "owner")
                finally:
                    self._close_db(con)
                return self._send_json({"ok": True, **out})
            return self._fail(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except AppError as exc:
            self._fail(exc.status, str(exc), exc.code, exc.details)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._fail_internal(exc)


    # -- static + media -----------------------------------------------
    def _static(self, name: str):
        target = (REPO / "app" / name).resolve()
        if (REPO / "app").resolve() not in target.parents:
            return self._public("404.html", status=HTTPStatus.NOT_FOUND)
        try:
            data = target.read_bytes()
        except OSError:
            return self._public("404.html", status=HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type",
                         STATIC.get(name) or STATIC[name.rsplit("/", 1)[-1]])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", STATIC_CSP)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, dest: str, status=HTTPStatus.FOUND):
        self.send_response(status)
        self.send_header("Location", dest)
        self.send_header("Content-Security-Policy", STATIC_CSP)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _public(self, name: str, status=HTTPStatus.OK):
        ctype = PUBLIC_DOCS.get(name, PUBLIC_ASSETS.get(name))
        if ctype is None:
            return self._public("404.html", status=HTTPStatus.NOT_FOUND)
        target = (PUBLIC_DIR / name).resolve()
        if PUBLIC_DIR.resolve() not in target.parents:
            return self._public("404.html", status=HTTPStatus.NOT_FOUND)
        try:
            data = target.read_bytes()
        except OSError:
            return self._public("404.html", status=HTTPStatus.NOT_FOUND)
        if name == PRICING_DOC:
            data = public_pricing_mod.render(data, PUBLIC_DIR)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", STATIC_CSP)
        self.end_headers()
        self.wfile.write(data)

    def _media(self, media_id: str):
        try:
            media_id = _seg(media_id)
        except AppError:
            return self._fail(HTTPStatus.NOT_FOUND, "unknown media")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", media_id or ""):
            return self._fail(HTTPStatus.NOT_FOUND, "unknown media")
        spec = media_mod.resolve(REPO, media_id)
        if spec is None:
            return self._fail(HTTPStatus.NOT_FOUND, "unknown media")
        size = spec["size"]
        start, end, status = 0, size - 1, HTTPStatus.OK
        range_hdr = (self.headers.get("Range") or "").strip()
        if range_hdr:
            m = re.match(r"bytes=(\d*)-(\d*)$", range_hdr)
            if not m or (not m.group(1) and not m.group(2)):
                return self._fail(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "invalid Range")
            if not m.group(1):
                start, end = max(0, size - int(m.group(2))), size - 1
            else:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            if start >= size or end < start:
                return self._fail(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "range out of bounds")
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        try:
            with open(spec["path"], "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
        except OSError:
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "media read failed")
        self.send_response(status)
        self.send_header("Content-Type", spec["mime"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self.wfile.write(data)

    # -- GET routes ----------------------------------------------------
    def _route_get(self, path: str, qs: dict):
        con = self._request_db()
        try:
            m = re.fullmatch(r"/customer/records/(.+)/media", path)
            if m:
                served = self._customer_errors(media_mod.recording_chunk, con,
                    unquote(m.group(1)), self.headers.get("Range"))
                self.send_response(served["status"])
                self.send_header("Content-Type", served["mime"])
                self.send_header("Content-Length", str(len(served["payload"])))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if served["content_range"]:
                    self.send_header("Content-Range", served["content_range"])
                self.end_headers()
                self.wfile.write(served["payload"])
                return
            if path == "/customer/search":
                criteria = self._customer_query_from_params(qs)
                out = self._customer_errors(customer_mod.query, con, criteria,
                                            REPO)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/records/(.+)/mistake", path)
            if m:
                ref = unquote(m.group(1))
                from earwitness.app import customer_saved as _saved
                out = self._customer_errors(_saved.list_notes, con, ref=ref)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/records/(.+)", path)
            if m:
                ref = unquote(m.group(1))
                single = lambda k, d="": (qs.get(k) or [d])[0]
                if {"start_s", "end_s", "cursor", "limit"} & set(qs):
                    try:
                        limit = int(single("limit", "100"))
                    except ValueError:
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "limit must be an integer")
                    try:
                        start_s = float(single("start_s", "")) \
                            if single("start_s", "") else None
                        end_s = float(single("end_s", "")) \
                            if single("end_s", "") else None
                    except ValueError:
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "start_s/end_s must be numbers")
                    with db.transaction(con, write=False):
                        out = self._customer_errors(
                            customer_mod.record_window, con, ref,
                            start_s, end_s,
                            single("cursor", "") or None, limit,
                            local_view_context())
                    return self._send_json({"ok": True, "record": out})
                out = self._customer_errors(customer_mod.record_window, con, ref,
                                            workspace_context=local_view_context())
                return self._send_json({"ok": True, "record": out})
            if path == "/customer/overview":
                return self._send_json({"ok": True,
                                        **self._customer_errors(customer_mod.overview, con)})
            if path == "/customer/brands":
                q = (qs.get("q") or [""])[0]
                return self._send_json({"ok": True, "brands":
                                        self._customer_errors(customer_mod.brands_directory,
                                                              con, q)})
            m = re.fullmatch(r"/customer/brands/(.+)", path)
            if m:
                out = self._customer_errors(customer_mod.brand_detail, con,
                                            unquote(m.group(1)))
                return self._send_json({"ok": True, "brand": out})
            if path == "/customer/shows":
                q = (qs.get("q") or [""])[0]
                return self._send_json({"ok": True, "shows":
                                        self._customer_errors(customer_mod.customer_shows,
                                                              con, q)})
            m = re.fullmatch(r"/customer/shows/([^/]+)/artwork", path)
            if m:
                show_id = unquote(m.group(1))
                served = self._customer_errors(
                    customer_mod.show_artwork_bytes, con, show_id)
                if served is None:
                    return self._fail(HTTPStatus.NOT_FOUND,
                                      "unknown artwork")
                mime, raw = served
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(raw)
                return
            m = re.fullmatch(r"/customer/episodes/([^/]+)", path)
            if m:
                with db.transaction(con, write=False):
                    out = self._customer_errors(customer_mod.episode_reads, con,
                                                unquote(m.group(1)))
                return self._send_json({"ok": True, "episode": out})
            m = re.fullmatch(r"/customer/shows/(.+)", path)
            if m:
                show_id = unquote(m.group(1))
                out = self._customer_errors(customer_mod.show_detail,
                                            con, show_id)
                return self._send_json({"ok": True, **out})
            if path == "/customer/summary":
                criteria = self._customer_query_from_params(qs)
                group_by = (qs.get("group_by") or ["week"])[0]
                out = self._customer_errors(customer_mod.summary, con,
                                            criteria, group_by)
                return self._send_json({"ok": True, **out})
            if path == "/customer/compare":
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    brands = json.loads(single("brands", "[]"))
                    raw_a = single("period_a", "")
                    period_a = json.loads(raw_a) if raw_a else None
                    raw_b = single("period_b", "")
                    period_b = json.loads(raw_b) if raw_b else None
                    raw_q = single("query", "")
                    wire_query = json.loads(raw_q) if raw_q else None
                    raw_s = single("selector", "")
                    wire_selector = json.loads(raw_s) if raw_s else None
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "compare requires JSON brands/periods")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        customer_query_mod.compare_bundle, con, brands,
                        period_a, period_b, wire_query, wire_selector,
                        local_view_context(), REPO)
                return self._send_json({"ok": True, **out})
            if path == "/customer/filter-values":
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = int(single("limit", "50"))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        customer_query_mod.filter_values, con,
                        single("field", ""), single("q", ""),
                        single("cursor", "") or None, limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/creatives/(.+)", path)
            if m:
                out = self._customer_errors(customer_mod.creative_detail, con,
                                            unquote(m.group(1)))
                return self._send_json({"ok": True, "creative": out})
            if path == "/customer/saved-items":
                from earwitness.app import customer_saved as _saved
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = int(single("limit", "20"))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _saved.list_items, con,
                        item_type=single("type", "") or None,
                        q=single("q", ""),
                        cursor=single("cursor", "") or None, limit=limit,
                        workspace_context=local_view_context())
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/saved-items/([^/]+)", path)
            if m:
                from earwitness.app import customer_saved as _saved
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _saved.get_item, con, unquote(m.group(1)),
                        workspace_context=local_view_context())
                return self._send_json({"ok": True, **out})
            if path == "/customer/mistakes":
                from earwitness.app import customer_saved as _saved
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = int(single("limit", "20"))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _saved.list_notes, con, q=single("q", ""),
                        ref=single("ref", "") or None,
                        cursor=single("cursor", "") or None, limit=limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/mistakes/([^/]+)", path)
            if m:
                from earwitness.app import customer_saved as _saved
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _saved.get_note, con, unquote(m.group(1)))
                return self._send_json({"ok": True, **out})
            if path == "/customer/alerts":
                return self._send_json({"ok": True, "alerts":
                                        self._customer_errors(customer_mod.list_alerts,
                                                              con)})
            m = re.fullmatch(r"/customer/alerts/(\d+)", path)
            if m:
                # summaries=0: callers that do not show event summaries (F-16).
                # page: the page of matches, as the hosted route (PBA-R1-01).
                page = self._customer_errors(customer_mod.alert_page_number,
                                             (qs.get("page") or [None])[0])
                out = self._customer_errors(customer_mod.get_alert, con, int(m.group(1)),
                                            summaries=(qs.get("summaries") or ["1"])[0] != "0",
                                            page=page)
                return self._send_json({"ok": True, "alert": out})
            if path == "/customer/preferences":
                return self._send_json({"ok": True, "preferences":
                                        self._customer_errors(
                                            customer_mod.get_preferences, con)})
            if path == "/customer/reader-status":
                return self._send_json({"ok": True, **self._customer_errors(
                    customer_mod.reader_status, con)})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/draft", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                out = self._customer_errors(_camp.build_customer_draft, con,
                                            cid)
                return self._send_json({"ok": True, "draft": out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/evidence-fit", path)
            if m:
                # Which placements would take this read (UAT-092): the
                # attach rule run read-only, before the user submits.
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                out = self._customer_errors(_camp.placement_evidence_fit, con, cid,
                                            (qs.get("ref") or [""])[0])
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/reports/(\d+)/html", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                download = (qs.get("download") or [""])[0] == "1"
                body, digest = self._customer_errors(
                    _camp.get_customer_report_html, con, cid, int(m.group(2)))
                style_src = _camp.report_style_src(con, cid, int(m.group(2)), body)
                return self._send_report_html(body, digest, download, m.group(2), style_src)
            m = re.fullmatch(r"/customer/campaigns/(\d+)/reports", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                out = self._customer_errors(_camp.list_customer_reports, con,
                                            cid)
                return self._send_json({"ok": True, "reports": out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/reports/(\d+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                out = self._customer_errors(_camp.get_customer_report, con,
                                            cid, int(m.group(2)))
                return self._send_json({"ok": True, **out})
            if path == "/customer/campaigns":
                from earwitness.app import campaigns as _camp
                with db.transaction(con, write=False):
                    rows = _camp.customer_list_summaries(con, [
                        c for c in ops.list_campaigns(con, ctx=self._request_ctx)
                        if isinstance(c, dict) and c.get("id") is not None
                        and customer_mod.is_customer_campaign(con, c["id"])])
                return self._send_json({"ok": True, "campaigns": rows})
            m = re.fullmatch(r"/customer/campaigns/(\d+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                d = ops.campaign_detail(con, cid, ctx=self._request_ctx)
                if d is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown campaign")
                with db.transaction(con, write=False):
                    dto = _camp.campaign_projection(con, d)
                return self._send_json({"ok": True, "campaign": dto})
            if path == "/customer/briefs":
                from earwitness.app import briefs as _briefs
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = int(single("limit", "20"))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _briefs.list_briefs, con, single("q", ""),
                        single("cursor", "") or None, limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/briefs/([^/]+)/versions/([^/]+)", path)
            if m:
                from earwitness.app import briefs as _briefs
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _briefs.get_version, con, unquote(m.group(1)),
                        unquote(m.group(2)))
                return self._send_json({"ok": True, "brief_version": out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/targets", path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        _camp.list_targets, con, cid)
                return self._send_json({"ok": True, "targets": out})
            if path == "/session":
                return self._send_json({
                    "ok": True, "csrf_token": _state["csrf"],
                    "aggregate_rev": db.aggregate_revision(con),
                    "fts_available": db.fts_available(con),
                    "workspace_context": local_view_context(),
                    "frontend_contract": customer_mod.local_session_contract_marker(
                        con)})
            if path == "/overview":
                return self._send_json({"ok": True, **ops.overview(con)})
            if path == "/admin/review-queue":
                # Operator ad review queue (corrections module): needs-review
                # reads and member reports, most impact first.
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = max(1, min(1000, int(single("limit", "200"))))
                except ValueError:
                    return self._fail(HTTPStatus.BAD_REQUEST, "limit must be an integer")
                with db.transaction(con, write=False):
                    out = self._op_errors(corrections_mod.queue, con, show=single("show"),
                                          brand=single("brand"), reason=single("reason"),
                                          limit=limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/admin/review-queue/([^/]+)", path)
            if m:
                eid = _evidence_id(m.group(1))
                with db.transaction(con, write=False):
                    item = corrections_mod.queue_item(con, eid)
                    if item is None:
                        return self._fail(HTTPStatus.NOT_FOUND, "unknown evidence")
                    item["media"] = self._review_media(con, item)
                return self._send_json({"ok": True, "item": item})
            if path == "/admin/brands":
                with db.transaction(con, write=False):
                    out = corrections_mod.known_brands(con, (qs.get("q") or [""])[0])
                return self._send_json({"ok": True, "brands": out})
            if path == "/admin/rules":
                with db.transaction(con, write=False):
                    self._op_errors(corrections_mod.require_tables, con)
                    out = corrections_mod.export(con)
                    out["rule_ids"] = {
                        (f"self_promo\t{r['value']['brand']}\t{r['value']['network']}"
                         f"\t{int(bool(r['value'].get('exact')))}" if r["kind"] == "self_promo"
                         else f"{r['kind']}\t{r['key']}"): r["id"]
                        for r in corrections_mod.load(con)["rows"]}
                # Labels still in force: an undone or superseded label is
                # one the pipeline skips (D-04).
                out["labels"] = sum(1 for lab in out["labels"] if not lab.get("superseded"))
                out["reviewed_reads"] = len(out["reviewed_reads"])
                return self._send_json({"ok": True, "rules": out})
            if path == "/admin/brand-suggestions":
                # Stored brand suggestions for the Ad review panel (loopback
                # operator only; none of it reaches a customer route).
                with db.transaction(con, write=False):
                    self._op_errors(corrections_mod.require_tables, con)
                    out = brand_suggest_mod.suggestions(con)
                return self._send_json({"ok": True, "suggestions": out})
            if path == "/review-list":
                # Operator review list: lone rule candidates and unnamed Jev
                # windows nobody has decided yet (validation plan A2).
                from earwitness.app import ad_reads as _ad_reads
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = max(1, min(500, int(single("limit", "100"))))
                    offset = max(0, int(single("offset", "0")))
                except ValueError:
                    return self._fail(HTTPStatus.BAD_REQUEST, "limit/offset must be integers")
                with db.transaction(con, write=False):
                    out = _ad_reads.review_list(con, limit=limit, offset=offset)
                return self._send_json({"ok": True, **out})
            if path == "/evidence":
                filt = {k: v[0] if len(v) == 1 and k not in ("source", "review", "brand_basis")
                        else v for k, v in qs.items()}
                for key in ("source", "review", "brand_basis"):
                    if key in qs:
                        filt[key] = qs[key]
                try:
                    filt = agent_mod.parse_browser_evidence_params(filt)
                except agent_mod.AgentError as exc:
                    return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, **ops.search_evidence(con, filt)})
            m = re.fullmatch(r"/evidence/([^/]+)", path)
            if m:
                d = ops.evidence_detail(con, _evidence_id(m.group(1)))
                if d is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown evidence")
                # The load's read facts (brand display, needs-review, the
                # namer's other candidates) for the operator review page.
                d["read"] = customer_mod.read_detail(con, d["id"])
                return self._send_json({"ok": True, "evidence": d})
            m = re.fullmatch(r"/evidence/([^/]+)/transcript", path)
            if m:
                d = ops.evidence_detail(con, _evidence_id(m.group(1)))
                if d is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown evidence")
                return self._send_json({"ok": True, "episode_id": d["episode_id"],
                                        "segments": ops.transcript_segments(con, d["episode_id"], d["transcript_version_id"])})
            if path == "/episodes":
                q = (qs.get("q") or [""])[0]
                if not isinstance(q, str) or len(q) > 200:
                    return self._fail(HTTPStatus.BAD_REQUEST,
                                      "q exceeds 200 characters")
                return self._send_json({"ok": True, "episodes": ops.shows_index(con, q)})
            m = re.fullmatch(r"/episodes/([^/]+)/transcript", path)
            if m:
                eid = _episode_id(m.group(1))
                ep = con.execute("SELECT transcript_version_id FROM episodes WHERE id=?", (eid,)).fetchone()
                if ep is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown episode")
                version = (qs.get("version") or [ep["transcript_version_id"]])[0]
                if not con.execute("SELECT 1 FROM source_versions WHERE id=? AND source_key=?",
                                   (version, f"transcript:{eid}")).fetchone():
                    return self._send_json({"ok": True, "segments": [], "version_id": None})
                return self._send_json({"ok": True, "version_id": version,
                                        "segments": ops.transcript_segments(con, eid, version)})
            m = re.fullmatch(r"/sources/(\d+)", path)
            if m:
                row = con.execute("SELECT * FROM source_versions WHERE id=?", (int(m.group(1)),)).fetchone()
                if row is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown source version")
                record = dict(row)
                record["payload"] = json.loads(record["payload"])
                return self._send_json({"ok": True, "source": record})
            m = re.fullmatch(r"/episodes/([^/]+)", path)
            if m:
                try:
                    ep_id = _episode_id(m.group(1))
                except AppError as exc:
                    return self._fail(exc.status, str(exc), exc.code, exc.details)
                row = con.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
                if row is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown episode")
                ep = dict(row)
                if ep.get("alias_of") and not ep.get("audio_url") \
                        and not ep.get("media_id"):
                    # Pre-inheritance row: exact-transcript alias whose own
                    # audio is null serves the target's supplied enclosure
                    # stream. Bounded: single-row lookup, URL reuse only.
                    target = con.execute(
                        "SELECT audio_url FROM episodes WHERE id=?",
                        (ep["alias_of"],)).fetchone()
                    if target is not None and target["audio_url"]:
                        ep["audio_url"] = target["audio_url"]
                ep["media"] = media_mod.media_for_episode(ep, REPO)
                pilot = media_mod.pilot_media_id(ep.get("id") or "")
                if pilot is not None and not ep.get("audio_url") \
                        and media_mod.resolve_pilot(REPO, pilot) is not None:
                    ep["audio_url"] = f"/media/{pilot}"
                bench = media_mod.bench_media_id(ep.get("id") or "")
                if bench is not None and not ep.get("audio_url") \
                        and media_mod.resolve_bench(REPO, bench) is not None:
                    ep["audio_url"] = f"/media/{bench}"
                ep["evidence_count"] = ops.search_evidence(con, {"episode": ep_id})["total"]
                rec = ops.get_hidden_shows(con).get(ep.get("show_title") or "")
                ep["hidden"] = bool(isinstance(rec, dict) and rec.get("hidden") is True)
                ep["show_revision"] = int(rec.get("revision", 0)) if isinstance(rec, dict) else 0
                return self._send_json({"ok": True, "episode": ep})
            if path == "/shows/hidden":
                return self._send_json({"ok": True,
                                        "hidden": [{"show_title": t,
                                                    "revision": r.get("revision", 0),
                                                    "updated_at": r.get("updated_at")}
                                                   for t, r in sorted(ops.get_hidden_shows(con).items())
                                                   if isinstance(r, dict) and r.get("hidden") is True]})
            if path == "/brands":
                q = (qs.get("q") or [""])[0]
                if not isinstance(q, str) or len(q) > 200:
                    return self._fail(HTTPStatus.BAD_REQUEST,
                                      "q exceeds 200 characters")
                return self._send_json({"ok": True, "brands": ops.brands_index(con, q)})
            m = re.fullmatch(r"/brands/([^/]+)", path)
            if m:
                brand = _seg(m.group(1)).strip().lower()
                if not brand:
                    return self._fail(HTTPStatus.BAD_REQUEST, "invalid brand")
                where, args = ops._filter_where(
                    ops.normalize_filter({}),
                    exclude_shows=tuple(ops.hidden_show_titles(con)), con=con)
                rows = con.execute("SELECT a.evidence_id, a.basis, a.origin, a.channel, e.episode_id,"
                                   " e.orig_start_s, e.orig_end_s FROM brand_assocs a"
                                   " JOIN evidence e ON e.id=a.evidence_id"
                                   " JOIN episodes ep ON ep.id=e.episode_id WHERE a.brand=?"
                                   " AND a.basis<>'model' AND " + where,  # PODS: model rows are display-only
                                   [brand, *args]).fetchall()
                return self._send_json({"ok": True, "brand": brand,
                                        "assocs": [dict(r) for r in rows]})
            if path == "/watchlists":
                return self._send_json({"ok": True,
                                        "watchlists": ops.list_watchlists(con)})
            m = re.fullmatch(r"/watchlists/(\d+)/evaluate", path)
            if m:
                try:
                    out = ops.evaluate_watchlist(con, int(m.group(1)))
                except ops.NeedsCorrection as exc:
                    return self._send_json({"ok": False, "error": str(exc),
                                            "needs_correction": exc.reasons},
                                           status=HTTPStatus.BAD_REQUEST)
                except ops.BadInput as exc:
                    return self._fail(HTTPStatus.NOT_FOUND, str(exc))
                return self._send_json({"ok": True, **out})
            if path == "/campaigns":
                return self._send_json({"ok": True, "campaigns": ops.list_campaigns(con)})
            m = re.fullmatch(r"/campaigns/(\d+)", path)
            if m:
                d = ops.campaign_detail(con, int(m.group(1)))
                if d is None:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown campaign")
                return self._send_json({"ok": True, "campaign": d})
            m = re.fullmatch(r"/exports/(\d+)", path)
            if m:
                try:
                    with db.transaction(con, write=False):
                        out = exports_mod.load_export_bytes(con, int(m.group(1)), getattr(self, "_request_ctx", None))
                except exports_mod.DigestMismatch as exc:
                    return self._fail(HTTPStatus.CONFLICT, str(exc))
                except ops.BadInput:
                    return self._fail(HTTPStatus.NOT_FOUND, "unknown export")
                ext = {"csv": "csv", "html": "html"}.get(out["format"], "json")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", out["content_type"])
                self.send_header("Content-Disposition",
                                 f'attachment; filename="earwitness-export-{out["export_id"]}.{ext}"')
                self.send_header("X-Export-Digest",
                                 f"sha256={out['digest']}")
                body = out["payload"].encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/customer/archive":
                single = lambda k, d="": (qs.get(k) or [d])[0]
                try:
                    limit = int(single("limit", "20"))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer")
                campaign = single("campaign_id", "")
                try:
                    campaign_id = int(campaign) if campaign else None
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "campaign_id must be an integer")
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        exports_mod.archive_collection, con,
                        kind=single("kind", "") or None,
                        campaign_id=campaign_id,
                        q=single("q", ""),
                        cursor=single("cursor", "") or None, limit=limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/exports/(\d+)", path)
            if m:
                # Declared customer download path: same route, body,
                # headers and error semantics on both adapters. The
                # operator /exports/<id> path is preserved unchanged.
                try:
                    with db.transaction(con, write=False):
                        out = exports_mod.load_export_bytes(
                            con, int(m.group(1)), getattr(self, "_request_ctx", None))
                except exports_mod.DigestMismatch as exc:
                    return self._fail(HTTPStatus.CONFLICT, str(exc),
                                      "VIEW_CHANGED")
                except ops.BadInput:
                    return self._fail(HTTPStatus.NOT_FOUND,
                                      "unknown export", "NOT_FOUND")
                ext = {"csv": "csv", "html": "html"}.get(out["format"],
                                                         "json")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", out["content_type"])
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="earwitness-export-'
                    f'{out["export_id"]}.{ext}"')
                self.send_header("X-Export-Digest",
                                 f"sha256={out['digest']}")
                body = out["payload"].encode("utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/audit":
                try:
                    limit = min(200, max(1, int((qs.get("limit") or ["50"])[0])))
                except ValueError:
                    limit = 50
                rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
                return self._send_json({"ok": True, "audit": [dict(r) for r in rows]})
            if path == "/backup":
                import tempfile
                tmp = Path(tempfile.mkdtemp(dir=workspace())) / "workspace-backup.sqlite3"
                try:
                    info = ops.backup_to(con, tmp)
                    data = tmp.read_bytes()
                finally:
                    try:
                        tmp.unlink()
                        tmp.parent.rmdir()
                    except OSError:
                        pass
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 'attachment; filename="earwitness-workspace.sqlite3"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/settings":
                return self._send_json({"ok": True, "settings": ops.get_settings(con)})
            if path == "/processing/sources":
                _p09_require(con)
                feeds = [dict(r) for r in con.execute(
                    "SELECT show_title, COUNT(*) AS episodes FROM episodes GROUP BY "
                    "show_title ORDER BY show_title LIMIT 200").fetchall()]
                sources = [dict(r) for r in con.execute(
                    "SELECT id, url, host, created_at FROM p09_feed_sources WHERE "
                    "workspace_id='local' ORDER BY created_at DESC LIMIT 200").fetchall()]
                return self._send_json({
                    "ok": True, "registered_inputs": feeds, "feed_sources": sources,
                    "uploads": {
                        "audio": {"mime_types": sorted(P09_AUDIO_MIMES),
                                  "max_bytes": P09_AUDIO_MAX_BYTES,
                                  "max_duration_s": P09_AUDIO_MAX_DURATION_S},
                        "transcript": {"mime_types": ["application/json"],
                                       "note": "Transcript JSON uploads use the existing "
                                               "/api/v1/imports/upload endpoint."}},
                    "validation": "RSS locators share acquisition.validate_enclosure_url "
                                  "scheme vocabulary plus fail-closed DNS/address screening "
                                  "(private/link-local/reserved/metadata denied); "
                                  "content-type/bytes/time are enforced by the bounded "
                                  "operator fetch, never at registration."})
            m = re.fullmatch(r"/processing/runs", path)
            if m:
                _p09_require(con)
                try:
                    limit = min(P09_RUN_PAGE_LIMIT,
                                max(1, int((qs.get("limit") or ["20"])[0])))
                except ValueError:
                    limit = 20
                ids = [r["id"] for r in con.execute(
                    "SELECT id FROM processing_runs WHERE workspace_id='local' ORDER BY "
                    "created_at DESC LIMIT ?", (limit,)).fetchall()]
                runs = [_p09_job_errors(_jobs.run_summary, con, rid) for rid in ids]
                return self._send_json({"ok": True, "runs": runs})
            m = re.fullmatch(r"/processing/runs/(" + P09_RUN_ID_RE + r")", path)
            if m:
                _p09_require(con)
                return self._send_json(
                    {"ok": True, **_p09_run_payload(con, m.group(1))})
            m = re.fullmatch(r"/processing/worker", path)
            if m:
                _p09_require(con)
                profile = (qs.get("profile") or ["local"])[0]
                run_id = (qs.get("run") or [None])[0]
                live = {"worker_live": False, "last_heartbeat": None,
                        "queued_without_worker": False}
                if run_id and re.fullmatch(P09_RUN_ID_RE, run_id):
                    try:
                        live = _p09_liveness(con, run_id)
                    except KeyError:
                        pass
                return self._send_json({
                    "ok": True, "profile": profile,
                    "supported_profiles": list(P09_SUPPORTED_PROFILES),
                    "lease_window_s": _jobs.DEFAULT_LEASE_S, **live,
                    "operator_command": _p09_operator_command(profile),
                    "note": "The server never launches workers. Queued work waits "
                            "for the operator command above."})
            # -- PSURF additive GET surfaces (384/407/408/540/604) --
            # Purely additive: no existing route above is modified.
            if path == "/creatives":
                from earwitness.app import creatives as _cr
                _cr._ensure_aux(con)
                try:
                    limit = min(100, max(1, int((qs.get("limit") or ["50"])[0])))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer 1..100")
                rows = con.execute("SELECT id, revision, status, created_at, updated_at"
                                   " FROM creatives ORDER BY updated_at DESC LIMIT ?",
                                   (limit,)).fetchall()
                return self._send_json({"ok": True, "creatives": [dict(r) for r in rows]})
            m = re.fullmatch(r"/creatives/suggest", path)
            if m:
                from earwitness.app import creatives as _cr
                oid = (qs.get("observation_id") or [""])[0]
                if not isinstance(oid, str) or not re.fullmatch(EVIDENCE_ID_RE, oid):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "observation_id is required (1..128 chars)")
                try:
                    limit = min(50, max(1, int((qs.get("limit") or ["20"])[0])))
                except ValueError:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "limit must be an integer 1..50")
                out = self._op_errors(_cr.suggest_memberships, con, oid, None, limit)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/creatives/([^/]+)", path)
            if m:
                from earwitness.app import creatives as _cr
                cid = _seg(m.group(1))
                if not re.fullmatch(EVIDENCE_ID_RE, cid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid creative id")
                out = self._op_errors(_cr.creative_service_payload, con, cid)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/observations/([^/]+)", path)
            if m:
                from earwitness.app import observations as _obs
                oid = _seg(m.group(1))
                if not re.fullmatch(EVIDENCE_ID_RE, oid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid observation id")
                try:
                    out = _obs.observation_detail(con, oid)
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.NOT_FOUND, str(exc))
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/observations/([^/]+)/effective-text", path)
            if m:
                from earwitness.app import observations as _obs
                oid = _seg(m.group(1))
                if not re.fullmatch(EVIDENCE_ID_RE, oid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid observation id")
                try:
                    out = _obs.effective_text(con, oid)
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/brands/([^/]+)/counts", path)
            if m:
                from earwitness.app import creatives as _cr
                brand = _seg(m.group(1)).strip().lower()
                if not brand or len(brand) > 200:
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid brand")
                review_filter = (qs.get("review") or ["reviewed"])[0]
                if review_filter not in ("reviewed", "all"):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "review must be reviewed or all")
                hidden = tuple(ops.hidden_show_titles(con))
                hidden_q = (" AND ep.show_title NOT IN (%s)" % ",".join("?" * len(hidden))) if hidden else ""
                reviewed_rows = con.execute(
                    "SELECT a.evidence_id FROM brand_assocs a"
                    " JOIN evidence e ON e.id=a.evidence_id"
                    " JOIN episodes ep ON ep.id=e.episode_id"
                    " LEFT JOIN reviews r ON r.evidence_id=e.id"
                    " WHERE a.brand=? AND a.basis<>'model'"  # PODS: model rows are display-only
                    " AND r.judgment='ad'" + hidden_q,
                    (brand, *hidden)).fetchall()
                prop_rows = con.execute(
                    "SELECT a.evidence_id FROM brand_assocs a"
                    " JOIN evidence e ON e.id=a.evidence_id"
                    " JOIN episodes ep ON ep.id=e.episode_id"
                    " WHERE a.brand=? AND a.basis<>'model'"  # PODS: model rows are display-only
                    + hidden_q,
                    (brand, *hidden)).fetchall()
                assoc_rows = reviewed_rows if review_filter == "reviewed" else prop_rows
                _cr._ensure_aux(con)
                cc = con.execute("SELECT COUNT(*) c FROM creatives"
                                 " WHERE status='confirmed'").fetchone()["c"]
                oc = con.execute("SELECT COUNT(*) c FROM occurrences"
                                 " WHERE correspondence='confirmed'").fetchone()["c"]
                return self._send_json({
                    "ok": True, "brand": brand, "review_filter": review_filter,
                    "brand_associations": len(prop_rows),
                    "reviewed_associations": len(reviewed_rows),
                    "creatives": {"value": cc, "unit": "confirmed creative IDs"},
                    "occurrences": {"value": oc, "unit": "resolved occurrence IDs"},
                    "proposals": {"value": len(prop_rows),
                                  "unit": "brand association rows (proposals and reviewed, separated)"},
                    "note": "Units are distinct and never summed: confirmed creative IDs,"
                            " resolved occurrence IDs, and brand association rows"
                            " are separate counts."})
            if path == "/monitoring/schedules":
                from earwitness.app import monitoring as _mon
                _mon._require_aux(con)
                rows = con.execute("SELECT s.* FROM schedules s ORDER BY s.id LIMIT 100").fetchall()
                return self._send_json({"ok": True,
                                        "schedules": [dict(r) for r in rows]})
            m = re.fullmatch(r"/monitoring/schedules/([^/]+)/status", path)
            if m:
                from earwitness.app import monitoring as _mon
                sid = _seg(m.group(1))
                if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,128}", sid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid schedule id")
                try:
                    out = _mon.schedule_status(con, sid)
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, "schedule": out})
            if path == "/monitoring/preferences":
                from earwitness.app import monitoring as _mon
                member = (qs.get("member") or [""])[0]
                if not isinstance(member, str) or not member or len(member) > 120:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "member is required (1..120 chars)")
                return self._send_json({"ok": True,
                                        "preferences": _mon.get_preferences(
                                            con, "local", member)})
            m = re.fullmatch(r"/campaigns/(\d+)/placements", path)
            if m:
                from earwitness.app import campaigns as _camp
                try:
                    items = _camp.list_expected(con, int(m.group(1)))
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, "placements": items})
            m = re.fullmatch(r"/campaigns/(\d+)/placements/([^/]+)/history", path)
            if m:
                from earwitness.app import campaigns as _camp
                pid = _seg(m.group(2))
                if not re.fullmatch(EVIDENCE_ID_RE, pid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid placement id")
                return self._send_json({"ok": True,
                                        "history": _camp.evaluation_history(con, pid)})
            return self._fail(HTTPStatus.NOT_FOUND, "unknown endpoint")
        finally:
            self._close_db(con)

    # -- POST routes ---------------------------------------------------
    def _route_post(self, path: str, _qs):
        con = self._request_db()
        try:
            m = re.fullmatch(r"/customer/records/(.+)/mistake", path)
            if m:
                body = self._read_json()
                if set(body) - {"reason", "status", "expected_revision"}:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "note creation holds reason/status/expected_revision only")
                from earwitness.app import customer_saved as _saved
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _saved.save_note, con, unquote(m.group(1)),
                        body.get("reason", ""), body.get("status", "open"),
                        None, self._optional_revision(), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/customer/research-query":
                body = self._read_json()
                if not isinstance(body, dict):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "body must be an object")
                summaries = body.get("summaries") or []
                if isinstance(summaries, str):
                    summaries = [summaries]
                with db.transaction(con, write=False):
                    out = self._customer_errors(
                        customer_query_mod.research_bundle, con,
                        body.get("query"), body.get("selector"),
                        list(summaries),
                        body.get("refresh_selector") is True,
                        local_view_context(), REPO,
                        expected_view_revision=body.get("expected_view_revision"))
                return self._send_json({"ok": True, **out})
            if path == "/customer/research-export":
                body = self._read_json()
                if not isinstance(body, dict):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "body must be an object")
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        customer_query_mod.capture_export, con,
                        body.get("query"), body.get("selector"),
                        body.get("scope", "all_matches"),
                        body.get("format", "json"),
                        body.get("expected_view_revision", ""),
                        local_view_context(), REPO,
                        str(body.get("title") or
                            "EarWitness research export")[:160], "owner")
                redacted = {k: v for k, v in out.items()
                            if k not in ("rows", "payload")}
                return self._send_json({"ok": True, **redacted})
            if path == "/customer/saved-items":
                from earwitness.app import customer_saved as _saved
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _saved.create_item, con, body,
                        workspace_context=local_view_context(),
                        actor="owner")
                return self._send_json({"ok": True, **out})
            if path == "/customer/alerts":
                body = self._read_json()
                self._deny_tracker_authority_fields(body)
                query = self._customer_query2_from_body(
                    (body or {}).get("query", {}))
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        customer_mod.save_alert, con, (body or {}).get(
                            "name", ""), query,
                        (body or {}).get("origin_route", ""),
                        (body or {}).get("event_types"),
                        (body or {}).get("delivery", {}),
                        (body or {}).get("selector"),
                        None, None, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/alerts/(\d+)/check", path)
            if m:
                out = self._customer_errors(customer_mod.check_alert, con,
                                            int(m.group(1)), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/alerts/(\d+)/read", path)
            if m:
                body = self._read_json()
                out = self._customer_errors(customer_mod.mark_alert_events_read, con,
                                            int(m.group(1)), body.get("event_ids", []),
                                            "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/alerts/(\d+)/lifecycle", path)
            if m:
                body = self._read_json()
                out = self._customer_errors(
                    customer_mod.set_alert_lifecycle, con, int(m.group(1)),
                    body.get("lifecycle", ""),
                    self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/alerts/(\d+)/backfill", path)
            if m:
                body = self._read_json()
                out = self._customer_errors(customer_mod.backfill_alert, con,
                                            int(m.group(1)),
                                            body.get("confirm") is True, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/placements/([^/]+)/evidence",
                             path)
            if m:
                from earwitness.app import campaigns as _camp
                cid = self._customer_visible_campaign(con, int(m.group(1)))
                body = self._read_json()
                out = self._customer_errors(
                    _camp.save_placement_evidence, con, cid,
                    unquote(m.group(2)), body.get("ref", ""),
                    self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/reports", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                out = self._customer_errors(
                    _camp.freeze_customer_report, con, int(m.group(1)),
                    self._expected_revision_from_body(),
                    body.get("pins", {}), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/imports/seed":
                out = self._op_errors(importer.seed_all, REPO, con, "owner")
                return self._send_json({"ok": True, "import": out})
            if path == "/imports/upload":
                body = self._read_json(IMPORT_BODY_LIMIT)
                # Only scripts/pilot_import.py writes a fetch-date provenance.
                body.pop("fetch_date_provenance", None)
                out = self._op_errors(importer.import_upload_transcript, con, body, "owner",
                                      str(body.get("origin_note", ""))[:500])
                return self._send_json({"ok": True, "import": out})
            if path == "/imports/reindex":
                self._op_errors(importer._reindex, con)
                rev = db.bump_revision(con)
                db.audit(con, "owner", "reindex", "workspace", "reindex", {}, rev)
                return self._send_json({"ok": True, "aggregate_rev": rev})
            if path == "/evidence/manual":
                body = self._read_json()
                out = self._op_errors(ops.create_manual_evidence, con, body, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/evidence/([^/]+)/review", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.save_review, con, _evidence_id(m.group(1)), body,
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/admin/review-queue/([^/]+)/action", path)
            if m:
                # One operator review action; rules it makes apply corpus-wide.
                eid = _evidence_id(m.group(1))
                out = self._op_errors(corrections_mod.act, con, eid, self._read_json(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/admin/review-queue/([^/]+)/undo", path)
            if m:
                # Undo the newest action on the read (decision 11); a read
                # changed since is READ_CHANGED. Hosted serves neither route.
                eid = _evidence_id(m.group(1))
                body = self._read_json()
                out = self._op_errors(corrections_mod.undo, con, eid, "owner",
                                      body.get("label_id") if isinstance(body, dict) else None)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/admin/brand-suggestions/(\d+)/(confirm|dismiss|undo)", path)
            if m:
                # Confirm / Not this / Undo on one stored suggestion; count
                # first when a rule changes shown reads. Hosted serves none.
                body = self._read_json()
                body = body if isinstance(body, dict) else {}
                sid, action = int(m.group(1)), m.group(2)
                if action == "confirm":
                    out = self._op_errors(brand_suggest_mod.confirm, con, sid, body,
                                          actor="owner", approved_by="Reno")
                elif action == "dismiss":
                    out = self._op_errors(brand_suggest_mod.dismiss, con, sid, body,
                                          actor="owner")
                else:
                    out = self._op_errors(brand_suggest_mod.undo, con, sid, actor="owner",
                                          expected_reads=body.get("expected_reads"), shown=body)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/records/(.+)/report", path)
            if m:
                # A member reports an ad as wrong; it waits for the operator.
                from earwitness.app import services as _services
                body, ref = self._read_json(), unquote(m.group(1))
                out = self._customer_errors(lambda: _services.customer_report_ad(
                    self._request_ctx, con, None, ref, body))
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/evidence/([^/]+)/brand-choice", path)
            if m:
                # Operator one-click brand choice from the read's candidates (A6).
                body = self._read_json()
                eid = _evidence_id(m.group(1))
                out = self._op_errors(ops.choose_brand, con, eid, body.get("brand"),
                                      self._expected_revision_from_body(), "owner")
                with db.transaction(con, write=False):
                    out["read"] = customer_mod.read_detail(con, eid)
                return self._send_json({"ok": True, **out})
            if path == "/watchlists":
                body = self._read_json()
                out = self._op_errors(ops.save_watchlist, con, body, None, None, "owner")
                return self._send_json({"ok": True, **out})
            if path == "/campaigns":
                body = self._read_json()
                out = self._op_errors(ops.save_campaign, con, body, None, None, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/evidence", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.attach_evidence, con, int(m.group(1)),
                                      str(body.get("evidence_id", "")), self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/checks":
                body = self._read_json()
                out = self._op_errors(ops.save_check, con, body, None, None, "owner")
                return self._send_json({"ok": True, **out})
            if path == "/exports":
                body = self._read_json()
                try:
                    agent_mod.require_saved_meaning(body.get("filter", {}), "export")
                except agent_mod.AgentError as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                out = self._op_errors(
                    exports_mod.build_export, con, body.get("filter", {}),
                    body.get("format", "json"), body.get("evidence_ids"),
                    str(body.get("title", "EarWitness export"))[:160], "owner", body.get("campaign_id"))
                return self._send_json({"ok": True, **{k: v for k, v in out.items()
                                                       if k != "payload"},
                                        "payload": out["payload"]})
            if path == "/settings":
                body = self._read_json()
                out = self._op_errors(ops.save_settings, con, body, self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/processing/estimate":
                body = self._read_json()
                _p09_require(con)
                episodes = _p09_episodes_or_400(con, body.get("episode_ids"))
                profile = _p09_profile_or_400(body.get("profile", "local"))
                return self._send_json({"ok": True, **_p09_estimate_payload(
                    con, episodes, profile, body.get("config"))})
            if path == "/processing/runs":
                body = self._read_json()
                _p09_require(con)
                key = body.get("idempotency_key")
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "idempotency_key is required (1..128 chars); "
                                   "duplicate clicks replay the same key, never a new run")
                episodes = _p09_episodes_or_400(con, body.get("episode_ids"))
                profile = _p09_profile_or_400(body.get("profile", "local"))
                config_digest = _p09_config_digest(profile, body.get("config"))
                # Job ids are the global processing_jobs primary key: scope them
                # to the idempotency key so distinct runs never collide while
                # same-key replays rebuild identical ids (the request digest
                # covers them) and return the stable replay.
                scope = "p09-%s-" % hashlib.sha256(
                    f"p09:{key}".encode()).hexdigest()[:8]
                specs, _rows = _p09_build_specs(con, episodes, profile,
                                                config_digest, scope=scope)
                wire = [{k: s[k] for k in ("job_id", "stage", "input_digest",
                                           "parents", "backend_profile",
                                           "cost_estimate")} for s in specs]
                summary = _p09_job_errors(
                    _jobs.submit_run, con, stages=wire, config_digest=config_digest,
                    mode="p09_browser", initiator="owner:browser",
                    idempotency_key=f"p09:{key}")
                # Record the episode behind each job for Library/coverage links.
                try:
                    with db.transaction(con, write=True):
                        for s in specs:
                            con.execute("INSERT OR IGNORE INTO p09_run_episodes "
                                        "(run_id, job_id, episode_id) VALUES (?, ?, ?)",
                                        (summary["run_id"], s["job_id"],
                                         s["episode_id"]))
                except KeyError:
                    pass
                return self._send_json({"ok": True, **_p09_run_payload(
                    con, summary["run_id"])})
            m = re.fullmatch(r"/processing/runs/(" + P09_RUN_ID_RE + r")/retry", path)
            if m:
                body = self._read_json()
                _p09_require(con)
                key = body.get("idempotency_key")
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "retry requires an idempotency key")
                failed = body.get("failed_job_ids")
                if not isinstance(failed, list) or not failed \
                        or not all(isinstance(j, str) and j for j in failed):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "failed_job_ids must be a non-empty string list; "
                                   "only failed jobs are retried, under their "
                                   "existing input/config identity")
                revision = body.get("expected_revision")
                if revision is not None and (
                        isinstance(revision, bool) or not isinstance(revision, int)):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "expected_revision must be an integer or null")
                _p09_job_errors(
                    _jobs.request_retry, con, m.group(1), failed,
                    f"p09retry:{key}", expected_revision=revision)
                return self._send_json({"ok": True, **_p09_run_payload(
                    con, m.group(1))})
            m = re.fullmatch(r"/processing/runs/(" + P09_RUN_ID_RE + r")/cancel", path)
            if m:
                body = self._read_json()
                _p09_require(con)
                revision = body.get("expected_revision")
                if revision is None:
                    raise AppError(HTTPStatus.PRECONDITION_REQUIRED,
                                   "expected revision is required")
                if isinstance(revision, bool) or not isinstance(revision, int):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "expected_revision must be an integer")
                try:
                    _p09_job_errors(_jobs.cancel, con, m.group(1), revision)
                except KeyError:
                    raise AppError(HTTPStatus.NOT_FOUND, "unknown run")
                return self._send_json({"ok": True, **_p09_run_payload(
                    con, m.group(1))})
            if path == "/processing/sources":
                body = self._read_json()
                url = body.get("url")
                if not isinstance(url, str) or not url or len(url) > 2000:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "url must be a non-empty string (max 2000 chars)")
                cleaned = _p09_validate_source_url(url)
                _p09_require(con)
                host = urlparse(cleaned).hostname or ""
                row = con.execute("SELECT id, url FROM p09_feed_sources WHERE "
                                  "workspace_id='local' AND url=?", (cleaned,)).fetchone()
                if row is None:
                    import uuid as _uuid
                    source_id = "src_" + _uuid.uuid4().hex[:12]
                    with db.transaction(con, write=True):
                        con.execute("INSERT INTO p09_feed_sources (id, workspace_id, url, "
                                    "host, created_at) VALUES (?, 'local', ?, ?, ?)",
                                    (source_id, cleaned, host,
                                     datetime.datetime.now(
                                         datetime.timezone.utc).isoformat()))
                else:
                    source_id = row["id"]
                return self._send_json({"ok": True, "source_id": source_id,
                                        "url": cleaned, "host": host})
            if path == "/processing/uploads":
                return self._p09_upload(con)
            if path == "/observations/split-preview":
                body = self._read_json()
                from earwitness.app import observations as _obs
                try:
                    preview = _obs.preview_split(con, body.get("observation_id", ""),
                                                 body.get("outputs") or [])
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, "preview": preview})
            if path == "/observations/merge-preview":
                body = self._read_json()
                from earwitness.app import observations as _obs
                try:
                    preview = _obs.preview_merge(con, body.get("observation_ids") or [],
                                                 body.get("output") or {})
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, "preview": preview})
            # -- PSURF additive POST surfaces (407/540/604) --
            # Purely additive: no existing route above is modified.
            if path == "/creatives":
                from earwitness.app import creatives as _cr
                body = self._read_json()
                oids = body.get("observation_ids")
                if not isinstance(oids, list) or not oids or len(oids) > 100:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "observation_ids must be a non-empty list (max 100)")
                for oid in oids:
                    if not isinstance(oid, str) or not re.fullmatch(EVIDENCE_ID_RE, oid):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "invalid observation id in observation_ids")
                status = body.get("status", "suggested")
                if status not in ("suggested", "confirmed", "split", "merged", "rejected"):
                    raise AppError(HTTPStatus.BAD_REQUEST, "unknown creative status")
                out = self._op_errors(_cr.create_creative, con, oids, "owner", status)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/creatives/([^/]+)/confirm", path)
            if m:
                from earwitness.app import creatives as _cr
                cid = _seg(m.group(1))
                if not re.fullmatch(EVIDENCE_ID_RE, cid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid creative id")
                body = self._read_json()
                oid = body.get("observation_id")
                if not isinstance(oid, str) or not re.fullmatch(EVIDENCE_ID_RE, oid):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "observation_id is required (1..128 chars)")
                out = self._op_errors(_cr.confirm_membership, con, cid, oid,
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/creatives/([^/]+)/reject", path)
            if m:
                from earwitness.app import creatives as _cr
                cid = _seg(m.group(1))
                if not re.fullmatch(EVIDENCE_ID_RE, cid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid creative id")
                body = self._read_json()
                a = body.get("observation_id_a")
                b = body.get("observation_id_b")
                for v in (a, b):
                    if not isinstance(v, str) or not re.fullmatch(EVIDENCE_ID_RE, v):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "observation_id_a/b are required (1..128 chars)")
                digest = body.get("config_digest", "")
                if not isinstance(digest, str) or len(digest) > 64:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "config_digest must be a string (max 64 chars)")
                out = self._op_errors(_cr.reject_membership, con, cid, a, b,
                                      self._expected_revision_from_body(),
                                      "owner", digest)
                return self._send_json({"ok": True, **out})
            if path == "/creatives/split":
                from earwitness.app import creatives as _cr
                body = self._read_json()
                cid = body.get("creative_id")
                groups = body.get("groups")
                if not isinstance(cid, str) or not re.fullmatch(EVIDENCE_ID_RE, cid):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "creative_id is required (1..128 chars)")
                if not isinstance(groups, list) or len(groups) < 2 or len(groups) > 20:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "groups must be a list of 2..20 observation-id lists")
                for g in groups:
                    if not isinstance(g, list) or not g:
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "each split group must be a non-empty list")
                    for oid in g:
                        if not isinstance(oid, str) or not re.fullmatch(EVIDENCE_ID_RE, oid):
                            raise AppError(HTTPStatus.BAD_REQUEST,
                                           "invalid observation id in groups")
                out = self._op_errors(_cr.split_creative, con, cid, groups,
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/creatives/merge":
                from earwitness.app import creatives as _cr
                body = self._read_json()
                cids = body.get("creative_ids")
                revs = body.get("expected_revisions")
                if not isinstance(cids, list) or len(cids) < 2 or len(cids) > 20:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "creative_ids must be a list of 2..20 ids")
                for cid in cids:
                    if not isinstance(cid, str) or not re.fullmatch(EVIDENCE_ID_RE, cid):
                        raise AppError(HTTPStatus.BAD_REQUEST, "invalid creative id")
                if not isinstance(revs, dict) or not revs:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "expected_revisions is required")
                out = self._op_errors(_cr.merge_creatives, con, cids, revs, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/monitoring/watchlists/(\d+)/enable", path)
            if m:
                from earwitness.app import monitoring as _mon
                body = self._read_json()
                matches = body.get("current_matches", [])
                rev = body.get("definition_revision", 1)
                if not isinstance(matches, list) or len(matches) > 5000:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "current_matches must be a list (max 5000)")
                for mtch in matches:
                    if not isinstance(mtch, dict) or not mtch.get("identity"):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "each match needs an identity")
                if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "definition_revision must be a non-negative integer")
                wid = int(m.group(1))
                if con.execute("SELECT id FROM watchlists WHERE id=?",
                               (wid,)).fetchone() is None:
                    raise AppError(HTTPStatus.BAD_REQUEST, "unknown watchlist")
                _mon._require_aux(con)
                out = self._op_errors(_mon.establish_baseline, con, "local", wid, rev, matches)
                sid = f"watchlist-{wid}"
                now = _mon._now_iso()
                interval = body.get("interval_s", 21600)
                if not isinstance(interval, int) or isinstance(interval, bool):
                    interval = 21600
                interval = max(600, min(86400, interval))
                con.execute("INSERT OR IGNORE INTO schedules (id, workspace_id, mode,"
                            " source_ids, interval_s, enabled, definition_revision,"
                            " revision, updated_at) VALUES (?, 'local', 'discovery',"
                            " '[]', ?, 1, ?, 1, ?)",
                            (sid, interval, rev, now))
                con.execute("UPDATE schedules SET enabled=1, definition_revision=?,"
                            " revision=revision+1, updated_at=? WHERE id=?",
                            (rev, now, sid))
                due = _mon.compute_initial_due(now, interval, sid)
                _mon.save_schedule_state(con, sid, "local", due)
                return self._send_json({"ok": True, "enabled": True,
                                        "schedule_id": sid, **out})
            m = re.fullmatch(r"/monitoring/watchlists/(\d+)/disable", path)
            if m:
                from earwitness.app import monitoring as _mon
                self._read_json()
                wid = int(m.group(1))
                sid = f"watchlist-{wid}"
                _mon._require_aux(con)
                if con.execute("SELECT id FROM schedules WHERE id=?",
                               (sid,)).fetchone() is None:
                    raise AppError(HTTPStatus.BAD_REQUEST, "unknown monitor schedule")
                con.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?",
                            (_mon._now_iso(), sid))
                return self._send_json({"ok": True, "enabled": False,
                                        "schedule_id": sid})
            m = re.fullmatch(r"/monitoring/watchlists/(\d+)/backfill", path)
            if m:
                from earwitness.app import monitoring as _mon
                body = self._read_json()
                if body.get("confirm") is not True:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "backfill is a separate explicit action:"
                                   " pass confirm:true")
                matches = body.get("current_matches")
                if not isinstance(matches, list) or not matches or len(matches) > 5000:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "current_matches must be a non-empty list (max 5000)")
                rev = body.get("definition_revision", 1)
                if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "definition_revision must be a non-negative integer")
                wid = int(m.group(1))
                sid = f"watchlist-{wid}"
                if con.execute("SELECT id FROM schedules WHERE id=?",
                               (sid,)).fetchone() is None:
                    raise AppError(HTTPStatus.BAD_REQUEST, "unknown monitor schedule")
                for mtch in matches:
                    if not isinstance(mtch, dict):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "each match must be an object")
                    kind = str(mtch.get("kind") or "")
                    if kind not in _mon.EVENT_KINDS:
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       f"unknown event kind: {kind!r}")
                    if not mtch.get("identity"):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "each match needs an identity")
                recipients = body.get("recipients", [])
                if not isinstance(recipients, list) or any(
                        not isinstance(r, str) or not r or len(r) > 120
                        for r in recipients):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "recipients must be a list of non-empty"
                                   " strings (max 120 chars)")
                out = self._op_errors(_mon.detect_and_queue, con, "local",
                                      sid, rev, matches, recipients)
                return self._send_json({"ok": True, "schedule_id": sid, **out})
            if path == "/monitoring/preferences":
                from earwitness.app import monitoring as _mon
                body = self._read_json()
                member = body.get("member")
                digest = body.get("digest", "daily")
                immediate = body.get("immediate", False)
                if not isinstance(member, str) or not member or len(member) > 120:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "member is required (1..120 chars)")
                if digest not in ("daily", "immediate", "none"):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "digest must be daily, immediate, or none")
                if not isinstance(immediate, bool):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "immediate must be a boolean")
                _mon._require_aux(con)
                try:
                    out = _mon.set_preferences(con, "local", member, digest, immediate)
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                return self._send_json({"ok": True, **out})
            if path == "/monitoring/preferences/unsubscribe":
                from earwitness.app import monitoring as _mon
                body = self._read_json()
                member = body.get("member")
                unsub = body.get("unsubscribed", True)
                if not isinstance(member, str) or not member or len(member) > 120:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "member is required (1..120 chars)")
                if not isinstance(unsub, bool):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "unsubscribed must be a boolean")
                _mon._require_aux(con)
                out = _mon.set_unsubscribed(con, "local", member, unsub)
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/placements", path)
            if m:
                body = self._read_json()
                out = _create_customer_placement(con, int(m.group(1)), body, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/placements", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.save_customer_placement, con, int(m.group(1)),
                        body, None, None,
                        (body or {}).get("expected_campaign_revision"),
                        "owner")
                return self._send_json({"ok": True, **out})
            if path == "/customer/campaigns":
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.save_customer_campaign, con, body, None, None,
                        "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/targets", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                target = (body or {}).get("target", body or {})
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.create_target, con, int(m.group(1)),
                        target.get("scope", "episode"), target.get("show_id"),
                        target.get("episode_id"), target.get("date_from"),
                        target.get("date_to"), target.get("timezone", "UTC"),
                        target.get("note", ""), "owner",
                        (body or {}).get("expected_campaign_revision"))
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/allocation", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.allocate_placement_evidence, con,
                        int(m.group(1)), body,
                        (body or {}).get("expected_campaign_revision"),
                        "owner")
                if isinstance(out, dict) and "conflict" in out:
                    return self._send_json({"ok": False, **out},
                                           status=HTTPStatus.CONFLICT)
                return self._send_json({"ok": True, **out})
            if path == "/customer/briefs":
                from earwitness.app import briefs as _briefs
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _briefs.create_brief, con, body, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/briefs/([^/]+)/versions", path)
            if m:
                from earwitness.app import briefs as _briefs
                raw = self._read_json() or {}
                # B3 body envelope: expected_revision rides in the body.
                # Extract it as the displayed-revision precondition; the
                # stored version row keeps domain fields only.
                body = {k: v for k, v in raw.items()
                        if k != "expected_revision"}
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _briefs.add_version, con, unquote(m.group(1)), body,
                        self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/placements/([^/]+)/assess", path)
            if m:
                from earwitness.app import campaigns as _camp
                pid = _seg(m.group(2))
                if not re.fullmatch(EVIDENCE_ID_RE, pid):
                    raise AppError(HTTPStatus.BAD_REQUEST, "invalid placement id")
                body = self._read_json()
                author = body.get("author", "")
                verdict = body.get("verdict", "")
                reason = body.get("reason", "")
                evidence = body.get("evidence", [])
                if not isinstance(author, str) or not author or len(author) > 120:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "author is required (1..120 chars)")
                if verdict not in ("supported", "not_supported", "inconclusive"):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "verdict must be supported/not_supported/inconclusive")
                if not isinstance(reason, str) or not reason.strip():
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "a manual assessment requires a reason")
                if not isinstance(evidence, list) or len(evidence) > 100:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "evidence must be a list (max 100)")
                out = self._op_errors(_camp.save_manual_assessment, con, pid, author,
                                      verdict, reason, evidence, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/evaluate", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                cid = int(m.group(1))
                occurrences = body.get("occurrences")
                coverage = body.get("coverage", {})
                now_iso = body.get("now_iso", "")
                if not isinstance(occurrences, list) or len(occurrences) > 500:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "occurrences must be a list (max 500)")
                for occ in occurrences:
                    if not isinstance(occ, dict) or not occ.get("id"):
                        raise AppError(HTTPStatus.BAD_REQUEST,
                                       "each occurrence needs an id")
                if not isinstance(coverage, dict):
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "coverage must be an object")
                if not isinstance(now_iso, str) or not now_iso:
                    raise AppError(HTTPStatus.BAD_REQUEST, "now_iso is required")
                # Evaluate writes nothing, but an archived campaign takes
                # reads and Restore only (decision 16).
                self._op_errors(_camp._refuse_archived, con, cid)
                try:
                    placements = _camp.list_expected(con, cid)
                except ops.BadInput as exc:
                    raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
                if not placements:
                    raise AppError(HTTPStatus.BAD_REQUEST, "no expected placements")
                out = _camp.allocate_campaign(placements, occurrences, coverage,
                                              now_iso, body.get("reuse_overrides"))
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)/evaluations/freeze", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                cid = int(m.group(1))
                evaluation = body.get("evaluation")
                pins = body.get("pins", {})
                if not isinstance(evaluation, dict) or "evaluations" not in evaluation:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "evaluation.evaluations is required")
                if not isinstance(pins, dict):
                    raise AppError(HTTPStatus.BAD_REQUEST, "pins must be an object")
                out = self._op_errors(_camp.freeze_evaluation, con, cid,
                                      evaluation, pins, "owner")
                return self._send_json({"ok": True, **out})
            return self._fail(HTTPStatus.NOT_FOUND, "unknown endpoint")
        finally:
            self._close_db(con)

    def _optional_revision(self):
        try:
            body = self._cached_body
        except AttributeError:
            body = None
        match = self.headers.get("If-Match")
        if match is not None:
            try:
                return int(match.strip().strip('"'))
            except ValueError:
                raise AppError(HTTPStatus.BAD_REQUEST, "If-Match must be an integer revision")
        if isinstance(body, dict) and "expected_revision" in body:
            try:
                return int(body["expected_revision"])
            except (TypeError, ValueError):
                raise AppError(HTTPStatus.BAD_REQUEST, "expected_revision must be an integer")
        return None

    def _expected_revision_from_body(self):
        try:
            body = self._cached_body
        except AttributeError:
            body = None
        match = self.headers.get("If-Match")
        if match is not None:
            try:
                return int(match.strip().strip('"'))
            except ValueError:
                raise AppError(HTTPStatus.BAD_REQUEST, "If-Match must be an integer revision")
        if isinstance(body, dict) and "expected_revision" in body:
            try:
                return int(body["expected_revision"])
            except (TypeError, ValueError):
                raise AppError(HTTPStatus.BAD_REQUEST, "expected_revision must be an integer")
        raise AppError(HTTPStatus.PRECONDITION_REQUIRED, "expected revision is required")

    # -- P09 staged audio upload: streamed, capped, content-hash named --
    def _p09_upload(self, con):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = P09_AUDIO_MIMES.get(ctype)
        if ext is None:
            raise AppError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                           "permitted audio types: %s" % ", ".join(sorted(P09_AUDIO_MIMES)))
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise AppError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length <= 0 or length > P09_AUDIO_MAX_BYTES:
            raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           f"audio upload is bounded to {P09_AUDIO_MAX_BYTES} bytes")
        label = (self.headers.get("X-Filename") or "upload").strip()[:120]
        label = re.sub(r"[^A-Za-z0-9 _.,()-]", "_", label) or "upload"
        # Setup/upgrade refusals come before any byte is written.
        _p09_require(con)
        staged = workspace() / "staged"
        staged.mkdir(mode=0o700, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        tmp = staged / ("upload-%s.part" % secrets.token_hex(8))
        received = 0
        try:
            with open(tmp, "wb") as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    received += len(chunk)
                    remaining -= len(chunk)
                    if received > P09_AUDIO_MAX_BYTES:
                        raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                                       f"audio upload is bounded to {P09_AUDIO_MAX_BYTES} bytes")
                    digest.update(chunk)
                    handle.write(chunk)
            if received != length:
                raise AppError(HTTPStatus.BAD_REQUEST,
                               "truncated upload: received %d of %d bytes; "
                               "no artifact was kept" % (received, length))
            sha = digest.hexdigest()
            final = staged / (sha + ext)
            # Check-then-replace is safe only because every write POST (this
            # route included) holds the workspace write lock (BEGIN IMMEDIATE)
            # for the whole route: an identical concurrent upload waits
            # before reading bytes. Streaming outside that transaction
            # would make this a race.
            created = not final.exists()
            if created:
                os.replace(tmp, final)
                self._request_created.append(final)
            else:
                tmp.unlink(missing_ok=True)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        # Any refusal or failure from here rolls the request back, and
        # _protected_dispatch deletes the file this request created (only
        # that one) while the write lock is still held.
        duration = _p09_probe_duration(final)
        if duration is not None and duration > P09_AUDIO_MAX_DURATION_S:
            raise AppError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                           "audio exceeds the %.0f-minute duration cap" %
                           (P09_AUDIO_MAX_DURATION_S / 60))
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        try:
            record = _artifacts.register_media(
                con, digest=sha, size_bytes=received, duration_s=duration,
                format=ctype.split("/")[-1], locator="staged/%s" % final.name,
                rights_state="permitted", fetch_date=today,
                fetch_date_source="upload:received-date")
        except _artifacts.ArtifactError as exc:
            raise AppError(HTTPStatus.BAD_REQUEST, str(exc))
        return self._send_json({"ok": True, "artifact_id": record["id"],
                                "filename_label": label, "bytes": received,
                                "sha256": sha, "duration_s": duration,
                                "duration_unknown": duration is None,
                                "note": None if duration is not None else
                                "Duration is unknown (no media probe); the byte cap "
                                "was enforced and the artifact is private to this "
                                "workspace."})

    # -- PUT routes ----------------------------------------------------
    def _route_put(self, path: str, _qs):
        con = self._request_db()
        try:
            m = re.fullmatch(r"/customer/saved-items/([^/]+)", path)
            if m:
                from earwitness.app import customer_saved as _saved
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _saved.update_item, con, unquote(m.group(1)),
                        (body or {}).get("item", body or {}),
                        self._expected_revision_from_body(), actor="owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/alerts/(\d+)", path)
            if m:
                body = self._read_json()
                self._deny_tracker_authority_fields(body)
                query = self._customer_query2_from_body(
                    (body or {}).get("query", {}))
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        customer_mod.save_alert, con, (body or {}).get(
                            "name", ""), query,
                        (body or {}).get("origin_route", ""),
                        (body or {}).get("event_types"),
                        (body or {}).get("delivery", {}),
                        (body or {}).get("selector"),
                        int(m.group(1)),
                        self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/mistakes/(\d+)", path)
            if m:
                body = self._read_json()
                if set(body) - {"reason", "status", "expected_revision"}:
                    raise AppError(HTTPStatus.BAD_REQUEST,
                                   "note metadata holds reason/status/expected_revision only")
                from earwitness.app import customer_saved as _saved
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _saved.save_note, con, None,
                        body.get("reason", ""), body.get("status", "open"),
                        int(m.group(1)), self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            if path == "/customer/preferences":
                body = self._read_json()
                revision = self._expected_revision_from_body()
                # Envelope/domain separation: the revision is checked by
                # the adapter and never leaks in as a preference key.
                prefs = {k: v for k, v in (body or {}).items()
                         if k != "expected_revision"} if isinstance(body, dict) else body
                out = self._customer_errors(
                    customer_mod.save_preferences, con, prefs,
                    revision, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                raw = self._read_json() or {}
                # B3 body envelope: expected_revision rides outside the
                # domain campaign object; only domain fields go in.
                body = {k: v for k, v in raw.items()
                        if k != "expected_revision"}
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.save_customer_campaign, con, body,
                        int(m.group(1)),
                        self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/targets/([^/]+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                tid = unquote(m.group(1))
                with db.transaction(con, write=True):
                    cid = self._customer_errors(
                        _camp.target_campaign, con, tid)
                    out = self._customer_errors(
                        _camp.update_target_note, con, cid, tid,
                        (body.get("target", body) or {}).get("note", "")
                        if isinstance(body, dict) else "",
                        self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/observations/([^/]+)/fields/([^/]+)", path)
            if m:
                from earwitness.app import observations as _obs
                body = self._read_json()
                out = self._op_errors(
                    _obs.confirm_observation_field, con, unquote(m.group(1)),
                    unquote(m.group(2)), body.get("value", ""),
                    self._expected_revision_from_body(),
                    body.get("evidence", {}), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/customer/campaigns/(\d+)/placements/([^/]+)", path)
            if m:
                from earwitness.app import campaigns as _camp
                body = self._read_json()
                with db.transaction(con, write=True):
                    out = self._customer_errors(
                        _camp.save_customer_placement, con, int(m.group(1)),
                        body, unquote(m.group(2)),
                        self._expected_revision_from_body(),
                        (body or {}).get("expected_campaign_revision"),
                        "owner")
                return self._send_json({"ok": True, **out})
            if path == "/shows/hide":
                body = self._read_json()
                if not isinstance(body.get("show_title"), str) or not body["show_title"].strip():
                    raise AppError(HTTPStatus.BAD_REQUEST, "show_title is required")
                if not isinstance(body.get("hidden"), bool):
                    raise AppError(HTTPStatus.BAD_REQUEST, "hidden must be a boolean")
                revision = body.get("expected_revision")
                if revision is None and self.headers.get("If-Match") is not None:
                    revision = self._expected_revision_from_body()
                out = self._op_errors(ops.set_show_hidden, con,
                                      body["show_title"].strip(), body["hidden"],
                                      revision, "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/evidence/([^/]+)/assocs", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.set_brand_assocs, con, _evidence_id(m.group(1)),
                                      body.get("assocs", []),
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/episodes/([^/]+)/media", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.set_episode_media, con, _episode_id(m.group(1)), body,
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/watchlists/(\d+)", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.save_watchlist, con, body, int(m.group(1)),
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/campaigns/(\d+)", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.save_campaign, con, body, int(m.group(1)),
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            m = re.fullmatch(r"/checks/(\d+)", path)
            if m:
                body = self._read_json()
                out = self._op_errors(ops.save_check, con, body, int(m.group(1)),
                                      self._expected_revision_from_body(), "owner")
                return self._send_json({"ok": True, **out})
            return self._fail(HTTPStatus.NOT_FOUND, "unknown endpoint")
        finally:
            self._close_db(con)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="EarWitness local application server (loopback only).")
    p.add_argument("--workspace", default=".earwitness")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--repo-root", default=str(REPO))
    return p


def main(argv=None) -> int:
    global REPO
    args = build_parser().parse_args(argv)
    REPO = Path(args.repo_root).resolve()
    if args.port != DEFAULT_PORT and not (1024 <= args.port <= 65535):
        print("error: --port must be a valid local port", file=sys.stderr)
        return 2
    _state["port"] = args.port
    ws = Path(args.workspace)
    if not ws.is_absolute():
        ws = (REPO / ws).resolve()
    ws = ws.resolve()
    if REPO.resolve() not in ws.parents:
        print("error: --workspace must live inside the repository", file=sys.stderr)
        return 2
    _state["workspace"] = ws
    from contextlib import ExitStack
    locks = ExitStack()
    locks.enter_context(db.workspace_lock(ws, shared=True))
    locks.enter_context(db.workspace_lock(ws, name="server.lock"))
    # Read-path cutover: reads never migrate. A missing or incompatible
    # workspace is an actionable boot error, not a silent auto-upgrade.
    con = None
    try:
        con = open_db()
        db.require_current_schema(con)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: cannot serve workspace: {exc}", file=sys.stderr)
        locks.close()
        return 1
    finally:
        if con is not None:
            con.close()
    quarantine = ws / "quarantine.json"
    if quarantine.exists():
        try:
            reason = json.loads(quarantine.read_text()).get("reason", "quarantined")
        except (OSError, ValueError):
            reason = "quarantined"
        print(f"error: workspace is quarantined ({reason}); reconcile recovery authority"
              " with: python3 scripts/app_workspace.py reconcile --workspace <workspace>",
              file=sys.stderr)
        locks.close()
        return 1
    import os
    pid_file = ws / "server.pid"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.timeout = 30
    try:
        pid_file.write_text(str(os.getpid()))
        print(f"EarWitness app: http://127.0.0.1:{args.port}/app/")
        print(f"Workspace: {ws}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        locks.close()
        try:
            pid_file.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
