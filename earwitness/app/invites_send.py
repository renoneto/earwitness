"""L3: the explicit "send invite" action and its recovery.

One invitation email is one message: a notification_outbox row of kind
workspace_invite. Its status is queued, sent, failed or uncertain.

Message identity. The first send attempt stores, in the row's detail:
the environment id, a random per-message nonce, the idempotency key
built from them, and the exact request bytes. Every later attempt for
that message replays the same bytes under the same key. The key never
derives from row ids alone, so a restored or copied database that reuses
a row id still gives a new message a new key, and two environments never
share one.

Write-ahead. The claim (identity, attempt time, status uncertain "send in
progress") commits before the provider is called; the outcome commits
after. A process that dies in between leaves an uncertain message whose
identity survived. Both steps need their own transactions, so the action
refuses a caller-owned transaction (a rollback there would erase the
claim after the provider may have accepted the email).

Uncertain messages. A missing, invalid or truncated 2xx reply, a 409,
429, 5xx or transport error is uncertain. The send action never re-sends
an uncertain message. recover_invite_message() may replay it, with the
same key and bytes, only inside the provider's idempotency window (Resend
documents 24 hours; we stop an hour early), where the provider returns
the original result instead of sending again. Past that point, or when
the provider says the key was used with other bytes, the message stays
uncertain and is marked unresolved for an operator. Nothing here promises
exactly-once delivery: a restore that loses a sent record can still lead
to a second, separately identified email.

Sending is off unless configured (mailer_from_env); the configuration is
UNAPPROVED for live use until Reno approves it. See docs/hosting/invites.md.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import access as _access
from . import collaboration as _collaboration
from . import db as _db
from . import monitoring as _monitoring
from . import ops as _ops

KIND = "workspace_invite"
INVITE_TTL = _collaboration.INVITE_TTL  # one constant for send and acceptance
# Resend keeps idempotency keys for 24 hours
# (https://resend.com/docs/dashboard/emails/idempotency-keys). Replays stop
# an hour before that, measured from our first attempt (which precedes the
# provider's receipt), so clock error cannot push a replay past it.
IDEMPOTENCY_WINDOW = timedelta(seconds=_monitoring.RESEND_IDEMPOTENCY_WINDOW_S)
WINDOW_MARGIN = timedelta(hours=1)
# A claim younger than this may still be waiting on the provider (HTTP
# timeout 20 s); recovery does not replay it.
IN_FLIGHT_LEASE = timedelta(minutes=2)
STATUSES = ("queued", "sent", "failed", "uncertain")

_ENV_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
_EMAIL_RE = re.compile(r"[^@\s/:]+@[^@\s/:]+\.[^@\s/:]+")


class OutwardSendInTransaction(RuntimeError):
    """An outward send was called inside a caller-owned transaction."""


class InviteMailer:
    """The configured invite email sender for one environment."""

    def __init__(self, provider, *, environment_id: str, public_base_url: str,
                 clock=None):
        if not _ENV_RE.fullmatch(str(environment_id or "")):
            raise ValueError("environment id must be 1-40 letters, digits, '.', '_' or '-'")
        if not str(public_base_url or "").startswith(("https://", "http://")):
            raise ValueError("public base URL must be an http(s) URL")
        self.provider = provider
        self.environment_id = environment_id
        self.public_base_url = public_base_url.rstrip("/")
        self._clock = clock

    def now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        return value.astimezone(timezone.utc)


_REQUIRED_SETTINGS = ("RESEND_API_KEY", "EARWITNESS_RESEND_FROM",
                      "EARWITNESS_ENVIRONMENT_ID", "EARWITNESS_PUBLIC_BASE_URL")


def config_problems(env, *, production: bool) -> list:
    """Problems with the invite email settings; empty when sending is off
    (EARWITNESS_INVITE_EMAIL_UNAPPROVED unset) or fully configured.
    Production also requires an https public base URL."""
    flag = str(env.get("EARWITNESS_INVITE_EMAIL_UNAPPROVED") or "")
    if not flag:
        return []
    if flag != "resend":
        return ["EARWITNESS_INVITE_EMAIL_UNAPPROVED must be 'resend' or unset"]
    problems = [f"{name} must be set when invite email is enabled"
                for name in _REQUIRED_SETTINGS if not str(env.get(name) or "")]
    env_id = str(env.get("EARWITNESS_ENVIRONMENT_ID") or "")
    if env_id and not _ENV_RE.fullmatch(env_id):
        problems.append("EARWITNESS_ENVIRONMENT_ID must be 1-40 letters, digits,"
                        " '.', '_' or '-'")
    base = str(env.get("EARWITNESS_PUBLIC_BASE_URL") or "")
    if base and production and not base.startswith("https://"):
        problems.append("EARWITNESS_PUBLIC_BASE_URL must be an https URL when"
                        " invite email is enabled in production")
    elif base and not base.startswith(("https://", "http://")):
        problems.append("EARWITNESS_PUBLIC_BASE_URL must be an http(s) URL")
    return problems


def mailer_from_env(env) -> InviteMailer | None:
    """None (sending off) unless EARWITNESS_INVITE_EMAIL_UNAPPROVED is set.
    When that flag is set every other setting is required."""
    if not str(env.get("EARWITNESS_INVITE_EMAIL_UNAPPROVED") or ""):
        return None
    problems = config_problems(env, production=False)
    if problems:
        raise ValueError("invite email is enabled but not configured: "
                         + "; ".join(problems))
    provider = _monitoring.ResendDeliveryProvider(env["RESEND_API_KEY"],
                                                  env["EARWITNESS_RESEND_FROM"])
    return InviteMailer(provider, environment_id=env["EARWITNESS_ENVIRONMENT_ID"],
                        public_base_url=env["EARWITNESS_PUBLIC_BASE_URL"])


# -- helpers ---------------------------------------------------------------

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _one(con, sql: str, args=()) -> dict | None:
    cur = con.execute(sql, args)
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], tuple(row)))


def _detail(row: dict) -> dict:
    try:
        value = json.loads(row.get("detail") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _status(state: str) -> str:
    return "queued" if state in ("pending", "queued") else state


def _result(row: dict, detail: dict | None = None, **extra) -> dict:
    detail = _detail(row) if detail is None else detail
    out = {"invite_id": detail.get("invite_id", ""), "message_id": row["id"],
           "status": _status(row["state"]),
           "unresolved": bool(detail.get("unresolved")),
           "reason": detail.get("status_reason", "")}
    if detail.get("provider_message_id"):
        out["provider_message_id"] = detail["provider_message_id"]
    out.update(extra)
    return out


def _require_autocommit(con) -> None:
    if con.in_transaction:
        raise OutwardSendInTransaction(
            "an outward send commits its own claim before calling the provider;"
            " call it outside any transaction")


def _require_mailer(mailer) -> None:
    if mailer is None:
        raise _ops.MissingPrecondition("invite email sending is not configured")


def _valid_invite(con, workspace_id: str, invite_id: str, now: datetime) -> dict:
    inv = _one(con, "SELECT * FROM invites WHERE id=?", (invite_id,))
    if inv is None:
        raise _ops.BadInput("unknown invite")
    if inv["workspace_id"] != workspace_id:
        raise _access.AuthorizationError("invite is not in this workspace", 403)
    if inv["status"] != "created":
        raise _ops.BadInput("invite is no longer pending")
    if now >= _parse(inv["created_at"]) + INVITE_TTL:
        raise _ops.BadInput("invite has expired")
    if not _EMAIL_RE.fullmatch(str(inv["member"] or "")):
        raise _ops.BadInput("invite has no email address to send to")
    return inv


def _latest_message(con, workspace_id: str, invite_id: str) -> dict | None:
    return _one(con,
                "SELECT * FROM notification_outbox WHERE workspace_id=?"
                " AND event_id IS NULL AND CASE WHEN json_valid(detail) THEN"
                " json_extract(detail, '$.kind')=? AND"
                " json_extract(detail, '$.invite_id')=? END"
                " ORDER BY id DESC LIMIT 1", (workspace_id, KIND, invite_id))


def _message_row(con, workspace_id: str, outbox_id: int) -> dict:
    row = _one(con, "SELECT * FROM notification_outbox WHERE id=?", (int(outbox_id),))
    if row is None or row["workspace_id"] != workspace_id or \
            _detail(row).get("kind") != KIND:
        raise _ops.BadInput("unknown invite message")
    return row


def _new_identity(mailer: InviteMailer, workspace_id: str, hosted_workspace_id: str,
                  inv: dict, display_name: str, now: datetime) -> dict:
    nonce = uuid.uuid4().hex
    scope = hashlib.sha256(
        f"{hosted_workspace_id}|{workspace_id}".encode()).hexdigest()[:12]
    key = f"earwitness-invite/{mailer.environment_id}/{scope}/{nonce}"
    link = (f"{mailer.public_base_url}/#/invite?workspace={hosted_workspace_id}"
            f"&id={inv['id']}")
    name = display_name or hosted_workspace_id
    subject = f"You're invited to {name} on EarWitness"
    text = (f"You've been invited to join the workspace \"{name}\" on EarWitness"
            f" as {inv['role']}.\n\n"
            f"To accept, open this link and sign in with this email address:\n{link}\n\n"
            f"The invitation expires on {_iso(_parse(inv['created_at']) + INVITE_TTL)[:10]}."
            " If you weren't expecting it, you can ignore this email.\n")
    body = mailer.provider.build_body(inv["member"], subject, text)
    return {"environment": mailer.environment_id, "nonce": nonce,
            "idempotency_key": key, "body": body.decode("ascii"),
            "first_attempt_at": _iso(now), "attempts": 0}


def _claim(con, row_id: int, detail: dict, now: datetime) -> str:
    token = uuid.uuid4().hex
    message = detail["message"]
    message["claim"] = token
    message["claimed_at"] = _iso(now)
    message["attempts"] = int(message.get("attempts") or 0) + 1
    detail["status_reason"] = "send in progress"
    con.execute("UPDATE notification_outbox SET state='uncertain', detail=? WHERE id=?",
                (json.dumps(detail), row_id))
    return token


def _audit(con, actor: str, action: str, row: dict, detail: dict, status: str) -> None:
    _db.audit(con, actor or "system", action, "membership", row["recipient"],
              {"workspace": row["workspace_id"], "invite_id": detail.get("invite_id"),
               "message_id": row["id"], "status": status}, _db.bump_revision(con))


class PendingSend:
    """One send split into three steps so the provider call can leave an
    event loop: the claim has committed (prepare_*), transmit() calls only
    the provider (no database), finish() records the outcome. When no call
    is needed, result holds the answer and needs_transmit is False."""

    def __init__(self, *, result: dict | None = None, row_id: int | None = None,
                 token: str = "", body: bytes = b"", key: str = "", actor: str = "",
                 mailer: InviteMailer | None = None):
        self.result = result
        self.row_id = row_id
        self.token = token
        self.body = body
        self.key = key
        self.actor = actor
        self.mailer = mailer

    @property
    def needs_transmit(self) -> bool:
        return self.result is None

    def transmit(self) -> dict:
        """The provider call only. Safe to run in a worker thread."""
        return self.mailer.provider.send_prepared(self.body, self.key)

    def finish(self, con: sqlite3.Connection, outcome: dict) -> dict:
        """Record the outcome. A confirmed send always wins over an
        uncertain row. Any other outcome is recorded only while this
        attempt's claim is still current, so a late, superseded attempt
        never overwrites newer state."""
        _require_autocommit(con)
        with _db.transaction(con, write=True):
            row = _one(con, "SELECT * FROM notification_outbox WHERE id=?", (self.row_id,))
            detail = _detail(row)
            current = detail.get("message") or {}
            if row["state"] != "uncertain" or (
                    outcome["outcome"] != "sent" and current.get("claim") != self.token):
                return _result(row, detail)
            if outcome["outcome"] == "sent":
                state = "sent"
                detail["provider_message_id"] = outcome["message_id"]
                detail["status_reason"] = ""
                detail["unresolved"] = False
            elif outcome["outcome"] == "failed" and int(current.get("attempts") or 0) == 1:
                state = "failed"
                detail["status_reason"] = outcome["reason"]
            else:
                # Ambiguous, or a refusal of a replay (the first attempt may
                # still have been accepted): stays uncertain.
                state = "uncertain"
                detail["status_reason"] = outcome["reason"]
                if not outcome.get("replayable", True):
                    current["no_replay"] = True
                    detail["unresolved"] = True
            con.execute("UPDATE notification_outbox SET state=?, detail=? WHERE id=?",
                        (state, json.dumps(detail), self.row_id))
            row = dict(row, state=state)
            _audit(con, self.actor, "invite_send_result", row, detail, _status(state))
            return _result(row, detail)

    def run(self, con: sqlite3.Connection) -> dict:
        """All three steps in this thread (library and tests)."""
        if not self.needs_transmit:
            return self.result
        return self.finish(con, self.transmit())


def _pending(row_id: int, token: str, detail: dict, actor: str,
             mailer: InviteMailer) -> PendingSend:
    message = detail["message"]
    return PendingSend(row_id=row_id, token=token, body=message["body"].encode("ascii"),
                       key=message["idempotency_key"], actor=actor, mailer=mailer)


# -- public actions ----------------------------------------------------------

def prepare_send(con: sqlite3.Connection, *, workspace_id: str, hosted_workspace_id: str,
                 invite_id: str, actor: str, mailer: InviteMailer | None,
                 now: datetime | None = None) -> PendingSend:
    """Step 1 of "send invite": check and claim, committed.

    At send time, under the write lock: the invite is pending, unexpired
    and addressed to an email, and actor may still manage membership of
    workspace_id. A sent message is not sent again; an uncertain one is
    never re-sent here (see recover_invite_message). A queued message is
    claimed; a failed or missing one gets a new message with a new
    identity."""
    _require_autocommit(con)
    _require_mailer(mailer)
    from . import customer as _customer
    with _db.transaction(con, write=True):
        now = now or mailer.now()
        if not _collaboration.check_permission(con, workspace_id, actor, "membership"):
            raise _access.AuthorizationError("owner manages membership", 403)
        inv = _valid_invite(con, workspace_id, invite_id, now)
        latest = _latest_message(con, workspace_id, invite_id)
        if latest is not None and latest["state"] == "sent":
            return PendingSend(result=_result(latest))
        if latest is not None and latest["state"] == "uncertain":
            detail = _detail(latest)
            message = detail.get("message") if isinstance(detail.get("message"), dict) else {}
            in_flight = (detail.get("status_reason") == "send in progress"
                         and message.get("first_attempt_at")
                         and now < _last_attempt(message) + IN_FLIGHT_LEASE)
            return PendingSend(result=_result(
                latest, detail, reason="send in progress" if in_flight
                else "reconcile the uncertain send before any resend"))
        # The workspace name is member text that start-up does not scan; an
        # older one may hold a recording id. The message copies it into the
        # fully scanned outbox, so it is checked here, before any write or
        # provider call (CLIPCORE-SEC-R13-01). A BLOB name is decoded once
        # and that text is both checked and rendered; bytes that are not
        # UTF-8 are refused the same way (CLIPCORE-SEC-R14-01).
        from . import rights_admission as _rights
        try:
            display_name = _rights.stored_text(
                _customer.workspace_display_name(con, workspace_id)).strip()
        except UnicodeDecodeError:
            raise _ops.BadInput(_rights.RECORDING_COPY_REFUSAL) from None
        if _rights.has_recording_id(display_name):
            raise _ops.BadInput(_rights.RECORDING_COPY_REFUSAL)
        if latest is not None and latest["state"] in ("pending", "queued"):
            row_id, detail = latest["id"], _detail(latest)
        else:
            detail = {"kind": KIND, "invite_id": invite_id, "role": inv["role"]}
            row_id = con.execute(
                "INSERT INTO notification_outbox (workspace_id, event_id, recipient,"
                " state, detail, created_at) VALUES (?, NULL, ?, 'queued', ?, ?)",
                (workspace_id, inv["member"], json.dumps(detail), _iso(now))).lastrowid
        message = detail.get("message")
        if not isinstance(message, dict) or message.get("environment") != mailer.environment_id:
            detail["message"] = _new_identity(
                mailer, workspace_id, hosted_workspace_id, inv, display_name, now)
        token = _claim(con, row_id, detail, now)
        row = _one(con, "SELECT * FROM notification_outbox WHERE id=?", (row_id,))
        _audit(con, actor, "invite_send_attempt", row, detail, "uncertain")
    return _pending(row_id, token, detail, actor, mailer)


def send_invite(con: sqlite3.Connection, **kwargs) -> dict:
    """prepare_send, the provider call and the outcome, in this thread.
    An event loop calls prepare_send and runs transmit() in a thread."""
    return prepare_send(con, **kwargs).run(con)


def prepare_recover(con: sqlite3.Connection, *, workspace_id: str, outbox_id: int,
                    mailer: InviteMailer | None, now: datetime | None = None,
                    actor: str = "recovery") -> PendingSend:
    """Step 1 of reconciling one uncertain invite message.

    Replays the stored bytes under the stored key only when the message
    belongs to this environment, its last attempt is older than the
    in-flight lease, the invite is still pending, and the first attempt is
    inside the idempotency window (less the margin). Otherwise the message
    is marked unresolved (a message still in flight is left alone) and is
    never resent automatically."""
    _require_autocommit(con)
    _require_mailer(mailer)
    with _db.transaction(con, write=True):
        now = now or mailer.now()
        row = _message_row(con, workspace_id, outbox_id)
        detail = _detail(row)
        if row["state"] != "uncertain":
            return PendingSend(result=_result(row, detail))
        message = detail.get("message")
        if not isinstance(message, dict) or not message.get("idempotency_key"):
            return PendingSend(result=_unresolved(con, row, detail,
                                                  "no recorded message identity"))
        if message.get("environment") != mailer.environment_id:
            return PendingSend(result=_unresolved(
                con, row, detail,
                f"message belongs to environment {message.get('environment')!r};"
                " never replayed from another environment"))
        if message.get("no_replay"):
            return PendingSend(result=_result(row, detail))
        if now >= _parse(message["first_attempt_at"]) + IDEMPOTENCY_WINDOW - WINDOW_MARGIN:
            return PendingSend(result=_unresolved(
                con, row, detail,
                "past the provider idempotency window; never resent automatically"))
        if now < _last_attempt(message) + IN_FLIGHT_LEASE:
            return PendingSend(result=_result(row, detail, reason="send in progress"))
        inv = _one(con, "SELECT status FROM invites WHERE id=? AND workspace_id=?",
                   (detail.get("invite_id"), workspace_id))
        if inv is None or inv["status"] != "created":
            return PendingSend(result=_unresolved(
                con, row, detail, "invite is no longer pending; reconcile manually"))
        token = _claim(con, row["id"], detail, now)
    return _pending(row["id"], token, detail, actor, mailer)


def recover_invite_message(con: sqlite3.Connection, **kwargs) -> dict:
    return prepare_recover(con, **kwargs).run(con)


def _last_attempt(message: dict) -> datetime:
    return _parse(message.get("claimed_at") or message["first_attempt_at"])


def _unresolved(con, row: dict, detail: dict, reason: str) -> dict:
    if not detail.get("unresolved") or detail.get("status_reason") != reason:
        detail["unresolved"] = True
        detail["status_reason"] = reason
        con.execute("UPDATE notification_outbox SET detail=? WHERE id=? AND state='uncertain'",
                    (json.dumps(detail), row["id"]))
    return _result(row, detail)


def invite_message_status(con: sqlite3.Connection, workspace_id: str, outbox_id: int) -> dict:
    return _result(_message_row(con, workspace_id, outbox_id))


def unresolved_messages(con: sqlite3.Connection, workspace_id: str,
                        now: datetime | None = None) -> list:
    """Every uncertain invite message whose last attempt is older than the
    in-flight lease, for an operator: each needs reconciliation before the
    invite can be sent again. Read-only; sends nothing."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows = con.execute(
        "SELECT * FROM notification_outbox WHERE workspace_id=? AND state='uncertain'"
        " AND event_id IS NULL AND CASE WHEN json_valid(detail) THEN"
        " json_extract(detail, '$.kind')=? END ORDER BY id", (workspace_id, KIND))
    names = [c[0] for c in rows.description]
    out = []
    for raw in rows.fetchall():
        row = dict(zip(names, tuple(raw)))
        detail = _detail(row)
        message = detail.get("message") if isinstance(detail.get("message"), dict) else {}
        first = _parse(message.get("first_attempt_at") or row["created_at"])
        last = _parse(message.get("claimed_at") or message.get("first_attempt_at")
                      or row["created_at"])
        if now < last + IN_FLIGHT_LEASE:
            continue
        reason = detail.get("status_reason") or ""
        if not reason or reason == "send in progress":
            reason = "no outcome was recorded (the sending process may have stopped)"
        out.append(_result(row, detail, reason=reason,
                           age_s=int((now - first).total_seconds()),
                           last_attempt_at=_iso(last)))
    return out
