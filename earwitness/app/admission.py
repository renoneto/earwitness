"""Pilot admission gate (L14): who may sign in and create workspaces.

The pilot is invite-only. An admission file lists the principals
(`oidc:<issuer>:<subject>`) and, optionally, verified email addresses that
are admitted. The file is named by `EARWITNESS_ADMISSION_FILE`.

Pilot limits (HR2-11). In production the file admits by principal only:
`verified_emails` must be empty, because one email can be presented by
more than one subject. The distinct principals are then the account set,
and `max_accounts` caps it; `max_workspaces` caps registered workspaces
(checked by `services.collab_create_workspace`, in its control write
transaction). Production requires both, and `max_workspaces` of 1 or
more; a file without them stops start-up. The numbers are the owner's.

`removed_principals` lists accounts that were removed from the pilot. A
principal listed there is refused even when it is also in `principals` or
its email is verified (removal wins), and it does not count toward
`max_accounts`. `retired_workspace_ids` lists workspace IDs that are never
created again (`services.collab_create_workspace` refuses them like a
taken ID). Nothing leaves either list during the pilot.

- production (`EARWITNESS_MODE=production`) with no file: everyone is
  denied. The production configuration check also refuses to start.
- local-test (anything else) with no file: no gate, behaviour unchanged.
- A configured file is enforced in every mode.

An email admits an account only when the ID token carried
`email_verified: true`, and only an ASCII email: Unicode case folding
would merge distinct addresses, so a non-ASCII email never admits.
An unreadable or malformed file denies everyone at run time (fail closed)
and is refused at start in production.

Decisions have three outcomes. ADMITTED and EXCLUDED come from a policy
that loaded. UNAVAILABLE means no policy could be loaded (no file in
production, or a missing, unreadable or malformed file): the request is
denied, but a stored session is never revoked on that evidence, so an
edit caught half way cannot sign everyone out.

Purchase seats (purchase design 5.2, BY2). An optional block

    "purchase_seats": {"enabled": true,
      "pools": {"ew-a": ["<workspace id>", ...], "ew-b": [...]}}

lets `decide` also admit a principal P that holds a seat in state
`active` (purchases.py) whose workspace is in the pool of that seat's
offer, while `enabled` is true. `removed_principals` beats both rules.
Seats are read from the control store (EARWITNESS_GRANTS_DB, read-only)
on each decision; a failed read is UNAVAILABLE (deny, never revoke).
A seat admits sign-in and sessions only: `require_admitted` (used by
workspace creation) and bearer grants (`decide_principal`) stay
principals-only unless a caller passes `seats=True`, so a seat can never
create a workspace. Its holder reaches only the workspaces where they are
a member, which the window makes their own pool workspace.

Every load refuses a workspace in two pools and `max_accounts` below the
principals plus the total pool size. `config_problems` (production
start-up) also refuses a comped pool workspace, an unregistered one (unless
retired and in `retired_workspace_ids` with a `retired` seat),
`max_workspaces` below the registered workspaces plus every pool, a
Payment Link limit (EARWITNESS_PURCHASE_LINK_LIMITS) above its pool and,
while enabled, an offer with a pool that has no stated link limit or no
price in EARWITNESS_BILLING_PRICES tagged with that offer.

Refusals say nothing about the list: no names, counts or reasons.
"""
from __future__ import annotations

import json
import logging
import os

from . import access as _access

ENV_FILE = "EARWITNESS_ADMISSION_FILE"
ENV_CONTROL = "EARWITNESS_GRANTS_DB"
ENV_LINK_LIMITS = "EARWITNESS_PURCHASE_LINK_LIMITS"
ENV_COMPED = "EARWITNESS_BILLING_COMPED_WORKSPACES"
ENV_PRICES = "EARWITNESS_BILLING_PRICES"
OFFERS = ("ew-a", "ew-b")
ENV_MODE = "EARWITNESS_MODE"
APPROVED = "approved"
UNAPPROVED = "UNAPPROVED"
REFUSAL = "this account is not admitted to the pilot"
ADMITTED, EXCLUDED, UNAVAILABLE = "admitted", "excluded", "unavailable"
_KEYS = frozenset({"status", "principals", "verified_emails",
                   "max_accounts", "max_workspaces", "removed_principals",
                   "retired_workspace_ids", "purchase_seats"})
WORKSPACE_REFUSAL = "the pilot workspace limit has been reached"
LOGGER = logging.getLogger("earwitness.admission")


