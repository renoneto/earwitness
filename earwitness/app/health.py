"""Health routes, the production configuration check and the single-process
lock for the hosted app.

Health bodies carry only fixed words: no paths, no error text, no IDs.
The configuration check names the setting that is wrong, never its value.
"""
from __future__ import annotations

import ipaddress
import os
import tempfile
from urllib.parse import urlsplit

from . import admission as _admission

LOCAL_TEST = "local-test"
PRODUCTION = "production"
MODES = (LOCAL_TEST, PRODUCTION)


class ConfigError(RuntimeError):
    """The hosted app refuses to start with this configuration."""


def liveness() -> tuple:
    return 200, {"status": "ok"}


def _control_store_ok(store) -> bool:
    try:
        con = store._connect()
        try:
            con.execute("SELECT 1 FROM connection_grants LIMIT 1").fetchall()
        finally:
            con.close()
        return True
    except Exception:  # noqa: BLE001 - any failure means unavailable
        return False


def _workspace_storage_state(data_dir: str) -> str:
    if not data_dir:
        return "not_configured"
    try:
        if not os.path.isdir(data_dir):
            return "unavailable"
        fd, probe = tempfile.mkstemp(prefix=".ready-", dir=data_dir)
        os.close(fd)
        os.remove(probe)
        return "ok"
    except OSError:
        return "unavailable"


def readiness(store, data_dir: str) -> tuple:
    checks = {"control_store": "ok" if _control_store_ok(store) else "unavailable",
              "workspace_storage": _workspace_storage_state(data_dir)}
    ready = "unavailable" not in checks.values()
    return (200 if ready else 503,
            {"status": "ready" if ready else "unready", "checks": checks})


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_problem(origin: str) -> bool:
    """True when `origin` is not an https origin (http only on loopback)."""
    try:
        parts = urlsplit(origin.strip())
        host = parts.hostname or ""
        parts.port  # raises ValueError on a bad port
    except ValueError:
        return True
    # Exact form only: the request check compares origins byte for byte,
    # so a trailing slash would pass here and refuse every browser.
    if not host or parts.path or parts.query or parts.fragment:
        return True
    if parts.scheme == "https":
        return False
    return not (parts.scheme == "http" and _loopback_host(host))


def check_config(config) -> list:
    """Return the list of problems; empty means the settings may start.

    `local-test` (explicit) skips the persistence and allowlist rules, so
    the fixture suites and local experiments still work. Both modes
    refuse more than one web process: the rate limiter is per process.
    """
    problems = []
    if config.mode not in MODES:
        problems.append("EARWITNESS_MODE must be 'production' or 'local-test'")
        return problems
    # The expected import volume first (audio clips design 4.7;
    # CLIP4-SEC-R1-02), in both modes when a pilot layout is set
    # (CLIP4-R2-02): web start-up refuses without it, before any
    # local-account, rights or other database is opened.
    volume = import_volume_problems()
    if volume:
        return volume
    if config.web_processes != 1:
        problems.append("EARWITNESS_WEB_PROCESSES must be 1: the pilot runs"
                        " one web process (the rate limiter is per process)")
    if str(config.web_concurrency or "1").strip() not in ("1", ""):
        problems.append("WEB_CONCURRENCY must be unset or 1: the pilot runs"
                        " one web process")
    low, high = 1, 64  # the workspace in-flight cap (asgi RateLimiter)
    cap = getattr(config, "workspace_max_in_flight", 10)
    if type(cap) is not int or not low <= cap <= high:
        problems.append("EARWITNESS_WORKSPACE_MAX_IN_FLIGHT must be a whole"
                        f" number from {low} to {high}")
    problems.extend(local_signin_problems(config))
    if config.mode == LOCAL_TEST:
        return problems
    grants = config.grants_db or ""
    if not grants or grants == ":memory:" or grants.startswith("file::memory:"):
        problems.append("EARWITNESS_GRANTS_DB must be a persistent file path"
                        " (not empty and not in-memory)")
    elif not os.path.isabs(grants):
        problems.append("EARWITNESS_GRANTS_DB must be an absolute path")
    elif os.path.isdir(grants):
        problems.append("EARWITNESS_GRANTS_DB must be a file path, not a directory")
    elif not os.path.isdir(os.path.dirname(grants)):
        problems.append("EARWITNESS_GRANTS_DB directory must exist")
    for name, values in (("EARWITNESS_ALLOWED_HOSTS", config.allowed_hosts),
                         ("EARWITNESS_ALLOWED_ORIGINS", config.allowed_origins)):
        if not values:
            problems.append(f"{name} must list the allowed values")
        elif any(v.strip() in ("*", "") for v in values):
            problems.append(f"{name} must not contain a wildcard")
    if config.allowed_origins and any(
            v.strip() not in ("*", "") and _origin_problem(v)
            for v in config.allowed_origins):
        problems.append("EARWITNESS_ALLOWED_ORIGINS must be https origins"
                        " (http only for loopback; 'null' is refused)")
    for name, value in (("EARWITNESS_DATA_DIR", config.data_dir),
                        ("EARWITNESS_APP_DIR", config.app_dir)):
        if not value:
            problems.append(f"{name} must be set")
        elif not os.path.isabs(value):
            problems.append(f"{name} must be an absolute path")
        elif name == "EARWITNESS_DATA_DIR" and not os.path.isdir(value):
            problems.append("EARWITNESS_DATA_DIR must exist")
    problems.extend(_resource_uri_problems(config))
    problems.extend(_admission.config_problems(
        getattr(config, "admission_file", None)))
    # Invite email (L3): when enabled, every setting must be valid and the
    # invitation link must be https. Read from the process environment,
    # the same source create_app uses to build the mailer.
    from . import invites_send as _invites_send
    problems.extend(_invites_send.config_problems(os.environ, production=True))
    # Pilot rights (hosted runtime 5.2): end date, feature list, records
    # and the permission ceiling of every live workspace.
    from . import rights_admission as _rights
    problems.extend(_rights.startup_problems(config))
    return problems


