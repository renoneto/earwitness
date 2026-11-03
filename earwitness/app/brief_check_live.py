"""LIVE-BRIEF-CHECK (#7): a member presses "Check this brief" on a placement
and Muse Spark checks that placement's ads against the campaign's brief
(ws-meta/work/WIN13-FEASIBILITY.md; brief ws-meta/briefs/LIVE-BRIEF-CHECK.md).

brief_checks is the library: prepare_calls builds one call per ad,
parse_reply and check_call check a reply in code, bundle_row and
insert_row store it in model_brief_checks with the same rules as the
import, and read_for_draft shows it in the draft's model_checks. This
module adds the press: its gates, its spend and its run row
(brief_check_live_schema.brief_check_runs).

start (the route answers 202 with the run id; GET reads the run):
  1. a member's browser session (the route has checked that the member may
     change the campaign: customer_save_campaign's role and maker rules);
     the brief version the page showed {brief_id, version, digest};
  2. availability: the alerts ledger, the key and the workspace's model
     addendum (meaning._state, Ask's test), at Ask's level: an
     excerpt-level workspace is refused, since the +-15 s around an ad is
     transcript outside it (so is a transcript permission row that denies,
     checked again at each press); both tables present;
  3. the shared running slots (meaning: 2 in all, 1 per member, alerts
     included), then one write transaction: the placement is the
     campaign's, the campaign's pinned brief is the page's (else
     BRIEF_CHANGED; a typed brief is saved as a new version the campaign
     points to before it can be sent), the member's $0.45 a Los Angeles
     day (settled plus open spend, plus one call's bound), and the run row;
     a working run on the placement refuses (the table's lock);
  4. the run: the placement's ads, at most MAX_ADS_PER_PRESS (the rest are
     counted over the cap); one brief_check call per ad through
     meaning._Run.call (HALT, run time, fence, the member limit, its own
     alerts-ledger reservation settled with its usage or marked unknown,
     one 429/5xx retry per run). A malformed reply gets one more call; a
     second, or a failed call, stores a failed row for that ad ("Muse
     Spark didn't check this ad."). HALT, a limit, a lost session, a
     brief edit or the run time stop the run; rows stored stay.

The request carries the pinned brief version only (brief_checks.brief_text:
at most 4 KB, one line, no passage-header look-alike). No log line holds a
brief, a passage or a reply: type, code and location only.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import access as _access
from . import ask as _ask
from . import brief_check_live_schema as _schema
from . import brief_checks as _bc
from . import connections as _conn
from . import db as _db
from . import meaning as _meaning
from . import meta_client as _meta
from . import oplog as _oplog
from . import rights_admission as _rights

log = logging.getLogger(__name__)

KIND = "brief_check"
MAX_ADS_PER_PRESS = 3
JUDGE_DAY_USD = 0.45
RUN_ID = re.compile(r"bcr_[0-9a-f]{24}")
# The code that checked a live row (a bundle row's code_sha).
CODE_SHA = "live-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
# Codes that end the run; any other call failure fails only its ad.
_RUN_STOPS = frozenset((_meta.HALT, "ALERT_LIMIT_DAY", "ALERT_PAUSED", "LIMIT_DAY", "FORBIDDEN",
                        "BOUND", "BRIEF_CHANGED"))
_CODES = {"ALERT_LIMIT_DAY": "LIMIT_DAY", "ALERT_PAUSED": "PAUSED"}


# ---------------------------------------------------------------- errors

class BriefCheckError(Exception):
    """A refusal; `code` is a short code, never text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BriefCheckUnavailable(BriefCheckError):
    """The ledger, the key, the addendum, an excerpt-level workspace or a
    missing table: nothing stored."""


class BriefCheckBusy(BriefCheckError):
    """GLOBAL or MEMBER (the shared slots) or PLACEMENT (a run is going)."""


class BriefCheckLimit(BriefCheckError):
    """DAY: the member's $0.45 today."""


class BriefChanged(BriefCheckError):
    def __init__(self, code: str = "BRIEF_CHANGED"):
        super().__init__(code)


class BriefCheckNotFound(BriefCheckError):
    def __init__(self, code: str = "NOT_FOUND"):
        super().__init__(code)