class AdmissionError(ValueError):
    """The admission file is missing or malformed."""


class AdmissionDenied(_access.AuthorizationError, ValueError):
    """The account is not admitted. HTTP 403; also a ValueError so the
    sign-in callback maps it like any other refused sign-in."""

    def __init__(self, message: str = REFUSAL):
        super().__init__(message, 403)


class Policy:
    def __init__(self, status: str, principals: frozenset,
                 verified_emails: frozenset, max_accounts=None,
                 max_workspaces=None, removed_principals=frozenset(),
                 retired_workspace_ids=frozenset(), purchase_enabled=False,
                 pools=None):
        self.status = status
        self.principals = principals
        self.verified_emails = verified_emails
        self.max_accounts = max_accounts
        self.max_workspaces = max_workspaces
        self.removed_principals = removed_principals
        self.retired_workspace_ids = retired_workspace_ids
        self.purchase_enabled = purchase_enabled
        # offer -> tuple of workspace IDs; None when the file has no block.
        self.pools = pools

    def pool_workspaces(self) -> frozenset:
        return frozenset(w for ids in (self.pools or {}).values() for w in ids)

    def admits(self, issuer: str, subject: str, email: str = "",
               email_verified: bool = False) -> bool:
        if issuer and subject:
            principal = f"oidc:{issuer}:{subject}"
            if principal in self.removed_principals:
                return False  # removal wins, even over a verified email
            if self.admits_principal(principal):
                return True
        email = str(email or "").strip()
        return bool(email_verified is True and email and email.isascii()
                    and email.lower() in self.verified_emails)

    def admits_principal(self, principal: str) -> bool:
        return (principal in self.principals
                and principal not in self.removed_principals)


def _norm_email(value: str) -> str:
    return str(value).strip().lower()


def _string_list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() for v in value):
        raise AdmissionError(f"admission file: {key} must be a list of"
                             " non-empty strings")
    return value


def _is_production(env=None) -> bool:
    return str((os.environ if env is None else env).get(ENV_MODE)
               or "") == "production"


def _pools(data: dict):
    """(enabled, {offer: tuple of IDs}) from the purchase_seats block, or
    (False, None) without one. A workspace listed twice is refused."""
    if "purchase_seats" not in data:
        return False, None
    block = data["purchase_seats"]
    if not isinstance(block, dict) or set(block) - {"enabled", "pools"}:
        raise AdmissionError("admission file: purchase_seats must be an object"
                             " with enabled and pools")
    enabled = block.get("enabled")
    if not isinstance(enabled, bool):
        raise AdmissionError("admission file: purchase_seats.enabled must be"
                             " true or false")
    pools = block.get("pools", {})
    if not isinstance(pools, dict) or set(pools) - set(OFFERS):
        raise AdmissionError("admission file: purchase_seats.pools must map"
                             " ew-a and ew-b to workspace ID lists")
    from . import connections as _connections  # the workspace ID rule
    seen, out = set(), {}
    for offer in OFFERS:
        ids = _string_list(pools, offer)
        if any(not _connections._workspace_id_pattern().fullmatch(w)
               for w in ids):
            raise AdmissionError("admission file: purchase_seats.pools must"
                                 " list workspace IDs")
        if seen & set(ids) or len(set(ids)) != len(ids):
            raise AdmissionError("admission file: a workspace is in two"
                                 " purchase pools")
        seen |= set(ids)
        out[offer] = tuple(ids)
    return enabled, out


def _cap(data: dict, key: str, *, required: bool, minimum: int = 0):
    if key not in data:
        if required:
            raise AdmissionError(f"admission file: {key} is required in"
                                 " production")
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AdmissionError(f"admission file: {key} must be a whole number"
                             f" of {minimum} or more")
    return value


