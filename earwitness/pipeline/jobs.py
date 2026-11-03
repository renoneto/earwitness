"""P01 resumable processing job ledger: runs, jobs, attempts, paid operations.

Implements full-product plan section 6 on top of the P00-frozen contracts
(`earwitness/app/access.py`, `contracts.py`, `services.py`) and the P00
schema v4 tables (`processing_runs`, `processing_jobs`, `job_attempts`,
`paid_operations`). This module adds no P00 schema changes; its helper
tables (`p01_idempotency`, `p01_job_deps`, `p01_reconcile_log`,
`p01_job_profiles`) are created here with IF NOT EXISTS so historical
scripts and owner state are untouched.

Key rules enforced:
- Claims use a short bounded transaction and a unique fencing token
  (default 120s lease, 20s heartbeat). A worker that loses its lease
  cannot publish. No transaction stays open during network/inference work.
- Run reduction uses only the frozen requested graph. An empty candidate
  set is a successful empty downstream output, not a stranded dependency.
  Jobs may declare optional (informational) parents whose failure does not
  block them -- e.g. failed screening never blocks fast-transcript
  publication.
- Paid reservation is recorded before any paid send. An ambiguous dispatch
  (admitted but without a definitive recorded outcome) enters
  `reconciliation_required`, never ordinary retry; lease expiry cannot
  release or replace the original reservation.
- Cancellation fences publication; admitted remote exposure stays
  `reconciliation_required` with its own financial state.
- Reconciliation is revision-checked, idempotent, owner/operator only, and
  requires operation-linked evidence -- never elapsed time or a guess.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from earwitness.app import db as _db

DEFAULT_LEASE_S = 120.0
DEFAULT_HEARTBEAT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 3  # lease expiries before a job is dead-lettered
# Error marker expire_leases writes on attempts it expires; only these count
# as crashes (legacy 'expired' rows from the old --run release do not).
LEASE_EXPIRED = "lease_expired"

RUN_STATES = frozenset({
    "queued", "running", "blocked", "succeeded", "partial",
    "failed", "cancel_requested", "cancelled",
})
JOB_STATES = frozenset({
    "blocked", "queued", "running", "retry_wait", "succeeded",
    "failed", "cancelled", "reconciliation_required",
})
TERMINAL_JOBS = frozenset({"succeeded", "failed", "cancelled"})

RECONCILIATION_DISPOSITIONS = frozenset({
    "confirmed_success", "confirmed_nonacceptance", "still_unknown",
})
RECONCILING_ACTORS = frozenset({"owner", "operator"})


class JobError(Exception):
    """Base error for job-ledger violations."""


class LeaseExpired(JobError):
    """The claiming worker lost its fencing lease; publication refused."""


class FencingMismatch(JobError):
    """The presented fencing token is not the live claim token."""


class CancelledError(JobError):
    """Publication refused because cancellation won the race."""


class ConflictError(JobError):
    """Idempotent replay with a divergent body, or a stale revision."""
    status = 409


class BlockedError(JobError):
    """The requested transition is not allowed from the current state."""


class AuthorizationError(JobError):
    """The actor may not perform this operation (e.g. reconcile)."""


def utcnow() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _now_ts(now) -> float:
    return float(now) if now is not None else time.time()


def _parse_ts(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def stage_key(workspace_id: str, stage: str, stage_version: str,
              input_digest: str, backend: str = "", model: str = "",
              weights: str = "", config: str = "",
              output_schema: str = "") -> str:
    """Digest identifying reusable stage work (plan section 6).

    Covers workspace, stage/version, immutable input digests,
    backend/model/weights/config and output schema. A fetch job's
    input digest must include acquisition intent/time identity; URL
    alone is never a permanent cache key.
    """
    body = json.dumps({
        "workspace": workspace_id, "stage": stage,
        "stage_version": stage_version, "inputs": input_digest,
        "backend": backend, "model": model, "weights": weights,
        "config": config, "output_schema": output_schema,
    }, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def ensure_tables(con: sqlite3.Connection) -> None:
    """Create P01 helper tables only; never alters P00-owned table columns.

    Idempotent: init_schema and `app_workspace.py upgrade` (including on
    already-current workspaces) run it to add later helper tables/indexes.
    """
    _create_tables(con)
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    _db.ensure_view_state_guards(con)


def _create_tables(con: sqlite3.Connection) -> None:
    # expire_leases looks up running jobs every worker pass.
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON processing_jobs (state)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS p01_idempotency ("
        " idem_key TEXT PRIMARY KEY,"
        " workspace_id TEXT NOT NULL DEFAULT 'local',"
        " run_id TEXT NOT NULL,"
        " request_digest TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS p01_job_deps ("
        " job_id TEXT NOT NULL,"
        " parent_id TEXT NOT NULL,"
        " kind TEXT NOT NULL DEFAULT 'required',"
        " PRIMARY KEY (job_id, parent_id))")
    con.execute(
        "CREATE TABLE IF NOT EXISTS p01_reconcile_log ("
        " op_id TEXT NOT NULL,"
        " idem_key TEXT NOT NULL,"
        " disposition TEXT NOT NULL,"
        " actor TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (op_id, idem_key))")
    # Each job's frozen backend profile, denormalized from the run's
    # selection JSON so profile-filtered claims stay an indexed lookup.
    con.execute(
        "CREATE TABLE IF NOT EXISTS p01_job_profiles ("
        " job_id TEXT PRIMARY KEY,"
        " profile TEXT NOT NULL DEFAULT '')")
    # Backfill jobs submitted before the table existed: one pass per run
    # over its selection, then '' for jobs absent from any selection.
    con.execute(
        "INSERT OR IGNORE INTO p01_job_profiles (job_id, profile)"
        " SELECT json_extract(s.value, '$.job_id'),"
        " COALESCE(json_extract(s.value, '$.backend_profile'), '')"
        " FROM processing_runs r, json_each(CASE WHEN json_valid(r.selection)"
        " THEN r.selection ELSE '{}' END, '$.stages') s"
        " WHERE r.id IN (SELECT j.run_id FROM processing_jobs j WHERE NOT EXISTS"
        " (SELECT 1 FROM p01_job_profiles p WHERE p.job_id = j.id))"
        " AND EXISTS (SELECT 1 FROM processing_jobs j WHERE j.run_id = r.id"
        " AND j.id = json_extract(s.value, '$.job_id'))")
    con.execute(
        "INSERT OR IGNORE INTO p01_job_profiles (job_id, profile)"
        " SELECT j.id, '' FROM processing_jobs j WHERE NOT EXISTS"
        " (SELECT 1 FROM p01_job_profiles p WHERE p.job_id = j.id)")


def _require_tables(con):
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"p01_idempotency", "p01_job_deps", "p01_reconcile_log",
            "p01_job_profiles"} <= tables:
        raise JobError("job ledger requires explicit workspace setup")


def _get(con, run_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM processing_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


def _job(con, job_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM processing_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(job_id)
    return row


def _selection(con, run_id: str) -> dict:
    try:
        return json.loads(_get(con, run_id)["selection"] or "{}")
    except (ValueError, KeyError):
        return {}


def _bump_run(con, run_id: str, now_s: str) -> int:
    con.execute("UPDATE processing_runs SET revision = revision + 1, updated_at=?"
                " WHERE id=?", (now_s, run_id))
    return int(_get(con, run_id)["revision"])


def _run_jobs(con, run_id: str):
    return con.execute("SELECT * FROM processing_jobs WHERE run_id=?", (run_id,)).fetchall()


def _parents(con, job_id: str):
    return con.execute("SELECT parent_id, kind FROM p01_job_deps WHERE job_id=?",
                       (job_id,)).fetchall()


def _required_parents_succeeded(con, job_id: str, rows: dict | None = None) -> bool:
    """True when every required parent is succeeded (fail closed).

    Optional (informational) parents never gate. `rows`, when given, maps
    job ids to rows/dicts with a `state` key for same-transaction reads;
    anything unknown counts as not succeeded.
    """
    for dep in _parents(con, job_id):
        if dep["kind"] == "optional":
            continue
        pid = dep["parent_id"]
        state = None
        if rows is not None and pid in rows:
            state = rows[pid]["state"]
        else:
            try:
                state = _job(con, pid)["state"]
            except KeyError:
                state = None
        if state != "succeeded":
            return False
    return True


def _financial_status(con, run_id: str) -> str:
    jobs = _run_jobs(con, run_id)
    if any(j["state"] == "reconciliation_required" for j in jobs):
        return "reconciliation_required"
    ops = con.execute("SELECT disposition FROM paid_operations WHERE job_id IN "
                      "(SELECT id FROM processing_jobs WHERE run_id=?)", (run_id,)).fetchall()
    if any(o["disposition"] == "unknown" for o in ops):
        return "reconciliation_required"
    if ops:
        return "reserved"
    return "clear"


def reduce_run(con: sqlite3.Connection, run_id: str, now=None) -> dict:
    """Recompute job readiness and run status from the frozen graph.

    Queues children whose required parents all succeeded (optional-parent
    failures are ignored). Closes dependents of exhausted/cancelled parents.
    Derives the run state per the section-6 table. Commits atomically.
    """
    now_s = utcnow()
    with _db.transaction(con, write=True):
        run = _get(con, run_id)
        if run["status"] in ("cancel_requested", "cancelled"):
            _settle_cancel(con, run_id, now_s)
            return run_summary(con, run_id)
        jobs = {j["id"]: dict(j) for j in _run_jobs(con, run_id)}
        changed = True
        while changed:
            changed = False
            for jid, job in jobs.items():
                if job["state"] not in ("blocked", "queued"):
                    continue
                parents = _parents(con, jid)
                required = [p for p in parents if p["kind"] != "optional"]
                optional = [p for p in parents if p["kind"] == "optional"]
                states = {pid: jobs[pid]["state"] for pid, _k in
                          [(p["parent_id"], p["kind"]) for p in parents]
                          if pid in jobs}
                if any(states.get(p["parent_id"]) in ("cancelled",)
                       for p in required):
                    _set_job(con, jid, "cancelled", "DEPENDENCY_CANCELLED",
                             f"required parent cancelled", now_s)
                    job["state"] = "cancelled"
                    changed = True
                elif any(states.get(p["parent_id"]) == "failed" for p in required):
                    failed = [p["parent_id"] for p in required
                              if states.get(p["parent_id"]) == "failed"]
                    _set_job(con, jid, "failed", "DEPENDENCY_FAILED",
                             f"required parent failed: {','.join(failed)}", now_s)
                    job["state"] = "failed"
                    changed = True
                elif any(states.get(p["parent_id"]) == "reconciliation_required"
                         for p in parents):
                    # Cannot bypass reconciliation: hold as blocked, never queue.
                    if job["state"] != "blocked":
                        _set_job(con, jid, "blocked", "AWAITING_RECONCILIATION",
                                 "parent requires reconciliation", now_s)
                        job["state"] = "blocked"
                        changed = True
                elif all(states.get(p["parent_id"]) == "succeeded"
                         for p in required) and job["state"] == "blocked":
                    _set_job(con, jid, "queued", None, "", now_s,
                             clear_reason=True)
                    job["state"] = "queued"
                    changed = True
                elif job["state"] == "queued" and any(
                        states.get(p["parent_id"]) == "blocked" for p in required):
                    # Illicitly queued work (e.g. a parent-blind resume) can
                    # never run before its parents: hold it as blocked.
                    # Queued/running parents are in flight, not stuck, so a
                    # legitimate retry chain stays queued until they succeed.
                    _set_job(con, jid, "blocked", "AWAITING_PARENT",
                             "required parent blocked", now_s)
                    job["state"] = "blocked"
                    changed = True
                # Optional-parent failures never block; nothing to do.
                _ = optional
        _derive_run_status(con, run_id, now_s)
        con.execute("UPDATE processing_runs SET financial_status=?, updated_at=?"
                    " WHERE id=?", (_financial_status(con, run_id), now_s, run_id))
    return run_summary(con, run_id)


def _set_job(con, job_id: str, state: str, failure_class, error: str,
             now_s: str, clear_reason: bool = False) -> None:
    if clear_reason:
        con.execute("UPDATE processing_jobs SET state=?, failure_class=NULL,"
                    " updated_at=?, revision = revision + 1 WHERE id=?",
                    (state, now_s, job_id))
    else:
        con.execute("UPDATE processing_jobs SET state=?, failure_class=?,"
                    " updated_at=?, revision = revision + 1 WHERE id=?",
                    (state, failure_class, now_s, job_id))
    _ = error  # error detail lives on the owning attempt row, not the job row.


def _derive_run_status(con, run_id: str, now_s: str) -> None:
    # Terminal run states are sticky: cancellation is never un-decided by
    # later lease churn. Settle (cancel_requested drains to cancelled once
    # no running claim remains) instead of re-deriving from job states.
    if _get(con, run_id)["status"] in ("cancel_requested", "cancelled"):
        _settle_cancel(con, run_id, now_s)
        return
    jobs = _run_jobs(con, run_id)
    if not jobs:
        con.execute("UPDATE processing_runs SET status='blocked', updated_at=?"
                    " WHERE id=?", (now_s, run_id))
        return
    states = [j["state"] for j in jobs]
    live = [j for j in jobs if j["state"] in ("running", "retry_wait")]
    queued = [j for j in jobs if j["state"] == "queued"]
    blocked = [j for j in jobs if j["state"] in ("blocked", "reconciliation_required")]
    if live or queued:
        status = "running" if any(j["state"] == "running" for j in jobs) else "queued"
        # running once work has begun and runnable/retry-wait work remains.
        if live:
            status = "running"
        con.execute("UPDATE processing_runs SET status=?, updated_at=? WHERE id=?",
                    (status, now_s, run_id))
        return
    if blocked and not queued and not live:
        con.execute("UPDATE processing_runs SET status='blocked', updated_at=?"
                    " WHERE id=?", (now_s, run_id))
        return
    # No runnable or blocked work remains.
    if all(s == "succeeded" for s in states):
        status = "succeeded"
    elif any(s == "succeeded" for s in states):
        status = "partial"
    else:
        status = "failed"
    con.execute("UPDATE processing_runs SET status=?, updated_at=? WHERE id=?",
                (status, now_s, run_id))


def _run_cancelled(con, run_id: str) -> bool:
    """True when the run sits in a terminal cancellation state."""
    try:
        return _get(con, run_id)["status"] in ("cancel_requested", "cancelled")
    except KeyError:
        return False


def _settle_cancel(con, run_id: str, now_s: str) -> None:
    running = con.execute("SELECT COUNT(*) FROM processing_jobs WHERE run_id=?"
                          " AND state='running'", (run_id,)).fetchone()[0]
    if running:
        con.execute("UPDATE processing_runs SET status='cancel_requested',"
                    " financial_status=?, updated_at=? WHERE id=?",
                    (_financial_status(con, run_id), now_s, run_id))
    else:
        con.execute("UPDATE processing_runs SET status='cancelled',"
                    " financial_status=?, updated_at=? WHERE id=?",
                    (_financial_status(con, run_id), now_s, run_id))


def submit_run(con: sqlite3.Connection, *, workspace_id: str = "local",
               stages: list, config_digest: str = "", mode: str = "new_episodes",
               initiator: str = "", idempotency_key: str,
               policy_id: str = "", now=None) -> dict:
    """Create a run over a frozen requested graph, or replay a duplicate.

    `stages` is a list of dicts: {stage, input_digest, parents=[job ids or
    stage indexes], optional_parents=[...], backend_profile, cost_estimate}.
    Parent references may use the stage's own `job_id` key or its list
    index (`s0`, `s1`, ...). Concurrent same-key submissions yield one
    logical operation; same key with a divergent body raises ConflictError.
    """
    if not idempotency_key:
        raise JobError("creation requires an idempotency key")
    _require_tables(con)
    staged = _normalize_stages(stages)
    digest = hashlib.sha256(json.dumps(
        {"stages": staged, "config": config_digest, "mode": mode},
        sort_keys=True).encode()).hexdigest()
    with _db.transaction(con, write=True):
        prior = con.execute("SELECT run_id, request_digest FROM p01_idempotency"
                            " WHERE idem_key=?", (idempotency_key,)).fetchone()
        if prior is not None:
            if prior["request_digest"] != digest:
                raise ConflictError("idempotency key replays a different request body")
            return run_summary(con, prior["run_id"])
        run_id = "run_" + uuid.uuid4().hex[:12]
        # Default ids (s0, s1, ...) are unique only within one request, but
        # job ids are the global primary key: scope them by run. The request
        # digest above stays over the unscoped ids so replays still match.
        scoped = {f"s{i}": f"{run_id}.s{i}"
                  for i, spec in enumerate(stages) if not spec.get("job_id")}
        staged = [dict(spec, job_id=scoped.get(spec["job_id"], spec["job_id"]),
                       parents=[scoped.get(p, p) for p in spec["parents"]],
                       optional_parents=[scoped.get(p, p)
                                         for p in spec["optional_parents"]])
                  for spec in staged]
        now_s = utcnow()
        selection = json.dumps({"frozen_graph": True, "mode": mode,
                                "config_digest": config_digest,
                                "policy_id": policy_id,
                                "stages": staged})
        con.execute("INSERT INTO processing_runs (id, workspace_id, status,"
                    " financial_status, mode, selection, config_digest, initiator,"
                    " revision, created_at, updated_at)"
                    " VALUES (?, ?, 'queued', 'clear', ?, ?, ?, ?, 1, ?, ?)",
                    (run_id, workspace_id, mode, selection, config_digest,
                     initiator, now_s, now_s))
        # Reuse valid completed stage outputs across runs via stage key:
        # a stage whose key already succeeded links that output without
        # deleting the original run's lineage.
        for spec in staged:
            state = "blocked" if spec["parents"] or spec["optional_parents"] else "queued"
            reused = _find_reusable(con, workspace_id, spec, config_digest)
            if reused is not None:
                state = "succeeded"
            con.execute("INSERT INTO processing_jobs (id, workspace_id, run_id,"
                        " stage, state, input_digest, stage_key, revision, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        (spec["job_id"], workspace_id, run_id, spec["stage"],
                         state, spec["input_digest"],
                         _spec_stage_key(workspace_id, spec, config_digest), now_s))
            con.execute("INSERT OR REPLACE INTO p01_job_profiles (job_id, profile)"
                        " VALUES (?, ?)", (spec["job_id"], spec["backend_profile"]))
            for parent in spec["parents"]:
                con.execute("INSERT OR IGNORE INTO p01_job_deps (job_id, parent_id, kind)"
                            " VALUES (?, ?, 'required')", (spec["job_id"], parent))
            for parent in spec["optional_parents"]:
                con.execute("INSERT OR IGNORE INTO p01_job_deps (job_id, parent_id, kind)"
                            " VALUES (?, ?, 'optional')", (spec["job_id"], parent))
            if reused is not None:
                con.execute("INSERT INTO job_attempts (workspace_id, job_id, attempt_no,"
                            " state, reported_cost, error, created_at)"
                            " VALUES (?, ?, 0, 'succeeded', ?, ?, ?)",
                            (workspace_id, spec["job_id"], "{}",
                             f"reused:{reused}", now_s))
        con.execute("INSERT INTO p01_idempotency (idem_key, workspace_id, run_id,"
                    " request_digest, created_at) VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, workspace_id, run_id, digest, now_s))
        # Same transaction: a reused (succeeded) parent never strands children.
        return reduce_run(con, run_id, now=now)


def _normalize_stages(stages: list) -> list:
    if not stages:
        raise JobError("a run requires at least one requested stage")
    ids = []
    for i, spec in enumerate(stages):
        ids.append(spec.get("job_id") or f"s{i}")
    if len(set(ids)) != len(ids):
        raise JobError("stage job ids must be unique within a run")

    def resolve(ref):
        if ref in ids:
            return ref
        if isinstance(ref, int) and 0 <= ref < len(ids):
            return ids[ref]
        raise JobError(f"unknown parent reference: {ref!r}")

    out = []
    for i, spec in enumerate(stages):
        out.append({
            "job_id": ids[i],
            "stage": str(spec.get("stage", f"stage_{i}")),
            "stage_version": str(spec.get("stage_version", "")),
            "input_digest": str(spec.get("input_digest", "")),
            "parents": [resolve(p) for p in spec.get("parents", [])],
            "optional_parents": [resolve(p) for p in spec.get("optional_parents", [])],
            "backend_profile": str(spec.get("backend_profile", "")),
            "model": str(spec.get("model", "")),
            "weights": str(spec.get("weights", "")),
            "config": str(spec.get("config", "")),
            "output_schema": str(spec.get("output_schema", "")),
            "cost_estimate": float(spec.get("cost_estimate", 0.0) or 0.0),
        })
    # The requested graph must be acyclic.
    order = {jid: n for n, jid in enumerate(ids)}
    for spec in out:
        for parent in spec["parents"] + spec["optional_parents"]:
            if order[parent] >= order[spec["job_id"]]:
                raise JobError("requested stage graph must be acyclic")
    return out


def _spec_stage_key(workspace_id: str, spec: dict, config_digest: str = "") -> str:
    """Full stage key for a requested stage (plan section 6).

    Covers workspace, stage/version, input digest, backend/model/weights,
    config and output schema. A stage-level `config` overrides the run's
    `config_digest`; NULL/legacy stored rows match nothing.
    """
    return stage_key(
        workspace_id, spec.get("stage", ""), spec.get("stage_version", ""),
        spec.get("input_digest", ""), backend=spec.get("backend_profile", ""),
        model=spec.get("model", ""), weights=spec.get("weights", ""),
        config=spec.get("config", "") or config_digest,
        output_schema=spec.get("output_schema", ""))


def _job_stage_key(con, job) -> str:
    """Full stage key for a stored job row, from its frozen request spec."""
    run = _get(con, job["run_id"])
    spec = {}
    for candidate in _selection(con, job["run_id"]).get("stages", []):
        if candidate.get("job_id") == job["id"]:
            spec = candidate
            break
    base = {"stage": job["stage"], "stage_version": "",
            "input_digest": job["input_digest"], "backend_profile": "",
            "model": "", "weights": "", "config": "", "output_schema": ""}
    base.update(spec)
    return _spec_stage_key(job["workspace_id"], base, run["config_digest"])


def _find_reusable(con, workspace_id: str, spec: dict, config_digest: str = ""):
    """Find a completed stage with an equal FULL stage key in another run."""
    if not spec["input_digest"]:
        return None
    key = _spec_stage_key(workspace_id, spec, config_digest)
    row = con.execute("SELECT j.id FROM processing_jobs j"
                      " WHERE j.workspace_id=? AND j.stage=? AND j.state='succeeded'"
                      " AND j.stage_key=? LIMIT 1",
                      (workspace_id, spec["stage"], key)).fetchone()
    return row["id"] if row is not None else None


def run_summary(con: sqlite3.Connection, run_id: str) -> dict:
    run = _get(con, run_id)
    counts: dict = {}
    for job in _run_jobs(con, run_id):
        key = f"{job['stage']}:{job['state']}"
        counts[key] = counts.get(key, 0) + 1
    blocking = sorted({(j["failure_class"] or "UNKNOWN")
                       for j in _run_jobs(con, run_id)
                       if j["state"] in ("blocked", "reconciliation_required")})
    reserved = 0.0
    unknown = 0.0
    for op in con.execute("SELECT reservation, disposition FROM paid_operations"
                          " WHERE job_id IN (SELECT id FROM processing_jobs"
                          " WHERE run_id=?)", (run_id,)).fetchall():
        try:
            reservation = json.loads(op["reservation"] or "{}")
        except ValueError:
            reservation = {}
        if op["disposition"] == "unknown":
            unknown += float(reservation.get("worst_case_cost", 0.0) or 0.0)
        else:
            reserved += float(reservation.get("worst_case_cost", 0.0) or 0.0)
    return {
        "run_id": run["id"],
        "revision": int(run["revision"]),
        "status": run["status"],
        "financial_status": run["financial_status"],
        "mode": run["mode"],
        "stage_counts": counts,
        "blocking_reasons": blocking,
        "reserved_cost": reserved,
        "unknown_cost": unknown,
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
    }


def run_detail(con: sqlite3.Connection, run_id: str) -> dict:
    summary = run_summary(con, run_id)
    jobs = [dict(j) for j in _run_jobs(con, run_id)]
    attempts = [dict(a) for a in con.execute(
        "SELECT * FROM job_attempts WHERE job_id IN (SELECT id FROM"
        " processing_jobs WHERE run_id=?) ORDER BY job_id, attempt_no",
        (run_id,)).fetchall()]
    ops = [dict(o) for o in con.execute(
        "SELECT * FROM paid_operations WHERE job_id IN (SELECT id FROM"
        " processing_jobs WHERE run_id=?)", (run_id,)).fetchall()]
    for row in jobs + attempts + ops:
        row.pop("lease_token", None)
    return {"summary": summary, "jobs": jobs, "attempts": attempts,
            "paid_operations": ops, "selection": _selection(con, run_id)}


def _claim_sql(workspace_id: str, ts: float, run_id, profiles) -> tuple:
    """The claim query: oldest eligible job first, filtered entirely in SQL.

    One branch per claimable state, each walking idx_jobs_state in queue
    order and stopping at its first match; the older of the two wins.
    """
    # A cancelled run's leftovers are never claimable, even after
    # lease-expiry churn returns them to queued.
    where = (" FROM processing_jobs j"
             " JOIN processing_runs r ON r.id = j.run_id"
             " LEFT JOIN p01_job_profiles p ON p.job_id = j.id"
             " WHERE j.workspace_id=? AND j.state=?"
             " AND r.status NOT IN ('cancel_requested','cancelled')")
    params: list = [workspace_id]
    if run_id:
        where += " AND j.run_id=?"
        params.append(run_id)
    if profiles:
        marks = ",".join("?" * len(profiles))
        where += (" AND (j.stage IN ('fetch','transcribe','screen','refine','noop',"
                  f"'fail') OR j.stage IN ({marks}))"
                  f" AND COALESCE(p.profile, '') IN ('', {marks})")
        params += list(profiles) * 2
    branch = "SELECT * FROM (SELECT j.*, j.rowid AS queue_pos{} ORDER BY j.rowid LIMIT 1)"
    # retry_wait waits for its scheduled retry time (stored on the latest
    # attempt's heartbeat_at).
    retry = (where + " AND COALESCE(CAST((SELECT a.heartbeat_at FROM job_attempts a"
             " WHERE a.job_id=j.id ORDER BY a.attempt_no DESC LIMIT 1) AS REAL), 0) <= ?")
    sql = (f"SELECT * FROM ({branch.format(where)} UNION ALL {branch.format(retry)})"
           " ORDER BY queue_pos LIMIT 1")
    return sql, [params[0], "queued", *params[1:],
                 params[0], "retry_wait", *params[1:], ts]


def claim_next(con: sqlite3.Connection, *, workspace_id: str = "local",
               worker_id: str, profiles=(), run_id: str | None = None,
               lease_s: float = DEFAULT_LEASE_S, now=None) -> dict | None:
    """Claim one runnable job with a unique fencing token.

    Bounded immediate transaction; returns before any network/inference
    work begins. `profiles` restricts to jobs whose backend_profile is
    listed (empty means any local profile); `run_id` restricts to one
    run. Eligibility is decided in SQL (profiles via the denormalized
    p01_job_profiles row) so a match is found wherever it sits in the
    queue. Claims follow queue position; a crash-looping job is bounded
    by expire_leases' attempt limit rather than demoted.
    """
    ts = _now_ts(now)
    token = f"{worker_id}:{uuid.uuid4().hex[:12]}"
    sql, params = _claim_sql(workspace_id, ts, run_id, profiles)
    with _db.transaction(con, write=True):
        row = con.execute(sql, params).fetchone()
        if row is None:
            return None
        chosen = dict(row)
        attempt_no = con.execute("SELECT COALESCE(MAX(attempt_no), -1) + 1 FROM"
                                 " job_attempts WHERE job_id=?",
                                 (chosen["id"],)).fetchone()[0]
        con.execute("UPDATE processing_jobs SET state='running', lease_token=?,"
                    " updated_at=?, revision = revision + 1 WHERE id=?"
                    " AND state IN ('queued','retry_wait')",
                    (token, utcnow(), chosen["id"]))
        con.execute("INSERT INTO job_attempts (workspace_id, job_id, attempt_no,"
                    " state, heartbeat_at, created_at) VALUES (?, ?, ?, 'running', ?, ?)",
                    (workspace_id, chosen["id"], attempt_no, repr(ts), utcnow()))
        _derive_run_status(con, chosen["run_id"], utcnow())
    job = dict(_job(con, chosen["id"]))
    job["fencing_token"] = token
    job["lease_expires_at"] = ts + lease_s
    job["attempt_no"] = attempt_no
    return job


def heartbeat(con: sqlite3.Connection, job_id: str, token: str, now=None) -> bool:
    """Renew a live lease. Returns False when the token is stale/expired."""
    ts = _now_ts(now)
    with _db.transaction(con, write=True):
        job = _job(con, job_id)
        if job["state"] != "running" or job["lease_token"] != token:
            return False
        attempt = con.execute("SELECT * FROM job_attempts WHERE job_id=?"
                              " ORDER BY attempt_no DESC LIMIT 1",
                              (job_id,)).fetchone()
        issued = _parse_ts(attempt["heartbeat_at"]) if attempt is not None else ts
        lease_s = _lease_of(con, job)
        if ts - issued > lease_s:
            return False
        con.execute("UPDATE job_attempts SET heartbeat_at=? WHERE job_id=?"
                    " AND attempt_no=?", (repr(ts), job_id, attempt["attempt_no"]))
        return True


def _lease_of(con, job) -> float:
    return DEFAULT_LEASE_S


def _check_fencing(con, job_id: str, token: str, now=None) -> sqlite3.Row:
    """Validate the live fencing token inside the committing transaction."""
    ts = _now_ts(now)
    job = _job(con, job_id)
    if job["state"] != "running":
        raise BlockedError(f"job {job_id} is not running (state={job['state']})")
    if not token or job["lease_token"] != token:
        raise FencingMismatch(f"job {job_id}: fencing token is not live")
    attempt = con.execute("SELECT * FROM job_attempts WHERE job_id=?"
                          " ORDER BY attempt_no DESC LIMIT 1", (job_id,)).fetchone()
    issued = _parse_ts(attempt["heartbeat_at"]) if attempt is not None else 0.0
    if ts - issued > DEFAULT_LEASE_S:
        raise LeaseExpired(f"job {job_id}: lease expired; a stale worker cannot publish")
    run = _get(con, job["run_id"])
    if run["status"] in ("cancel_requested", "cancelled"):
        raise CancelledError(f"job {job_id}: cancellation won the publication race")
    if not _required_parents_succeeded(con, job_id):
        raise BlockedError(f"job {job_id} cannot publish:"
                           " required parents have not succeeded")
    return job


def publish_success(con: sqlite3.Connection, job_id: str, token: str,
                    output_digest: str = "", *, empty_ok: bool = False,
                    reported_cost: dict | None = None, now=None) -> dict:
    """Commit a stage result under fencing, cancellation and policy checks.

    Staged bytes alone are never success: this conditional commit is what
    makes result references readable. A legitimate empty candidate set
    commits with `empty_ok=True` so downstream stages queue normally.
    """
    if not output_digest and not empty_ok:
        raise JobError("successful publication requires an output digest"
                       " (or an explicit empty downstream result)")
    with _db.transaction(con, write=True):
        job = _check_fencing(con, job_id, token, now=now)
        now_s = utcnow()
        con.execute("UPDATE job_attempts SET state='succeeded', reported_cost=?,"
                    " error=? WHERE job_id=? AND state='running'",
                    (json.dumps(reported_cost or {}), output_digest[:200],
                     job_id))
        con.execute("UPDATE processing_jobs SET state='succeeded', lease_token=NULL,"
                    " input_digest=?, stage_key=?, updated_at=?,"
                    " revision = revision + 1 WHERE id=?",
                    (job["input_digest"], _job_stage_key(con, job), now_s, job_id))
        # Reduce in the same transaction: a crash after a separate commit
        # would leave dependents blocked behind a succeeded parent forever.
        return reduce_run(con, job["run_id"], now=now)


def publish_failure(con: sqlite3.Connection, job_id: str, token: str, *,
                    failure_class: str, error: str = "", retryable: bool = False,
                    retry_in_s: float = 60.0, now=None) -> dict:
    """Record a failed attempt. Retryable failures wait; terminal ones reduce."""
    ts = _now_ts(now)
    with _db.transaction(con, write=True):
        job = _check_fencing(con, job_id, token, now=now)
        now_s = utcnow()
        attempt = con.execute("SELECT MAX(attempt_no) FROM job_attempts WHERE job_id=?",
                              (job_id,)).fetchone()[0]
        con.execute("UPDATE job_attempts SET state='failed', error=? WHERE job_id=?"
                    " AND attempt_no=?", (error[:500], job_id, attempt))
        state = "retry_wait" if retryable else "failed"
        con.execute("UPDATE processing_jobs SET state=?, failure_class=?,"
                    " lease_token=NULL, updated_at=?, revision = revision + 1"
                    " WHERE id=?", (state, failure_class, now_s, job_id))
        if retryable:
            con.execute("UPDATE job_attempts SET heartbeat_at=? WHERE job_id=?"
                        " AND attempt_no=?", (repr(ts + retry_in_s), job_id, attempt))
        return reduce_run(con, job["run_id"], now=now)


def _crash_streak(con, job_id: str) -> int:
    """Consecutive most recent attempts that ended in lease expiry.

    Any other outcome (failure, release, dead-lettering, legacy unmarked
    expiry) ends the streak.
    """
    streak = 0
    for row in con.execute("SELECT state, error FROM job_attempts WHERE job_id=?"
                           " ORDER BY attempt_no DESC", (job_id,)).fetchall():
        if row["state"] != "expired" or row["error"] != LEASE_EXPIRED:
            break
        streak += 1
    return streak


def release_lease(con: sqlite3.Connection, job_id: str, token: str,
                  reason: str, now=None) -> bool:
    """Return a live claim to the queue with its true reason, not as a crash.

    Fenced on the claim's own token: a claim already reclaimed by
    expire_leases or another worker is left alone (returns False). The
    attempt ends `released` with `reason`, which never counts toward the
    poison-job limit. Paid-admitted work is never released here; lease
    expiry routes it to reconciliation.
    """
    with _db.transaction(con, write=True):
        job = _job(con, job_id)
        if job["state"] != "running" or not token or job["lease_token"] != token:
            return False
        if con.execute("SELECT COUNT(*) FROM paid_operations WHERE job_id=?"
                       " AND disposition='admitted'", (job_id,)).fetchone()[0]:
            return False
        now_s = utcnow()
        con.execute("UPDATE job_attempts SET state='released', error=? WHERE job_id=?"
                    " AND attempt_no=(SELECT MAX(attempt_no) FROM job_attempts"
                    " WHERE job_id=?)", (reason[:500], job_id, job_id))
        cancelled = _run_cancelled(con, job["run_id"])
        con.execute("UPDATE processing_jobs SET state=?, failure_class=?,"
                    " lease_token=NULL, updated_at=?, revision = revision + 1"
                    " WHERE id=?", ("cancelled" if cancelled else "queued",
                                    "CANCELLED" if cancelled else "LEASE_RELEASED",
                                    now_s, job_id))
        reduce_run(con, job["run_id"], now=now)
    return True


def expire_leases(con: sqlite3.Connection, *, now=None,
                  max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list:
    """Reclaim expired running claims without inventing completed work.

    Expired attempts are marked `expired`; their jobs return to `queued`
    for a fresh fenced claim. A job whose last `max_attempts` attempts all
    ended in lease expiry is a poison job (it kills or hangs every worker):
    it fails terminally with MAX_ATTEMPTS_EXCEEDED instead of looping.
    Paid-admitted jobs instead enter `reconciliation_required` -- lease
    expiry never releases exposure.
    """
    ts = _now_ts(now)
    expired = []
    with _db.transaction(con, write=True):
        for job in con.execute("SELECT * FROM processing_jobs WHERE state='running'").fetchall():
            attempt = con.execute("SELECT * FROM job_attempts WHERE job_id=?"
                                  " ORDER BY attempt_no DESC LIMIT 1",
                                  (job["id"],)).fetchone()
            issued = _parse_ts(attempt["heartbeat_at"]) if attempt is not None else 0.0
            if ts - issued <= DEFAULT_LEASE_S:
                continue
            now_s = utcnow()
            admitted = con.execute("SELECT COUNT(*) FROM paid_operations WHERE job_id=?"
                                   " AND disposition='admitted'", (job["id"],)).fetchone()[0]
            if attempt is not None:
                con.execute("UPDATE job_attempts SET state='expired', error=?"
                            " WHERE job_id=? AND attempt_no=?",
                            (LEASE_EXPIRED, job["id"], attempt["attempt_no"]))
            if admitted:
                con.execute("UPDATE processing_jobs SET state='reconciliation_required',"
                            " failure_class='AMBIGUOUS_DISPATCH', lease_token=NULL,"
                            " updated_at=? WHERE id=?", (now_s, job["id"]))
            elif _run_cancelled(con, job["run_id"]):
                # Lease expiry never resurrects a cancelled run: the expired
                # claim settles to cancelled with the rest.
                con.execute("UPDATE processing_jobs SET state='cancelled',"
                            " failure_class='CANCELLED', lease_token=NULL,"
                            " updated_at=? WHERE id=?", (now_s, job["id"]))
            elif _crash_streak(con, job["id"]) >= max_attempts:
                if attempt is not None:
                    # Ending the attempt `failed` closes the streak, so an
                    # operator retry starts a fresh count.
                    con.execute("UPDATE job_attempts SET state='failed', error=?"
                                " WHERE job_id=? AND attempt_no=?",
                                (f"max_attempts_exceeded: lease expired"
                                 f" {max_attempts} times", job["id"],
                                 attempt["attempt_no"]))
                con.execute("UPDATE processing_jobs SET state='failed',"
                            " failure_class='MAX_ATTEMPTS_EXCEEDED', lease_token=NULL,"
                            " updated_at=? WHERE id=?", (now_s, job["id"]))
            else:
                con.execute("UPDATE processing_jobs SET state='queued',"
                            " failure_class='LEASE_EXPIRED', lease_token=NULL,"
                            " updated_at=? WHERE id=?", (now_s, job["id"]))
            expired.append(job["id"])
        # Same transaction: dependents of a dead-lettered job never strand.
        for run_id in {_job(con, job_id)["run_id"] for job_id in expired}:
            reduce_run(con, run_id, now=now)
    return expired


def request_retry(con: sqlite3.Connection, run_id: str, failed_job_ids: list,
                  idempotency_key: str, *, expected_revision: int | None = None,
                  now=None) -> dict:
    """Reopen selected failed work plus descendants failed because of it.

    Successful outputs and attempt history are preserved. Jobs in
    `reconciliation_required` are never selected by ordinary retry.
    """
    if not idempotency_key:
        raise JobError("retry requires an idempotency key")
    _require_tables(con)
    with _db.transaction(con, write=True):
        run = _get(con, run_id)
        if expected_revision is not None and int(run["revision"]) != expected_revision:
            raise ConflictError("stale run revision")
        if run["status"] in ("cancel_requested", "cancelled"):
            raise BlockedError("a cancelled run cannot be retried; submit a new run")
        selected = set(failed_job_ids or [])
        rows = {j["id"]: dict(j) for j in _run_jobs(con, run_id)}
        for jid in selected:
            if jid not in rows:
                raise KeyError(jid)
            if rows[jid]["state"] == "reconciliation_required":
                raise BlockedError(f"job {jid} requires reconciliation, not retry")
            if rows[jid]["state"] != "failed":
                raise BlockedError(f"job {jid} is not failed (state={rows[jid]['state']})")
            for dep in _parents(con, jid):
                if dep["kind"] == "optional":
                    continue
                pid = dep["parent_id"]
                if pid in selected:
                    continue
                try:
                    pstate = rows[pid]["state"] if pid in rows \
                        else _job(con, pid)["state"]
                except KeyError:
                    pstate = None
                if pstate != "succeeded":
                    raise BlockedError(f"job {jid} cannot retry: required parent"
                                       f" {pid} is {pstate}, not succeeded")
        # Descendants failed *because of* the selected jobs reopen too.
        reopen = set(selected)
        changed = True
        while changed:
            changed = False
            for jid, job in rows.items():
                if jid in reopen or job["state"] != "failed":
                    continue
                if job["failure_class"] != "DEPENDENCY_FAILED":
                    continue
                parents = [p["parent_id"] for p in _parents(con, jid)
                           if p["kind"] != "optional"]
                if parents and all(p in reopen or rows.get(p, {}).get("state")
                                   in ("succeeded", "queued", "running") for p in parents) \
                        and any(p in reopen for p in parents):
                    reopen.add(jid)
                    changed = True
        now_s = utcnow()
        for jid in reopen:
            con.execute("UPDATE processing_jobs SET state='queued', failure_class=NULL,"
                        " lease_token=NULL, updated_at=?, revision = revision + 1"
                        " WHERE id=?", (now_s, jid))
        _bump_run(con, run_id, now_s)
    return reduce_run(con, run_id, now=now)


def resume(con: sqlite3.Connection, run_id: str, blocked_job_ids: list,
           expected_revision: int, *, now=None) -> dict:
    """Explicitly recheck reasoned blocked work. Cannot bypass reconciliation."""
    with _db.transaction(con, write=True):
        run = _get(con, run_id)
        if int(run["revision"]) != expected_revision:
            raise ConflictError("stale run revision")
        if run["status"] in ("cancel_requested", "cancelled"):
            raise BlockedError("a cancelled run cannot be resumed; submit a new run")
        rows = {j["id"]: dict(j) for j in _run_jobs(con, run_id)}
        now_s = utcnow()
        for jid in blocked_job_ids or []:
            if jid not in rows:
                raise KeyError(jid)
            job = rows[jid]
            if job["state"] == "reconciliation_required":
                raise BlockedError(f"job {jid} requires reconciliation before resume")
            if job["state"] != "blocked":
                raise BlockedError(f"job {jid} is not blocked (state={job['state']})")
            if not _required_parents_succeeded(con, jid, rows):
                raise BlockedError(f"job {jid} cannot resume:"
                                   " required parents have not succeeded")
            con.execute("UPDATE processing_jobs SET state='queued', failure_class=NULL,"
                        " updated_at=?, revision = revision + 1 WHERE id=?",
                        (now_s, jid))
        _bump_run(con, run_id, now_s)
    return reduce_run(con, run_id, now=now)


def cancel(con: sqlite3.Connection, run_id: str, expected_revision: int,
           *, now=None) -> dict:
    """Request cancellation: stop new stages and fence publication.

    Running local work is left to observe the fence; already accepted
    remote exposure keeps its unresolved financial state.
    """
    with _db.transaction(con, write=True):
        run = _get(con, run_id)
        if int(run["revision"]) != expected_revision:
            raise ConflictError("stale run revision")
        if run["status"] in ("succeeded", "partial", "failed", "cancelled"):
            return run_summary(con, run_id)
        now_s = utcnow()
        con.execute("UPDATE processing_jobs SET state='cancelled',"
                    " failure_class=COALESCE(failure_class, 'CANCELLED'),"
                    " updated_at=? WHERE run_id=? AND state IN"
                    " ('queued','blocked','retry_wait')", (now_s, run_id))
        con.execute("UPDATE processing_runs SET status='cancel_requested',"
                    " financial_status=?, updated_at=?, revision = revision + 1"
                    " WHERE id=?", (_financial_status(con, run_id), now_s, run_id))
    return reduce_run(con, run_id, now=now)


# -- Paid operations ---------------------------------------------------------
#
# Pilot: paid processing is unavailable (L4p). No budget authority exists, so
# `reserve_paid` and `admit_dispatch` refuse every call before reading or
# writing any state. There is no cap setting, approval flag or other switch
# that turns them on: a configured cap is not a cap authority. Enabling paid
# work requires the full L4 budget-authority design
# (validation repository: docs/plans/2026-10-20-budget-authority.md,
# deferred) and a reviewed
# replacement of these two entrypoints. See docs/hosting/paid-processing.md.

# Safe to show users: no internal paths or task labels (review LOW-3).
PAID_PROCESSING_UNAVAILABLE = "paid processing is unavailable in the pilot"


class PaidProcessingUnavailable(JobError):
    """Paid reservation or dispatch refused: paid processing is off."""
    code = "PAID_PROCESSING_UNAVAILABLE"


def reserve_paid(con: sqlite3.Connection, *, workspace_id: str = "local",
                 job_id: str, worst_case_cost: float, provider_key: str,
                 idempotency_key: str, now=None) -> dict:
    """Refuse: paid processing is unavailable in the pilot (L4p).

    Always raises `PaidProcessingUnavailable` before any validation, read or
    write. A replay of an existing reservation is refused too: the existing
    row is not returned. Do not add a cap, approval or enable switch here;
    the full L4 budget authority must replace this function.
    """
    raise PaidProcessingUnavailable(PAID_PROCESSING_UNAVAILABLE)


def admit_dispatch(con: sqlite3.Connection, op_id: str, *, now=None) -> dict:
    """Refuse: paid processing is unavailable in the pilot (L4p).

    Always raises `PaidProcessingUnavailable` before any lookup or write, so
    a restored `reserved` row stays `reserved` and no paid send can follow.
    Do not add a cap, approval or enable switch here; the full L4 budget
    authority must replace this function.
    """
    raise PaidProcessingUnavailable(PAID_PROCESSING_UNAVAILABLE)


def mark_ambiguous(con: sqlite3.Connection, op_id: str, detail: str = "",
                   *, fencing_token=None, expected_revision=None,
                   now=None) -> dict:
    """Record an ambiguous paid dispatch: unresolved exposure, reconcile only.

    Fenced: only an `admitted` (still unresolved) operation may enter
    reconciliation, only from its live running claim. A presented
    `fencing_token` must match the live lease; `expected_revision` must
    match the operation revision. Jobs with settled operations
    (confirmed/released/reserved) or non-running jobs -- notably succeeded
    jobs with confirmed-success ops -- are refused, never regressed.
    """
    ts = _now_ts(now)
    with _db.transaction(con, write=True):
        op = con.execute("SELECT * FROM paid_operations WHERE id=?",
                         (op_id,)).fetchone()
        if op is None:
            raise KeyError(op_id)
        if expected_revision is not None and int(op["revision"]) != expected_revision:
            raise ConflictError("stale operation revision")
        if op["disposition"] == "unknown":
            return dict(op)  # idempotent replay of an already-ambiguous op
        if op["disposition"] != "admitted":
            raise BlockedError(f"operation {op_id} cannot enter reconciliation"
                               f" (disposition={op['disposition']})")
        if not op["job_id"]:
            raise BlockedError(f"operation {op_id} links no job")
        job = _job(con, op["job_id"])
        if job["state"] != "running":
            raise BlockedError(f"job {job['id']} cannot enter reconciliation"
                               f" (state={job['state']})")
        if not job["lease_token"]:
            raise FencingMismatch(f"job {job['id']}: no live fencing lease")
        attempt = con.execute("SELECT * FROM job_attempts WHERE job_id=?"
                              " ORDER BY attempt_no DESC LIMIT 1",
                              (job["id"],)).fetchone()
        issued = _parse_ts(attempt["heartbeat_at"]) if attempt is not None else 0.0
        if ts - issued > DEFAULT_LEASE_S:
            raise LeaseExpired(f"job {job['id']}: fencing lease expired")
        if fencing_token is not None and fencing_token != job["lease_token"]:
            raise FencingMismatch(f"job {job['id']}: fencing token is not live")
        now_s = utcnow()
        con.execute("UPDATE paid_operations SET disposition='unknown',"
                    " receipt=?, revision = revision + 1, updated_at=? WHERE id=?",
                    (json.dumps({"ambiguity": detail[:500], "at": now_s}),
                     now_s, op_id))
        if op["job_id"]:
            con.execute("UPDATE processing_jobs SET state='reconciliation_required',"
                        " failure_class='AMBIGUOUS_DISPATCH', updated_at=? WHERE id=?",
                        (now_s, op["job_id"]))
    try:
        run_id = con.execute("SELECT run_id FROM processing_jobs WHERE id=?",
                             (op["job_id"],)).fetchone()["run_id"]
        return reduce_run(con, run_id, now=now)
    except (KeyError, TypeError):
        return dict(con.execute("SELECT * FROM paid_operations WHERE id=?",
                                (op_id,)).fetchone())


def reconcile(con: sqlite3.Connection, op_id: str, request: dict,
              expected_revision: int, idempotency_key: str,
              actor_kind: str, *, now=None) -> dict:
    """Revision-checked, idempotent owner/operator reconciliation.

    `request` carries disposition, receipt, usage and rationale (mirrors
    contracts.ReconciliationRequest). Evidence must identify the original
    logical operation: the receipt must carry the operation id or the
    qualified provider key. Elapsed time, cancellation, missing local
    responses and operator guesses are not evidence of nonacceptance.
    """
    if actor_kind not in RECONCILING_ACTORS:
        raise AuthorizationError(
            f"reconciliation requires an owner or trusted operator, not {actor_kind}")
    disposition = (request or {}).get("disposition", "")
    if disposition not in RECONCILIATION_DISPOSITIONS:
        raise JobError(f"unknown reconciliation disposition: {disposition!r}")
    receipt = (request or {}).get("receipt") or {}
    if disposition != "still_unknown" and not receipt:
        raise JobError("confirmed dispositions require provider receipt evidence")
    if not idempotency_key:
        raise JobError("reconciliation requires an idempotency key")
    _require_tables(con)
    with _db.transaction(con, write=True):
        op = con.execute("SELECT * FROM paid_operations WHERE id=?",
                         (op_id,)).fetchone()
        if op is None:
            raise KeyError(op_id)
        if int(op["revision"]) != expected_revision:
            raise ConflictError("stale operation revision")
        replay = con.execute("SELECT disposition FROM p01_reconcile_log WHERE op_id=?"
                             " AND idem_key=?", (op_id, idempotency_key)).fetchone()
        if replay is not None:
            return dict(con.execute("SELECT * FROM paid_operations WHERE id=?",
                                    (op_id,)).fetchone())
        if disposition in ("confirmed_success", "confirmed_nonacceptance"):
            identifies = (receipt.get("operation_id") == op_id
                          or receipt.get("provider_key") == op["provider_key"])
            if not identifies:
                raise JobError("reconciliation evidence must identify the logical"
                               " operation (operation id or qualified provider key)")
            if disposition == "confirmed_nonacceptance" and not receipt.get("definitive"):
                raise JobError("nonacceptance requires definitive provider evidence"
                               " of no accepted work; a guess cannot release exposure")
        now_s = utcnow()
        usage = (request or {}).get("usage") or {}
        merged = {"receipt": receipt, "usage": usage,
                  "rationale": str((request or {}).get("rationale", ""))[:1000],
                  "actor": actor_kind, "at": now_s}
        con.execute("INSERT INTO p01_reconcile_log (op_id, idem_key, disposition,"
                    " actor, created_at) VALUES (?, ?, ?, ?, ?)",
                    (op_id, idempotency_key, disposition, actor_kind, now_s))
        if disposition == "confirmed_success":
            con.execute("UPDATE paid_operations SET disposition='confirmed',"
                        " receipt=?, revision = revision + 1, updated_at=? WHERE id=?",
                        (json.dumps(merged), now_s, op_id))
            if op["job_id"]:
                # Result recovery needs a fresh fenced attempt under current
                # authority; the job becomes retry-eligible, never auto-done.
                con.execute("UPDATE processing_jobs SET state='failed',"
                            " failure_class='RECONCILED_AWAITING_RESULT',"
                            " updated_at=? WHERE id=?", (now_s, op["job_id"]))
        elif disposition == "confirmed_nonacceptance":
            con.execute("UPDATE paid_operations SET disposition='released',"
                        " receipt=?, revision = revision + 1, updated_at=? WHERE id=?",
                        (json.dumps(merged), now_s, op_id))
            if op["job_id"]:
                con.execute("UPDATE processing_jobs SET state='failed',"
                            " failure_class='RECONCILED_NOT_ACCEPTED',"
                            " updated_at=? WHERE id=?", (now_s, op["job_id"]))
        else:
            con.execute("UPDATE paid_operations SET receipt=?,"
                        " revision = revision + 1, updated_at=? WHERE id=?",
                        (json.dumps(merged), now_s, op_id))
    if op["job_id"]:
        try:
            run_id = con.execute("SELECT run_id FROM processing_jobs WHERE id=?",
                                 (op["job_id"],)).fetchone()["run_id"]
            reduce_run(con, run_id, now=now)
        except (KeyError, TypeError):
            pass
    return dict(con.execute("SELECT * FROM paid_operations WHERE id=?",
                            (op_id,)).fetchone())


def get_paid_operation(con: sqlite3.Connection, op_id: str) -> dict:
    op = con.execute("SELECT * FROM paid_operations WHERE id=?", (op_id,)).fetchone()
    if op is None:
        raise KeyError(op_id)
    return dict(op)
