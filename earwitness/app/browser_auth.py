"""Browser authentication: OIDC sign-in plus opaque server sessions (Task 11).

Auth0-compatible OIDC authorization-code flow with PKCE (S256), one-use
state and nonce, exact issuer/client/audience validation, and signature,
algorithm, JWKS, exp, and nbf validation through joserfc (Authlib
remains the OIDC client dependency, not the JOSE implementation). No
custom JWT cryptography, no demo account.

Built-in email and password sign-in for the pilot (PW design rev 4) lives
in local_accounts.py and the asgi password routes: the `local_accounts`
table is part of this schema, and open_issuer_session mints the same
opaque session for the local issuer (the public origin). Passwords are
stored only as scrypt-v1 hashes; the operator creates each login.

An account is the (issuer, subject) pair, never an editable email. A
verified email may bind an explicit named invitation; nothing merges
accounts or grants membership from an unverified claim.

Sessions are opaque server-side records; the browser holds only a random
session id in an HttpOnly SameSite cookie (Secure except an explicit
loopback test/dev exception). Tokens and secrets never reach browser
storage, logs, URLs, or fixtures. Membership and policy recheck at the
service layer on every operation; logout/revocation takes effect on the
next request. Browser writes additionally require a same-origin CSRF
token independent of bearer rules. Owner mode works without an IdP;
an unconfigured provider reports unavailable instead of inventing access.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import secrets
import socket
import sqlite3
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass

SESSION_COOKIE = "ew_session"
TX_COOKIE = "ew_tx"
CSRF_HEADER = "x-csrf-token"
LOGIN_TXN_TTL_S = 600
SESSION_TTL_S = 12 * 3600
ALLOWED_ALGS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
# Provider I/O bounds (L20). Every discovery, token and JWKS request ends
# within PROVIDER_TOTAL_S (connect, headers and body together) and reads at
# most PROVIDER_MAX_BYTES. Validated discovery and JWKS are reused for
# PROVIDER_CACHE_TTL_S; a token signed with an unknown key id refreshes the
# JWKS at most once per JWKS_KID_REFRESH_S.
PROVIDER_TOTAL_S = 5.0
PROVIDER_MAX_BYTES = 256 * 1024
PROVIDER_CACHE_TTL_S = 900.0
JWKS_KID_REFRESH_S = 30.0

_AUTH_DDL = """
CREATE TABLE IF NOT EXISTS oidc_login_txns (
    state_hash TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    nonce TEXT NOT NULL,
    return_to TEXT NOT NULL,
    created_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    browser_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_hash TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    email_verified INTEGER NOT NULL DEFAULT 0,
    csrf_token TEXT NOT NULL,
    active_workspace TEXT NOT NULL DEFAULT '',
    workspace_switch_revision INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(workspace_switch_revision)='integer' AND workspace_switch_revision>=0),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS local_accounts (
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT,
    password_set_at REAL,
    created_at REAL NOT NULL,
    PRIMARY KEY (issuer, subject)
);
CREATE UNIQUE INDEX IF NOT EXISTS local_accounts_email ON local_accounts (lower(email));
"""


@dataclass
class BrowserAuthConfig:
    issuer: str
    client_id: str
    audience: str
    redirect_uri: str
    jwks_uri: str = ""
    token_endpoint: str = ""
    authorization_endpoint: str = ""
    allow_insecure_loopback: bool = False
    session_ttl_s: int = SESSION_TTL_S

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.audience
                    and self.redirect_uri)


def config_from_env(env: dict | None = None) -> BrowserAuthConfig:
    """Build the provider config from explicit local configuration only.

    No provider is ever invented: missing issuer/client/audience/
    redirect URI means unavailable, never a demo account.
    """
    import os
    src = env if env is not None else os.environ
    return BrowserAuthConfig(
        # The issuer is an exact identity (OIDC compares it byte for byte):
        # never normalized, so a slash-terminated issuer stays itself.
        issuer=str(src.get("EARWITNESS_OAUTH_ISSUER") or ""),
        client_id=str(src.get("EARWITNESS_OAUTH_CLIENT_ID") or ""),
        audience=str(src.get("EARWITNESS_OAUTH_AUDIENCE") or ""),
        redirect_uri=str(src.get("EARWITNESS_OAUTH_REDIRECT_URI") or ""),
        jwks_uri=str(src.get("EARWITNESS_OAUTH_JWKS_URI") or ""),
        token_endpoint=str(src.get("EARWITNESS_OAUTH_TOKEN_ENDPOINT") or ""),
        authorization_endpoint=str(src.get("EARWITNESS_OAUTH_AUTHORIZATION_ENDPOINT") or ""),
        allow_insecure_loopback=str(
            src.get("EARWITNESS_OAUTH_ALLOW_INSECURE_LOOPBACK") or "").lower()
        in ("1", "true", "yes"),
    )


def ensure_auth_schema(con: sqlite3.Connection) -> None:
    con.executescript(_AUTH_DDL)
    cols = {(row["name"] if hasattr(row, "keys") else row[1]) for row in
            con.execute("PRAGMA table_info(oidc_login_txns)").fetchall()}
    if "browser_hash" not in cols:
        con.execute("ALTER TABLE oidc_login_txns ADD COLUMN"
                    " browser_hash TEXT NOT NULL DEFAULT ''")
    cols = {(row["name"] if hasattr(row, "keys") else row[1]) for row in
            con.execute("PRAGMA table_info(browser_sessions)").fetchall()}
    if "workspace_switch_revision" not in cols:
        con.execute("ALTER TABLE browser_sessions ADD COLUMN workspace_switch_revision"
                    " INTEGER NOT NULL DEFAULT 0 CHECK"
                    " (typeof(workspace_switch_revision)='integer' AND workspace_switch_revision>=0)")


def _hash_presented(value: str) -> str:
    return hashlib.sha256(f"ew-browser-v1:{value}".encode()).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class ProviderError(Exception):
    """The sign-in provider did not answer in time, within the size bound,
    or with a usable reply. Callers answer 503; no detail reaches users."""


def _cut(sock) -> None:
    try:
        # The plain socket shutdown, not SSLSocket's: it only wakes the
        # blocked read in the request thread, which then fails.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


def _provider_request(method: str, url: str, body: bytes | None = None,
                      headers: dict | None = None,
                      timeout: float | None = None) -> tuple[int, bytes]:
    """One provider HTTP exchange bounded in total time and reply size.

    A timer shuts the socket at the deadline, so a held or dripping reply
    cannot outlast it; the reply is read up to PROVIDER_MAX_BYTES + 1."""
    total = PROVIDER_TOTAL_S if timeout is None else min(timeout,
                                                         PROVIDER_TOTAL_S)
    deadline = time.monotonic() + total
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProviderError("sign-in provider URL is not http(s)")
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=total,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port,
                                          timeout=total)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    timer = resp = None
    try:
        conn.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("sign-in provider timed out")
        # The connection drops its own reference once the reply is marked
        # will-close, so the timer holds the socket itself.
        timer = threading.Timer(remaining, _cut, (conn.sock,))
        timer.daemon = True
        timer.start()
        conn.request(method, target, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read(PROVIDER_MAX_BYTES + 1)
        if len(data) > PROVIDER_MAX_BYTES:
            raise ProviderError("sign-in provider reply is too large")
        if time.monotonic() > deadline:
            raise ProviderError("sign-in provider timed out")
        return resp.status, data
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderError("sign-in provider is unavailable") from exc
    finally:
        if timer is not None:
            timer.cancel()
        if resp is not None:
            resp.close()
        conn.close()


def _json_reply(data: bytes) -> dict:
    try:
        doc = json.loads(data.decode())
    except ValueError as exc:
        raise ProviderError("sign-in provider reply is not JSON") from exc
    if not isinstance(doc, dict):
        raise ProviderError("sign-in provider reply is not a JSON object")
    return doc


def _fetch_json(url: str, *, timeout: float | None = None) -> dict:
    status, data = _provider_request("GET", url,
                                     headers={"Accept": "application/json"},
                                     timeout=timeout)
    if not 200 <= status < 300:
        raise ProviderError("sign-in provider is unavailable")
    return _json_reply(data)


def _post_form(url: str, fields: dict, *,
               timeout: float | None = None) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    status, data = _provider_request(
        "POST", url, body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"}, timeout=timeout)
    if status == 429 or status >= 500:
        # The provider is throttling the app or failing: unavailable for
        # everyone, not a refusal of this user's code.
        raise ProviderError("sign-in provider is unavailable")
    if 400 <= status < 500:
        # The provider refused this code (expired, reused, wrong client).
        raise ValueError("provider refused sign-in")
    if not 200 <= status < 300:
        raise ProviderError("sign-in provider is unavailable")
    return _json_reply(data)


class ProviderCache:
    """Validated discovery documents (by issuer) and JWKS (by URI), kept
    for ttl_s. One per web process; thread-safe. An unknown key id may
    refresh a JWKS only when the last fetch is kid_refresh_s old."""

    def __init__(self, *, clock=time.monotonic,
                 ttl_s: float | None = None,
                 kid_refresh_s: float | None = None):
        self.clock = clock
        self.ttl_s = ttl_s
        self.kid_refresh_s = kid_refresh_s
        self._lock = threading.Lock()
        self._docs: dict = {}
        self._jwks: dict = {}

    def _ttl(self) -> float:
        return PROVIDER_CACHE_TTL_S if self.ttl_s is None else self.ttl_s

    def discovery(self, issuer: str):
        with self._lock:
            hit = self._docs.get(issuer)
        if hit and self.clock() - hit[0] < self._ttl():
            return hit[1]
        return None

    def store_discovery(self, issuer: str, doc: dict) -> None:
        with self._lock:
            self._docs[issuer] = (self.clock(), doc)

    def jwks(self, uri: str, kid: str = ""):
        """The cached (KeySet, key ids), or None when it must be fetched."""
        with self._lock:
            hit = self._jwks.get(uri)
        if not hit:
            return None
        age = self.clock() - hit[0]
        if age >= self._ttl():
            return None
        refresh = (JWKS_KID_REFRESH_S if self.kid_refresh_s is None
                   else self.kid_refresh_s)
        if kid and kid not in hit[2] and age >= refresh:
            return None
        return hit[1], hit[2]

    def store_jwks(self, uri: str, key_set, kids: frozenset) -> None:
        with self._lock:
            self._jwks[uri] = (self.clock(), key_set, kids)


def discover(config: BrowserAuthConfig,
             cache: ProviderCache | None = None) -> BrowserAuthConfig:
    """Fill endpoints from the issuer's OIDC discovery document (explicit
    issuer only; the document never overrides issuer/client/audience).
    With a cache, a validated document is reused until it expires."""
    if not config.configured:
        raise ValueError("browser sign-in is not configured")
    doc = cache.discovery(config.issuer) if cache is not None else None
    if doc is None:
        # Only the discovery URL drops trailing slashes before joining; the
        # document must name exactly the configured issuer.
        doc = _fetch_json(config.issuer.rstrip("/")
                          + "/.well-known/openid-configuration")
        if str(doc.get("issuer", "")) != config.issuer:
            raise ValueError("discovery issuer mismatch")
        if cache is not None:
            cache.store_discovery(config.issuer, doc)
    return _with_discovery(config, doc)


def cached_discovery(config: BrowserAuthConfig,
                     cache: ProviderCache) -> BrowserAuthConfig | None:
    """The discovered config from the cache alone (no I/O), or None."""
    if not config.configured:
        raise ValueError("browser sign-in is not configured")
    doc = cache.discovery(config.issuer)
    return None if doc is None else _with_discovery(config, doc)


def _with_discovery(config: BrowserAuthConfig, doc: dict) -> BrowserAuthConfig:
    return BrowserAuthConfig(
        issuer=config.issuer, client_id=config.client_id,
        audience=config.audience, redirect_uri=config.redirect_uri,
        jwks_uri=config.jwks_uri or str(doc.get("jwks_uri") or ""),
        token_endpoint=config.token_endpoint
        or str(doc.get("token_endpoint") or ""),
        authorization_endpoint=config.authorization_endpoint
        or str(doc.get("authorization_endpoint") or ""),
        allow_insecure_loopback=config.allow_insecure_loopback,
        session_ttl_s=config.session_ttl_s)


def allowed_return(path: str) -> bool:
    """Validated local return routes only: same-origin app routes, never
    absolute URLs or protocol-relative targets."""
    if not isinstance(path, str) or not path.startswith("/") \
            or path.startswith("//") or "\\" in path:
        return False
    if len(path) > 500:
        return False
    return True


def start_login(con: sqlite3.Connection, config: BrowserAuthConfig,
                return_to: str = "/", *, now: float | None = None) -> dict:
    """Begin a sign-in: one-use state/nonce/verifier, authorize redirect."""
    if not config.configured:
        raise ValueError("browser sign-in is not configured")
    if not allowed_return(return_to):
        raise ValueError("invalid return route")
    ensure_auth_schema(con)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    # Pre-auth browser secret: proves the callback arrives in the browser
    # that initiated this transaction. Only its hash is stored; the raw
    # value travels in an HttpOnly cookie the provider hop cannot read.
    tx_secret = secrets.token_urlsafe(32)
    ts = now if now is not None else time.time()
    con.execute("DELETE FROM oidc_login_txns WHERE created_at < ?",
                (ts - LOGIN_TXN_TTL_S,))
    con.execute("INSERT INTO oidc_login_txns (state_hash, code_verifier, nonce,"
                " return_to, created_at, browser_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_hash_presented(state), verifier, nonce, return_to, ts,
                 _hash_presented(tx_secret)))
    params = {"response_type": "code", "client_id": config.client_id,
              "redirect_uri": config.redirect_uri,
              "scope": "openid profile email",
              "audience": config.audience, "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    url = config.authorization_endpoint + "?" + urllib.parse.urlencode(params)
    return {"authorize_url": url, "state": state, "browser_proof": tx_secret}


def _token_kid(id_token: str) -> str:
    """The unverified header kid, used only to pick a cached key set."""
    try:
        head = id_token.split(".", 1)[0]
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
        return str(header.get("kid") or "") if isinstance(header, dict) else ""
    except (ValueError, TypeError):
        return ""


def _jwks_keys(config: BrowserAuthConfig, cache: ProviderCache | None = None,
               kid: str = ""):
    try:
        from joserfc.jwk import KeySet
    except ImportError as exc:
        raise ValueError("browser sign-in requires the hosted joserfc runtime") from exc
    if not config.jwks_uri:
        raise ValueError("provider JWKS URI is not configured")
    if cache is not None:
        cached = cache.jwks(config.jwks_uri, kid)
        if cached is not None:
            return cached
    data = _fetch_json(config.jwks_uri)
    if not isinstance(data.get("keys"), list):
        raise ProviderError("sign-in provider JWKS is malformed")
    # Only asymmetric signing keys can verify an allowed algorithm.
    usable = [k for k in data["keys"]
              if isinstance(k, dict) and k.get("kty") in ("RSA", "EC")]
    if not usable:
        raise ProviderError("sign-in provider JWKS has no usable key")
    try:
        key_set = KeySet.import_key_set({"keys": usable})
    except Exception as exc:  # noqa: BLE001 - any bad key is a provider fault
        raise ProviderError("sign-in provider JWKS is malformed") from exc
    kids = frozenset(str(k["kid"]) for k in usable if k.get("kid"))
    if cache is not None:
        cache.store_jwks(config.jwks_uri, key_set, kids)
    return key_set, kids


def validate_id_token(config: BrowserAuthConfig, id_token: str,
                      nonce: str, cache: ProviderCache | None = None) -> dict:
    """Validate an ID token through joserfc: signature over the
    provider JWKS, allow-listed asymmetric algorithm, issuer,
    audience, expiry, not-before, and the one-use nonce."""
    try:
        from joserfc import errors as _jose_errors
        from joserfc import jwt
        from joserfc.jwt import JWTClaimsRegistry
    except ImportError as exc:
        raise ValueError("browser sign-in requires the hosted joserfc runtime") from exc
    if not config.configured:
        raise ValueError("browser sign-in is not configured")
    kid = _token_kid(id_token)
    key_set, kids = _jwks_keys(config, cache, kid)
    if kid and kid not in kids:
        raise ProviderError("sign-in provider has no usable key for the token")
    try:
        token = jwt.decode(id_token, key_set, algorithms=sorted(ALLOWED_ALGS))
        JWTClaimsRegistry(
            exp={"essential": True},
            iss={"essential": True, "value": config.issuer},
            aud={"essential": True, "value": config.client_id},
            nbf={"essential": False},
        ).validate(token.claims)
        claims = token.claims
    except (_jose_errors.InvalidKeyTypeError, _jose_errors.InvalidKeyIdError,
            _jose_errors.MissingKeyError,
            _jose_errors.InvalidKeyCurveError) as exc:
        # No usable key: a provider fault, never library text to users.
        raise ProviderError("sign-in provider has no usable key") from exc
    except Exception as exc:
        raise ValueError(f"invalid ID token: {exc}") from exc
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ValueError("ID token nonce mismatch")
    subject = str(claims.get("sub") or "")
    if not subject:
        raise ValueError("ID token has no subject")
    return {"subject": subject,
            "email": str(claims.get("email") or ""),
            # Only the JSON boolean true counts: bool("false") is True.
            "email_verified": claims.get("email_verified") is True}


def claim_login(con: sqlite3.Connection, config: BrowserAuthConfig,
                params: dict, *, now: float | None = None,
                browser_proof: str = "") -> dict:
    """Validate the provider callback against its one-use transaction and
    the initiating browser's pre-auth secret, and consume it. Database
    only: a malformed or unbound callback never reaches the provider."""
    if not config.configured:
        raise ValueError("browser sign-in is not configured")
    if params.get("error"):
        raise ValueError("provider refused sign-in")
    state = str(params.get("state") or "")
    code = str(params.get("code") or "")
    if not state or not code:
        raise ValueError("callback is missing state or code")
    ensure_auth_schema(con)
    ts = now if now is not None else time.time()
    row = con.execute("SELECT code_verifier, nonce, return_to, created_at,"
                      " used, browser_hash FROM oidc_login_txns"
                      " WHERE state_hash=?",
                      (_hash_presented(state),)).fetchone()
    if row is None or row["used"] or ts - row["created_at"] > LOGIN_TXN_TTL_S:
        raise ValueError("unknown or expired sign-in request")
    stored = str(row["browser_hash"] or "")
    if (not browser_proof or not stored
            or not hmac.compare_digest(_hash_presented(browser_proof),
                                       stored)):
        # A foreign browser holding the callback URL learns nothing and
        # leaves no session behind. The hosted callback does not commit a
        # refused claim, so this mark is rolled back there: the initiating
        # browser can still finish, and a foreign retry is refused again.
        # Only a caller that commits after the error keeps it consumed.
        con.execute("UPDATE oidc_login_txns SET used=1 WHERE state_hash=?",
                    (_hash_presented(state),))
        raise ValueError("sign-in was not initiated by this browser")
    con.execute("UPDATE oidc_login_txns SET used=1 WHERE state_hash=?",
                (_hash_presented(state),))
    return {"code": code, "code_verifier": row["code_verifier"],
            "nonce": row["nonce"], "return_to": row["return_to"], "ts": ts}


def exchange_code(config: BrowserAuthConfig, claim: dict,
                  cache: ProviderCache | None = None) -> dict:
    """Exchange the claimed code and validate the ID token. Provider I/O
    only, no database: callers run it off the event loop."""
    if not config.token_endpoint:
        raise ValueError("provider token endpoint is not configured")
    tokens = _post_form(config.token_endpoint, {
        "grant_type": "authorization_code", "client_id": config.client_id,
        "code": claim["code"], "redirect_uri": config.redirect_uri,
        "code_verifier": claim["code_verifier"]})
    id_token = str(tokens.get("id_token") or "")
    if not id_token:
        raise ValueError("provider returned no ID token")
    return validate_id_token(config, id_token, claim["nonce"], cache)


def open_session(con: sqlite3.Connection, config: BrowserAuthConfig,
                 identity: dict, claim: dict) -> dict:
    """Admit the validated identity and mint an opaque server session."""
    return open_issuer_session(con, config.issuer, identity, claim,
                               session_ttl_s=config.session_ttl_s)


def open_issuer_session(con: sqlite3.Connection, issuer: str, identity: dict,
                        claim: dict, *, session_ttl_s: int = SESSION_TTL_S) -> dict:
    """The session core for any issuer (the OIDC provider or the local
    issuer): admit the identity and mint an opaque server session."""
    # Admission (L14): a principal that is not admitted gets no session.
    # An active purchase seat admits as the session lookup does; a seat
    # matches only its exact `oidc:<local issuer>:<subject>` principal.
    from . import admission as _admission
    _admission.require_admitted(issuer, identity["subject"],
                                identity["email"], identity["email_verified"],
                                seats=True)
    ts = claim["ts"]
    session_id = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    con.execute("INSERT INTO browser_sessions (session_hash, issuer, subject,"
                " email, email_verified, csrf_token, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash_presented(session_id), issuer, identity["subject"],
                 identity["email"], 1 if identity["email_verified"] else 0,
                 csrf, ts, ts + session_ttl_s))
    return {"session_id": session_id, "csrf_token": csrf,
            "issuer": issuer, "subject": identity["subject"],
            "email": identity["email"],
            "email_verified": identity["email_verified"],
            "return_to": claim["return_to"]}


def finish_login(con: sqlite3.Connection, config: BrowserAuthConfig,
                 params: dict, *, now: float | None = None,
                 browser_proof: str = "",
                 cache: ProviderCache | None = None) -> dict:
    """Validate the provider callback, exchange the code, and mint an
    opaque server-side session. One-use state; the initiating browser must
    present the pre-auth secret before any token exchange; errors never
    leak tokens. The hosted callback runs the three steps separately so
    the exchange happens off the event loop."""
    claim = claim_login(con, config, params, now=now,
                        browser_proof=browser_proof)
    identity = exchange_code(config, claim, cache)
    return open_session(con, config, identity, claim)


def _insecure_loopback_allowed(config: BrowserAuthConfig,
                                 host: str = "") -> bool:
    """Secure is omitted only under the explicit loopback test/dev flag
    and only for a loopback host."""
    bare = (host or "").split(":")[0].lower()
    return bool(config.allow_insecure_loopback
                and bare in ("localhost", "127.0.0.1", "::1"))


def session_cookie_value(config: BrowserAuthConfig, session_id: str,
                         *, host: str = "") -> str:
    """Set-Cookie value: HttpOnly + SameSite always; Secure except the
    explicit loopback test/dev exception (loopback host only)."""
    parts = [f"{SESSION_COOKIE}={session_id}", "Path=/", "HttpOnly",
             "SameSite=Lax"]
    if not _insecure_loopback_allowed(config, host):
        parts.append("Secure")
    parts.append(f"Max-Age={config.session_ttl_s}")
    return "; ".join(parts)


def clear_session_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def tx_cookie_value(config: BrowserAuthConfig, tx_secret: str,
                    *, host: str = "") -> str:
    """Pre-auth transaction cookie: HttpOnly + SameSite=Lax so it returns
    on the provider's top-level callback navigation; Secure except the
    explicit loopback test/dev exception. SameSite=Lax (not Strict) is
    required so the cookie is present after the cross-site hop."""
    parts = [f"{TX_COOKIE}={tx_secret}", "Path=/", "HttpOnly",
             "SameSite=Lax"]
    if not _insecure_loopback_allowed(config, host):
        parts.append("Secure")
    parts.append(f"Max-Age={LOGIN_TXN_TTL_S}")
    return "; ".join(parts)


def clear_tx_cookie() -> str:
    return f"{TX_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def lookup_session(con: sqlite3.Connection, session_id: str, *,
                   now: float | None = None) -> dict | None:
    """Return the live session record or None (expired, revoked, unknown).
    Revocation and expiry take effect on the next request."""
    if not session_id:
        return None
    ts = now if now is not None else time.time()
    row = con.execute("SELECT session_hash, issuer, subject, email, email_verified,"
                      " csrf_token, active_workspace, created_at, expires_at,"
                      " revoked_at, workspace_switch_revision FROM browser_sessions WHERE session_hash=?",
                      (_hash_presented(session_id),)).fetchone()
    if row is None or row["revoked_at"] is not None or ts >= row["expires_at"]:
        return None
    # Admission (L14): the file is re-read on each request, so a user
    # removed from it loses the session on the next request. Revoke only
    # when a policy loaded and excludes the principal; if no policy could
    # be loaded (a half-written file), refuse this request and keep the
    # session. Revocation is stored: re-adding the user needs a new sign-in.
    from . import admission as _admission
    verdict = _admission.decide(row["issuer"], row["subject"], row["email"],
                                bool(row["email_verified"]))
    if verdict != _admission.ADMITTED:
        if verdict == _admission.EXCLUDED:
            owned = not con.in_transaction  # a caller's transaction commits itself
            con.execute("UPDATE browser_sessions SET revoked_at=?"
                        " WHERE session_hash=? AND revoked_at IS NULL",
                        (ts, row["session_hash"]))
            if owned:
                con.commit()
        return None
    return dict(row)


def session_principal(session: dict) -> str:
    return f"oidc:{session['issuer']}:{session['subject']}"


def build_session_context(session: dict, workspace_id: str, role: str,
                          request_id: str, scopes: tuple = ()):
    """Server-constructed AccessContext for a browser session. Role comes
    from the workspace's membership (or the owner binding), never the
    request body."""
    from . import access as _access
    kind = {"owner": "owner", "editor": "editor"}.get(role, "viewer")
    ctx = _access.AccessContext.create(
        request_id=request_id, authority_kind="membership",
        principal_id=session_principal(session), principal_kind=kind,
        workspace_id=workspace_id, credential_kind="browser_session",
        session_id=session.get("session_hash", ""),
        scopes=tuple(scopes) or ("search:read", "customer:read"))
    ctx.session_binding = (session["issuer"], session["subject"])
    return ctx


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _split_host_port(host_header: str) -> tuple[str, str | None]:
    """Lowercased host plus explicit port (None when absent).

    Bracketed IPv6 first; otherwise a single colon separates host from
    port. Anything else is a bare host with no port claim.
    """
    h = (host_header or "").strip()
    if h.startswith("["):
        end = h.find("]")
        if end == -1:
            return h.lower(), None
        rest = h[end + 1:]
        port = rest[1:] if rest.startswith(":") else None
        return h[1:end].lower(), port or None
    if h.count(":") == 1:
        name, _, port = h.partition(":")
        return name.lower(), port or None
    return h.lower(), None


def validate_csrf(session: dict, presented: str | None,
                  *, origin: str = "", host: str = "",
                  scheme: str = "http") -> bool:
    """Same-origin CSRF check independent of bearer rules: the token must
    match and, when an Origin is sent, its scheme, host, and effective
    port must equal this request's own scheme, host, and effective
    port. No Origin header keeps the established token-only contract."""
    if not presented or not hmac.compare_digest(
            str(presented), str(session.get("csrf_token") or "")):
        return False
    if not origin:
        return True
    try:
        parts = urllib.parse.urlparse(origin)
        origin_port = parts.port
    except ValueError:
        return False
    origin_scheme = (parts.scheme or "").lower()
    origin_host = (parts.hostname or "").lower()
    if not origin_scheme or not origin_host:
        return False
    want_scheme = (scheme or "http").lower()
    want_host, want_port_raw = _split_host_port(host)
    if not want_host:
        return False
    try:
        want_port = int(want_port_raw) if want_port_raw else None
    except ValueError:
        return False
    if want_port is None:
        want_port = _DEFAULT_PORTS.get(want_scheme)
    if origin_port is None:
        origin_port = _DEFAULT_PORTS.get(origin_scheme)
    # A matching token never excuses a mismatching origin: same-host
    # cross-scheme or cross-port origins are rejected.
    return (origin_scheme == want_scheme and origin_host == want_host
            and origin_port is not None and origin_port == want_port)


class WorkspaceSwitchConflict(Exception):
    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__("workspace switch revision changed")


def set_active_workspace(con: sqlite3.Connection, session_id: str,
                         workspace_id: str, expected_revision: int, *,
                         now: float | None = None) -> int:
    """Consume one switch epoch on a prepared caller-owned session transaction."""
    if not con.in_transaction:
        raise RuntimeError("workspace switch requires the caller's control transaction")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValueError("expected_workspace_switch_revision must be a nonnegative integer")
    ts = time.time() if now is None else now
    session = lookup_session(con, session_id, now=ts)
    if session is None:
        from .access import AccessLost
        raise AccessLost("session unavailable")
    if session["workspace_switch_revision"] != expected_revision:
        raise WorkspaceSwitchConflict(session["workspace_switch_revision"])
    changed = con.execute(
        "UPDATE browser_sessions SET active_workspace=?,"
        " workspace_switch_revision=workspace_switch_revision+1"
        " WHERE session_hash=? AND workspace_switch_revision=?"
        " AND revoked_at IS NULL AND expires_at>?",
        (workspace_id, _hash_presented(session_id), expected_revision, ts))
    if changed.rowcount != 1:
        raise WorkspaceSwitchConflict(session["workspace_switch_revision"])
    return expected_revision + 1


def revoke_session(con: sqlite3.Connection, session_id: str, *,
                   now: float | None = None) -> None:
    ensure_auth_schema(con)
    con.execute("UPDATE browser_sessions SET revoked_at=? WHERE session_hash=?",
                (now if now is not None else time.time(),
                 _hash_presented(session_id)))


def principal_identity(issuer: str, subject: str) -> str:
    return f"oidc:{issuer}:{subject}"