def load_file(path: str, *, warn: bool = True,
              production: bool | None = None) -> Policy:
    """Read and validate the admission file; raise AdmissionError.

    `production` selects the production rules (principals only, and
    `max_accounts` and `max_workspaces` required); None reads EARWITNESS_MODE."""
    if production is None:
        production = _is_production()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise AdmissionError("admission file is unreadable or not JSON") from exc
    if not isinstance(data, dict):
        raise AdmissionError("admission file must be a JSON object")
    if set(data) - _KEYS:
        raise AdmissionError("admission file has an unknown key")
    status = data.get("status")
    if not isinstance(status, str) or not status:
        raise AdmissionError("admission file: status must be a string")
    principals = _string_list(data, "principals")
    if any(not p.startswith("oidc:") for p in principals):
        raise AdmissionError("admission file: principals must be"
                             " oidc:<issuer>:<subject>")
    removed = _string_list(data, "removed_principals")
    if any(not p.startswith("oidc:") for p in removed):
        raise AdmissionError("admission file: removed_principals must be"
                             " oidc:<issuer>:<subject>")
    for key, listed in (("principals", principals),
                        ("removed_principals", removed)):
        if any(p != p.strip() for p in listed):
            raise AdmissionError(f"admission file: {key} must not have"
                                 " leading or trailing whitespace")
    retired = _string_list(data, "retired_workspace_ids")
    from . import connections as _connections  # the workspace ID rule
    if any(not _connections._workspace_id_pattern().fullmatch(w)
           for w in retired):
        raise AdmissionError("admission file: retired_workspace_ids must be"
                             " workspace IDs (lowercase letters, digits and"
                             " hyphens, 1 to 64 characters)")
    emails = _string_list(data, "verified_emails")
    if any("@" not in e or not e.isascii() for e in emails):
        raise AdmissionError("admission file: verified_emails must be ASCII"
                             " email addresses")
    if production and emails:
        raise AdmissionError("admission file: verified_emails must be empty"
                             " in production (principals only)")
    max_accounts = _cap(data, "max_accounts", required=production)
    max_workspaces = _cap(data, "max_workspaces", required=production,
                          minimum=1 if production else 0)
    enabled, pools = _pools(data)
    pool_size = sum(len(ids) for ids in (pools or {}).values())
    if max_accounts is not None and len(
            set(principals) - set(removed)) + pool_size > max_accounts:
        raise AdmissionError("admission file: more principals than"
                             " max_accounts" if not pool_size else
                             "admission file: max_accounts is below the"
                             " principals plus the purchase pool size")
    if warn and status != APPROVED:
        LOGGER.warning("admission file status is %r, not 'approved': the"
                       " pilot user list has not been approved by the owner",
                       status)
    return Policy(status, frozenset(principals),
                  frozenset(_norm_email(e) for e in emails),
                  max_accounts, max_workspaces, frozenset(removed),
                  frozenset(retired), enabled, pools)


def configured_path(env=None) -> str:
    return str((os.environ if env is None else env).get(ENV_FILE) or "").strip()


def _policy(env=None):
    """(state, policy): policy is set only when a file loaded."""
    source = os.environ if env is None else env
    path = configured_path(source)
    if not path:
        if str(source.get(ENV_MODE) or "") == "production":
            return UNAVAILABLE, None  # default deny: no policy exists
        return ADMITTED, None  # no gate outside production
    try:
        return EXCLUDED, load_file(path, warn=False,
                                   production=_is_production(source))
    except AdmissionError:
        return UNAVAILABLE, None  # fail closed


def _open_control(source):
    import sqlite3
    from urllib.parse import quote
    path = str(source.get(ENV_CONTROL) or "").strip()
    if not path or path == ":memory:" or not os.path.isfile(path):
        raise sqlite3.OperationalError("control store unavailable")
    return sqlite3.connect("file:" + quote(os.path.abspath(path), safe="/")
                           + "?mode=ro", uri=True)


def _seat_admits(policy: Policy, principal: str, source, control=None):
    """True when P holds an `active` seat whose workspace is in its
    offer's pool, False when it does not, None when the seats could not
    be read."""
    import sqlite3
    from . import purchases as _purchases
    try:
        con = control if control is not None else _open_control(source)
    except sqlite3.Error:
        return None
    try:
        row = _purchases.holding_seat(con, principal)
    except sqlite3.Error:
        return None
    finally:
        if control is None:
            con.close()
    return bool(row is not None
                and row["state"] in _purchases.ADMITTING_STATES
                and row["workspace_id"] in (policy.pools or {}).get(row["offer"], ()))