def _log_failure(exc: BaseException, what: str) -> None:
    code = getattr(exc, "code", None)
    log.error("brief check %s failed: type=%s code=%s where=%s", what, type(exc).__name__,
              code if isinstance(code, str) else None, _oplog.error_location(exc))


# ---------------------------------------------------------------- input

def brief_ref(value) -> dict:
    """The brief version the page showed: {brief_id, version, digest}."""
    if not isinstance(value, dict) or set(value) != {"brief_id", "version", "digest"}:
        raise ValueError("brief must hold brief_id, version and digest")
    if not isinstance(value["brief_id"], str) or not value["brief_id"] \
            or not isinstance(value["digest"], str) or not value["digest"] \
            or not isinstance(value["version"], int) or isinstance(value["version"], bool):
        raise ValueError("brief must hold brief_id, version and digest")
    return {"brief_id": value["brief_id"], "version": value["version"],
            "digest": value["digest"]}


def _pin(brief) -> tuple:
    return (brief["brief_id"], int(brief["version"]), brief["digest"]) if brief else None


def requirements_sha256(con, placement_id: str):
    """The placement's requirements as a call sends them (message, offer,
    code), hashed; None for no placement."""
    row = con.execute("SELECT requirements FROM expected_placements WHERE id=?",
                      (placement_id,)).fetchone()
    if row is None:
        return None
    try:
        reqs = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        reqs = {}
    reqs = reqs if isinstance(reqs, dict) else {}
    sent = {k: reqs.get(k) for k in ("message", "offer", "code") if reqs.get(k) not in (None, "")}
    return hashlib.sha256(json.dumps(sent, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- availability

def _state(store, ctx, ledger, control=None):
    """(rights, code): code None when available (Ask's test, at Ask's level)."""
    rights, code = _meaning._state(store, ctx, ledger, control)
    if code is None and not rights.get("transcripts"):
        code = "EXCERPT_LEVEL"
    return rights, code


def _tables_present(ctx) -> bool:
    con = _db.open_existing(ctx.db_path, readonly=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        return row is not None and row[0] == ctx.db_identity and _schema.present(con) \
            and _bc.table_exists(con)
    finally:
        con.close()


def available(store, data_dir, control, ledger, ctx) -> bool:
    """The test start makes first (the caller checks the switches)."""
    try:
        return _ask._member(ctx) and _state(store, ctx, ledger, control)[1] is None \
            and _tables_present(ctx)
    except Exception as exc:  # noqa: BLE001 - unknown means unavailable
        _log_failure(exc, "available")
        return False


def require_editor(con, entry, ctx, campaign_id) -> None:
    """The member may change the campaign (customer_save_campaign's gate)."""
    from . import services as _services
    _services._require_customer_role(con, entry, ctx, "organize", "customer_save_campaign")
    _services._require_maker(con, entry, ctx, "campaign", campaign_id)


# ---------------------------------------------------------------- start

def _judge_used(con, principal: str, now: float) -> float:
    start, end = _ask._la_day(now)
    return float(con.execute(
        f"SELECT COALESCE(SUM(usd_spent + usd_open), 0) FROM {_schema.TABLE}"
        " WHERE member=? AND created_at >= ? AND created_at < ?",
        (principal, start, end)).fetchone()[0])


def start(store, data_dir, control, ctx, campaign_id, placement_id, *, brief, client, ledger,
          clock=time.time) -> str:
    if not _ask._member(ctx):
        raise _access.AuthorizationError("the brief check needs a member's browser session", 403)
    if not isinstance(campaign_id, int) or isinstance(campaign_id, bool) \
            or not isinstance(placement_id, str) or not placement_id:
        raise ValueError("campaign and placement are required")
    shown = brief_ref(brief)
    rights, code = _state(store, ctx, ledger, control)
    if code:
        raise BriefCheckUnavailable(code)
    run_id = "bcr_" + secrets.token_hex(12)
    try:
        _meaning._take_slot(run_id, ctx.principal_id)
    except _meaning.MeaningBusy as exc:
        raise BriefCheckBusy(exc.code) from None
    started = False
    try:
        _store_start(store, data_dir, ctx, run_id, campaign_id, placement_id, shown, rights,
                     clock)
        job = functools.partial(_guarded, store, data_dir, ctx, run_id, rights, client=client,
                                ledger=ledger, clock=clock, campaign_id=campaign_id,
                                placement_id=placement_id, brief=shown)
        try:
            _meaning._spawn(run_id, job)
        except BaseException:
            _end(store, data_dir, ctx, run_id, "failed", "INTERNAL", clock)
            raise
        started = True
    finally:
        if not started:
            _meaning._release(run_id)
    return run_id


def _store_start(store, data_dir, ctx, run_id, campaign_id, placement_id, shown, rights,
                 clock) -> None:
    now = clock()
    con = _meaning._open_rw(store, data_dir, ctx)
    try:
        with _db.transaction(con, write=True):
            if not (_schema.present(con) and _bc.table_exists(con)):
                raise BriefCheckUnavailable("TABLE_MISSING")
            # A transcript row that denies, applied since start-up: the
            # workspace reads at excerpt level now (Ask's level refuses).
            if _rights._transcript_denied(con):
                raise BriefCheckUnavailable("EXCERPT_LEVEL")
            if con.execute("SELECT 1 FROM expected_placements WHERE id=? AND campaign_id=?",
                           (placement_id, campaign_id)).fetchone() is None:
                raise BriefCheckNotFound()
            if _pin(_bc.campaign_brief(con, campaign_id)) != _pin(shown):
                raise BriefChanged()
            if _judge_used(con, ctx.principal_id, now) + _meaning.bound_usd(KIND) \
                    > JUDGE_DAY_USD:
                raise BriefCheckLimit("DAY")
            try:
                con.execute(
                    f"INSERT INTO {_schema.TABLE} (id, member, campaign_id, placement_id,"
                    " brief_id, brief_version, brief_digest, requirements_sha256, state,"
                    " rights_level, record_revision, addendum_revision, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'working', ?, ?, ?, ?)",
                    (run_id, ctx.principal_id, campaign_id, placement_id, shown["brief_id"],
                     shown["version"], shown["digest"],
                     requirements_sha256(con, placement_id), rights["rights_level"],
                     rights["record_revision"], rights["addendum_revision"], _ask._iso(now)))
            except sqlite3.IntegrityError:
                raise BriefCheckBusy("PLACEMENT") from None
    finally:
        con.close()


def _end(store, data_dir, ctx, run_id, state, code, clock) -> None:
    """A run that could not run: ended, its open spend counted."""
    try:
        con = _meaning._open_rw(store, data_dir, ctx)
        try:
            with _db.transaction(con, write=True):
                con.execute(f"UPDATE {_schema.TABLE} SET state=?, error_code=?,"
                            " usd_spent=usd_spent + usd_open, usd_open=0, finished_at=?"
                            " WHERE id=? AND state='working'",
                            (state, code, _ask._iso(clock()), run_id))
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 - a restart marks it failed
        _log_failure(exc, "end")


def _guarded(store, data_dir, ctx, run_id, rights, **kw) -> None:
    try:
        run = _LiveRun(store, data_dir, ctx, run_id, rights, **kw)
        state, code = "failed", "INTERNAL"
        try:
            run.go()
            state, code = "done", None
        except _meaning._Stop as stop:
            code = _CODES.get(stop.code, stop.code)
        except _meaning._Gone:
            code = "GONE"
        except Exception as exc:  # noqa: BLE001 - logged without text
            _log_failure(exc, "run")
        finally:
            run.close()
            run.finish(state, code)
        if state != "done":
            log.info("brief check %s ended: state=%s code=%s", run_id, state, code)
    except BaseException as exc:  # noqa: BLE001
        _log_failure(exc, "thread")
    finally:
        _meaning._release(run_id)
        _meaning._threads.pop(run_id, None)


# ---------------------------------------------------------------- the run

class _LiveRun(_meaning._Run):
    """meaning._Run's call (HALT, run time, fence, reservation, timeout,
    retry) on brief_check_runs and this member's $0.45."""

    def __init__(self, store, data_dir, ctx, run_id, rights, *, client, ledger, clock,
                 campaign_id, placement_id, brief):
        super().__init__(store, data_dir, ctx, run_id, KIND, {}, rights, client=client,
                         ledger=ledger, clock=clock)
        self.campaign_id, self.placement_id, self.brief = campaign_id, placement_id, brief
        self.ads = self.sent = self.ok = self.bad = self.over = 0
        self.ad_spent = 0.0
        self.ad_call = None

    def fence(self) -> None:
        from . import services as _services
        try:
            _services.fenced_authority_check(self.store, self.data_dir, self.ctx,
                                             operation="customer_brief_check")
        except (_access.AuthorizationError, _conn.GrantError):
            raise _meaning._Stop("FORBIDDEN") from None

    def _member_open(self, delta_open: float, delta_spent: float = 0.0, *, check: bool) -> None:
        con = _meaning._open_rw(self.store, self.data_dir, self.ctx)
        try:
            with _db.transaction(con, write=True):
                if check and _judge_used(con, self.ctx.principal_id, self.clock()) \
                        + delta_open > JUDGE_DAY_USD:
                    raise _meaning._Stop("LIMIT_DAY")
                done = con.execute(f"UPDATE {_schema.TABLE} SET usd_open=MAX(0, usd_open + ?),"
                                   " usd_spent=usd_spent + ? WHERE id=?",
                                   (delta_open, delta_spent, self.run_id))
                if done.rowcount != 1:
                    raise _meaning._Gone()
        finally:
            con.close()
        self.ad_spent += delta_spent

    def _close(self, res, bound: float, usage) -> None:
        super()._close(res, bound, usage)
        self.ad_call = res.id

    def go(self) -> None:
        self.ws, _entry = _conn.open_registered_db(self.store, self.ctx.workspace_id,
                                                   self.data_dir, readonly=True)
        self.ws.row_factory = sqlite3.Row
        self.check_brief()
        calls = [c for c in _bc.prepare_calls(self.ws, self.campaign_id, model=_meta.MODEL)
                 if c["ad"]["placement_id"] == self.placement_id]
        self.ads = len(calls)
        self.over = max(0, len(calls) - MAX_ADS_PER_PRESS)
        for call in calls[:MAX_ADS_PER_PRESS]:
            self.check_brief()
            self.sent += 1
            self.check_ad(call)

    def check_brief(self) -> None:
        brief = _bc.campaign_brief(self.ws, self.campaign_id)
        if _pin(brief) != _pin(self.brief):
            raise _meaning._Stop("BRIEF_CHANGED")

    def check_ad(self, call) -> None:
        self.ad_spent, self.ad_call = 0.0, None
        unknown_before = self.unknown
        state, fields, removed = "failed", [], 0
        try:
            for _attempt in range(2):
                reply = self.call(KIND, call["body"])
                items = _bc.parse_reply(reply.message)
                if items is not None:
                    fields, removed = _bc.check_call(call, items)
                    state = "checked"
                    break
        except _meaning._Stop as stop:
            if stop.code in _RUN_STOPS or self.remaining() <= 0:
                if self.ad_call is not None:
                    self.store_row(call, "failed", [], 0, unknown_before)
                raise
        self.store_row(call, state, fields, removed, unknown_before)

    def store_row(self, call, state, fields, removed, unknown_before) -> None:
        now = _ask._iso(self.clock())
        usd = None if self.unknown and not unknown_before else round(self.ad_spent, 9)
        row = _bc.bundle_row(call, fields, removed, state=state, run_id=self.run_id,
                             model=self.model or _meta.MODEL, usd=usd,
                             ledger_call_id=self.ad_call, code_sha=CODE_SHA, created_at=now)
        con = _meaning._open_rw(self.store, self.data_dir, self.ctx)
        try:
            with _db.transaction(con, write=True):
                done = con.execute(
                    f"UPDATE {_schema.TABLE} SET checked=checked + ?, failed=failed + ?"
                    " WHERE id=? AND state='working'",
                    (int(state == "checked"), int(state == "failed"), self.run_id))
                if done.rowcount != 1:
                    raise _meaning._Gone()
                _bc.insert_row(con, row, req=self.run_id, imported_at=now)
        finally:
            con.close()

    def finish(self, state: str, code) -> None:
        now = _ask._iso(self.clock())
        usd = None if self.unknown else round(self.settled_usd, 9)
        try:
            con = _meaning._open_rw(self.store, self.data_dir, self.ctx)
            try:
                with _db.transaction(con, write=True):
                    con.execute(
                        f"UPDATE {_schema.TABLE} SET state=?, error_code=?, ads=?, sent=?,"
                        " over_cap=?, calls=?, model=?, usd_settled=?,"
                        " usd_spent=usd_spent + usd_open, usd_open=0, finished_at=?"
                        " WHERE id=? AND state='working'",
                        (state, code, self.ads, self.sent, self.over, self.calls,
                         self.model or (_meta.MODEL if self.calls else ""), usd, now,
                         self.run_id))
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - a restart marks it failed
            _log_failure(exc, "finish")


# ---------------------------------------------------------------- reads

_VIEW_KEYS = ("state", "error_code", "campaign_id", "placement_id", "ads", "sent", "checked",
              "failed", "over_cap")


def read_run(store, ctx, run_id) -> dict:
    """A run's view for any member: its state and counts (codes only), its
    spend, and whether the brief or the placement's requirements changed
    since it was sent (the page then offers the check again)."""
    if not _ask._member(ctx) or not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise BriefCheckNotFound()
    con = _db.open_existing(ctx.db_path, readonly=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
        if row is None or row[0] != ctx.db_identity or not _schema.present(con):
            raise BriefCheckNotFound()
        run = con.execute(f"SELECT * FROM {_schema.TABLE} WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise BriefCheckNotFound()
        view = {"run_id": run["id"], **{k: run[k] for k in _VIEW_KEYS}}
        view["cost_usd"] = None if run["state"] == "working" else round(run["usd_spent"], 6)
        view["brief_changed"] = _pin(_bc.campaign_brief(con, run["campaign_id"])) != (
            run["brief_id"], run["brief_version"], run["brief_digest"])
        view["requirements_changed"] = requirements_sha256(con, run["placement_id"]) \
            != run["requirements_sha256"]
        return view
    finally:
        con.close()


def requirements_changed(con, model_checks) -> list:
    """The check ids among a draft's shown rows (model_checks) that a live
    run stored and whose placement's requirements changed since that run
    sent them (requirements_sha256), in id order; [] for none. An imported
    row has no run here and is never named. The page shows UX's stale line
    under each such box."""
    ids = sorted({row["check_id"] for rows in (model_checks or {}).values() for row in rows})
    if not ids or not _schema.present(con):
        return []
    marks = ", ".join("?" * len(ids))
    rows = con.execute(f"SELECT m.id, m.placement_id, r.requirements_sha256 FROM {_bc.TABLE} m"
                       f" JOIN {_schema.TABLE} r ON r.id = m.run_id WHERE m.id IN ({marks})",
                       ids).fetchall()
    now, out = {}, []
    for check_id, placement_id, sent in rows:
        if sent is None:
            continue
        if placement_id not in now:
            now[placement_id] = requirements_sha256(con, placement_id)
        if now[placement_id] != sent:
            out.append(int(check_id))
    return sorted(out)


# ---------------------------------------------------------------- restart

def on_startup(store) -> None:
    """A restart: every working run becomes failed (RESTART); its open
    spend stays counted for the member's day."""
    now = _ask._iso(time.time())
    try:
        workspaces = store.list_registered_workspaces()
    except Exception as exc:  # noqa: BLE001
        _log_failure(exc, "startup")
        return
    for workspace in workspaces:
        try:
            entry = _conn.resolve_workspace(store, workspace)
            con = _db.open_existing(entry["db_path"], readonly=False)
            try:
                if not _schema.present(con):
                    continue
                with _db.transaction(con, write=True):
                    con.execute(f"UPDATE {_schema.TABLE} SET state='failed', error_code='RESTART',"
                                " usd_spent=usd_spent + usd_open, usd_open=0, finished_at=?"
                                " WHERE state='working'", (now,))
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - one workspace never stops the rest
            _log_failure(exc, "startup")


__all__ = ["BriefChanged", "BriefCheckBusy", "BriefCheckError", "BriefCheckLimit",
           "BriefCheckNotFound", "BriefCheckUnavailable", "JUDGE_DAY_USD", "KIND",
           "MAX_ADS_PER_PRESS", "available", "brief_ref", "on_startup", "read_run",
           "require_editor", "requirements_changed", "start"]