def import_volume_problems(environ=None) -> list:
    """The volume guard on the installed layout (EARWITNESS_PILOT_LAYOUT),
    when one is set."""
    from . import media as _media
    code = _media.import_volume_code(environ)
    if code is None:
        return []
    return [f"the import volume is not the expected mounted volume ({code}):"
            " the service must not start (audio clips design 4.7)"]


def _local_account_rows(grants_db: str) -> list:
    """(issuer, password_hash) of every local account, read-only; [] when
    the control store or the table does not exist yet."""
    import sqlite3
    from urllib.parse import quote
    path = str(grants_db or "")
    if not path or not os.path.isabs(path) or not os.path.isfile(path):
        return []
    try:
        con = sqlite3.connect(f"file:{quote(path, safe='/')}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return con.execute("SELECT issuer, password_hash FROM local_accounts").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def local_signin_problems(config) -> list:
    """Local email and password sign-in (PW design 3.2, T10, T13). Checked
    whenever EARWITNESS_SIGNIN_LOCAL is on or any local account exists, in
    every mode: the local issuer (the origin of EARWITNESS_PUBLIC_BASE_URL)
    must be valid, must not be the OAuth issuer, and must be the issuer of
    every row. Production also needs an https origin whose host is
    allowed, and refuses any password hash that is not scrypt-v1."""
    from . import local_accounts as _la
    on = bool(getattr(config, "signin_local", False))
    rows = _local_account_rows(getattr(config, "grants_db", ""))
    if not on and not rows:
        return []
    try:
        issuer = _la.local_issuer(getattr(config, "public_base_url", ""))
    except ValueError:
        return ["EARWITNESS_PUBLIC_BASE_URL must be the public origin while local"
                " sign-in is on or local accounts exist"]
    problems = []
    if config.mode == PRODUCTION:
        if not issuer.startswith("https://"):
            problems.append("EARWITNESS_PUBLIC_BASE_URL must be https for local sign-in")
        if (urlsplit(issuer).hostname or "") not in set(config.allowed_hosts or ()):
            problems.append("EARWITNESS_PUBLIC_BASE_URL host must be in"
                            " EARWITNESS_ALLOWED_HOSTS for local sign-in")
        if any(stored is not None and not _la.valid_hash(stored) for _i, stored in rows):
            problems.append("local_accounts holds a password hash that is not scrypt-v1")
    oauth = str(getattr(config, "issuer", "") or "").strip()
    if oauth and (oauth == issuer or oauth.rstrip("/") == issuer):
        problems.append("EARWITNESS_OAUTH_ISSUER must not be the local sign-in issuer"
                        " (the EARWITNESS_PUBLIC_BASE_URL origin)")
    if any(row_issuer != issuer for row_issuer, _s in rows):
        problems.append("local_accounts rows name another issuer than the"
                        " EARWITNESS_PUBLIC_BASE_URL origin (the host name changed)")
    return problems


def _resource_uri_problems(config) -> list:
    """The MCP resource URI is exactly https://<allowed host>/mcp.

    Tokens carry it as their audience and the 401 pointer is built from it,
    so a different shape breaks token checks or sends clients to a URL the
    app does not serve."""
    value = getattr(config, "resource_uri", "") or ""
    hosts = set(config.allowed_hosts or ())
    wanted = "https://<an EARWITNESS_ALLOWED_HOSTS host>/mcp"
    problem = [f"EARWITNESS_MCP_RESOURCE_URI must be exactly {wanted}"
               " (no userinfo, query, fragment or trailing slash)"]
    try:
        parts = urlsplit(value)
        netloc_ok = parts.netloc != "" and "@" not in parts.netloc \
            and parts.netloc in hosts
    except ValueError:
        return problem
    if (parts.scheme != "https" or not netloc_ok or parts.path != "/mcp"
            or parts.query or parts.fragment or "?" in value or "#" in value
            or value != f"https://{parts.netloc}/mcp"):
        return problem
    return []


def require_valid_config(config) -> None:
    problems = check_config(config)
    if problems:
        raise ConfigError("refusing to start: " + "; ".join(problems))


def acquire_single_process_lock(grants_db: str):
    """Hold an exclusive lock beside the control store for the life of the
    process, so a second web process on the same data refuses to start.
    Returns the open lock file; closing it releases the lock."""
    import fcntl
    handle = open(grants_db + ".web.lock", "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ConfigError("refusing to start: another web process holds this"
                          " control store; the pilot runs one web process") from None
    return handle