def decide(issuer: str, subject: str, email: str = "",
           email_verified: bool = False, *, env=None, seats: bool = True,
           control=None) -> str:
    """ADMITTED, EXCLUDED (a loaded policy leaves the account out) or
    UNAVAILABLE (no policy could be loaded, or the seats could not be read;
    deny, but never revoke). The file is re-read on each decision so edits
    apply without a restart. With `seats` (the default here), an `active`
    purchase seat in its offer's pool admits too, while the block is
    enabled; `removed_principals` wins over it."""
    source = os.environ if env is None else env
    state, policy = _policy(source)
    if policy is None:
        return state
    if policy.admits(issuer, subject, email, email_verified):
        return ADMITTED
    if not (seats and policy.purchase_enabled and issuer and subject):
        return EXCLUDED
    principal = f"oidc:{issuer}:{subject}"
    if principal in policy.removed_principals:
        return EXCLUDED
    held = _seat_admits(policy, principal, source, control)
    if held is None:
        return UNAVAILABLE
    return ADMITTED if held else EXCLUDED


def decide_principal(principal: str, *, env=None) -> str:
    """Like `decide` for a bearer grant's `oidc:<iss>:<sub>` member. The
    principal only: there is no email path for a grant."""
    state, policy = _policy(env)
    if policy is None:
        return state
    return ADMITTED if policy.admits_principal(principal) else EXCLUDED


def is_admitted(issuer: str, subject: str, email: str = "",
                email_verified: bool = False, *, env=None,
                seats: bool = False, control=None) -> bool:
    return decide(issuer, subject, email, email_verified, env=env,
                  seats=seats, control=control) == ADMITTED


def is_principal_admitted(principal: str, *, env=None) -> bool:
    return decide_principal(principal, env=env) == ADMITTED


def require_admitted(issuer: str, subject: str, email: str = "",
                     email_verified: bool = False, *, env=None,
                     seats: bool = False, control=None) -> None:
    """Principals (or verified emails) only, unless `seats=True`: workspace
    creation calls this as it is, so a purchase seat never creates one."""
    if not is_admitted(issuer, subject, email, email_verified, env=env,
                       seats=seats, control=control):
        raise AdmissionDenied()


def workspace_limit(*, env=None):
    """The workspace cap: an int, or None for no limit (outside production,
    with no file or no `max_workspaces`). Production with no usable value
    gives 0, so creation is refused. A file that cannot load gives 0."""
    source = os.environ if env is None else env
    production = _is_production(source)
    state, policy = _policy(source)
    if policy is None:
        return None if state == ADMITTED else 0
    if policy.max_workspaces is None:
        return 0 if production else None
    return policy.max_workspaces


def is_workspace_retired(workspace_id: str, *, env=None) -> bool:
    """True when workspace_id may not be created: a loaded policy retires
    it, or no policy could be loaded on this read (fail closed, even if an
    earlier read in the same request succeeded). No file outside
    production means no gate, so nothing is retired."""
    state, policy = _policy(env)
    if policy is None:
        return state == UNAVAILABLE
    return workspace_id in policy.retired_workspace_ids


def retired_workspace_ids(*, env=None):
    """The retired workspace IDs on one read of the policy, or None when no
    policy could be loaded on this read (fail closed: the caller decides
    nothing). No file outside production retires nothing."""
    state, policy = _policy(env)
    if policy is None:
        return None if state == UNAVAILABLE else frozenset()
    return policy.retired_workspace_ids


def check_workspace_capacity(count: int, *, env=None) -> None:
    """Refuse (403) when `count` registered workspaces already meet the
    cap. Call it inside the control write transaction that registers the
    workspace, with the count read in that transaction."""
    limit = workspace_limit(env=env)
    if limit is not None and count >= limit:
        raise AdmissionDenied(WORKSPACE_REFUSAL)


def config_problems(path=None, env=None) -> list:
    """Production start-up rules: a file must be named and valid, list
    principals only, keep removed principals out of `principals` and stay
    within `max_accounts`. Names
    the setting, never its value. `path` is the file the configuration
    names; only when none is given does the environment decide."""
    if path is None:
        path = configured_path(env)
    path = str(path or "").strip()
    if not path:
        return [f"{ENV_FILE} must name the admission file (default deny:"
                " production admits no one without it)"]
    try:
        policy = load_file(path, production=True)
    except AdmissionError as exc:
        return [f"{ENV_FILE} must name a valid admission file ({exc})"]
    if policy.principals & policy.removed_principals:
        return [f"{ENV_FILE}: a principal is in both principals and"
                " removed_principals (removal wins; take it out of"
                " principals)"]
    return purchase_problems(policy, env)


def configured_pools(env=None) -> tuple:
    """Every purchase pool workspace of the configured admission file, or
    () when there is no file, no block, or the file does not load (the
    admission gate then fails closed on its own)."""
    source = os.environ if env is None else env
    path = configured_path(source)
    if not path:
        return ()
    try:
        policy = load_file(path, warn=False, production=_is_production(source))
    except AdmissionError:
        return ()
    return tuple(sorted(policy.pool_workspaces()))


def _link_limits(source):
    """{offer: limit} from EARWITNESS_PURCHASE_LINK_LIMITS, None when unset;
    ValueError when malformed."""
    raw = str(source.get(ENV_LINK_LIMITS) or "").strip()
    if not raw:
        return None
    loaded = json.loads(raw)
    if not isinstance(loaded, dict) or set(loaded) - set(OFFERS) or any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0
            for v in loaded.values()):
        raise ValueError("malformed")
    return loaded


def _control_state(source) -> tuple:
    """(registered workspace IDs, {workspace_id: seat state}) read-only
    from EARWITNESS_GRANTS_DB; a store that does not exist yet has none."""
    import sqlite3
    path = str(source.get(ENV_CONTROL) or "").strip()
    if not path or not os.path.isfile(path):
        return set(), {}
    con = _open_control(source)
    try:
        registry = {r[0] for r in con.execute(
            "SELECT workspace_id FROM workspace_registry")}
        try:
            seats = dict(con.execute(
                "SELECT workspace_id, state FROM purchase_seats"
                " WHERE workspace_id IS NOT NULL").fetchall())
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            seats = {}
    finally:
        con.close()
    return registry, seats


def purchase_problems(policy: Policy, env=None) -> list:
    """Start-up rules for the purchase_seats block (design 5.2). Names
    the rule, never a workspace or a principal."""
    if policy.pools is None:
        return []
    import sqlite3
    source = os.environ if env is None else env
    problems = []
    pool_ids = policy.pool_workspaces()
    comped = {w.strip() for w in str(source.get(ENV_COMPED) or "").split(",")
              if w.strip()}
    if pool_ids & comped:
        problems.append(f"{ENV_FILE}: a purchase pool workspace is also in"
                        f" {ENV_COMPED}")
    try:
        registry, seats = _control_state(source)
    except sqlite3.Error:
        return problems + [f"{ENV_CONTROL}: the workspace registry and"
                           " purchase seats could not be read"]
    for workspace_id in sorted(pool_ids - registry):
        if not (workspace_id in policy.retired_workspace_ids
                and seats.get(workspace_id) == "retired"):
            problems.append(f"{ENV_FILE}: a purchase pool workspace is not"
                            " registered (only a retired seat's workspace"
                            " listed in retired_workspace_ids may be)")
            break
    if policy.max_workspaces is not None and \
            policy.max_workspaces < len(registry | pool_ids):
        problems.append(f"{ENV_FILE}: max_workspaces is below the registered"
                        " workspaces plus every purchase pool")
    try:
        limits = _link_limits(source)
    except ValueError:
        return problems + [f"{ENV_LINK_LIMITS} must be a JSON object of"
                           " ew-a and ew-b to whole numbers"]
    sold = {offer for offer in OFFERS if policy.pools.get(offer)}
    if policy.purchase_enabled and (limits is None or sold - set(limits)):
        # Every offer with a pool needs its limit (an unused link may be
        # stated as 0); an omitted one could not be checked below.
        problems.append(f"{ENV_LINK_LIMITS} must state each Payment Link"
                        " limit while purchase_seats is enabled")
    if limits is not None and any(limit > len(policy.pools.get(offer, ()))
                                  for offer, limit in limits.items()):
        problems.append(f"{ENV_LINK_LIMITS}: a Payment Link limit is above"
                        " its plan's pool size")
    if policy.purchase_enabled and sold - _tagged_offers(source):
        # Without one, every seat delivery of that offer is the fixed 404
        # (billing.offer_prices), and its buyers lose access at period end.
        problems.append(f"{ENV_PRICES} must tag a price with each plan offer"
                        " that has a purchase pool (\"offer\": \"ew-a\" or"
                        " \"ew-b\")")
    return problems


def _tagged_offers(source) -> set:
    """The offers that EARWITNESS_BILLING_PRICES tags on at least one price,
    read as billing.config_from_env reads the setting."""
    try:
        prices = json.loads(str(source.get(ENV_PRICES) or ""))
    except ValueError:
        return set()
    if not isinstance(prices, dict):
        return set()
    return {meta.get("offer") for meta in prices.values()
            if isinstance(meta, dict) and isinstance(meta.get("offer"), str)}
