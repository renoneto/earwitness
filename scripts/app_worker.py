#!/usr/bin/env python3
"""P01/P04 operator worker CLI: work/status/cancel/retry/resume/reconcile.

Standalone operator entrypoint. It never starts automatically with the
web server (`scripts/app_server.py` does not import this module); the
browser shows the exact launch command when no worker is live.

Workspace and lock discipline is reused from `scripts/app_workspace.py`
via import; feed parsing conventions are reused from `scripts/ingest.py`
(via `earwitness.pipeline.acquisition`); historical CLI contracts are
unchanged. All mutations run under a server-constructed AccessContext
(worker for execution, owner/operator for reconciliation).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from earwitness.app import access as _access
from earwitness.app import db as _db
from earwitness.pipeline import acquisition as _acq
from earwitness.pipeline import artifacts as _artifacts
from earwitness.pipeline import jobs as _jobs

import app_workspace as _ws


def _connect(workspace: Path):
    _db.init_schema(_ws_con(workspace))
    return _db.connect(workspace)


def _ws_con(workspace: Path):
    con = _db.connect(workspace)
    _db.init_schema(con)
    return con


def _worker_ctx(workspace_id: str, profile: str) -> _access.AccessContext:
    return _access.AccessContext.create(
        principal_id=f"worker:{profile}", principal_kind="worker",
        workspace_id=workspace_id, credential_kind="local_operator",
        scopes={"processing:execute", "research:read", "transcripts:read"})


def _owner_ctx(workspace_id: str, kind: str) -> _access.AccessContext:
    if kind not in ("owner", "operator"):
        raise SystemExit("error: reconcile requires --as owner|operator")
    principal = "owner" if kind == "owner" else "operator"
    return _access.AccessContext.create(
        principal_id=f"{principal}-cli", principal_kind="worker",
        workspace_id=workspace_id, credential_kind="local_operator",
        scopes={"processing:reconcile", "processing:execute"})


# -- Local stage handlers (bounded, offline-safe) -------------------------------
# Handlers run only after a fenced claim and publish through the ledger.
# Network/model handlers are intentionally absent: paid sends and model
# downloads require separate owner authorization (plan section 22).
# Auto mode dispatches only stages with a real local implementation;
# "noop" stays available solely as an explicit mechanics choice for
# fencing checks and is never reported as production processing.

AUTO_GAPS = {
    "fetch": "fetch needs network acquisition (disabled: no downloads)",
    "transcribe": "transcribe needs an approved local model (none configured)",
    "screen": "screen needs upstream fetch/transcribe proof (absent)",
    "refine": "refine needs upstream fetch/transcribe proof (absent)",
    "noop": "noop is mechanics tooling, not production processing;"
            " select --handler noop explicitly for fencing checks",
}


# Paid stages (L4p): never runnable in the pilot, under any handler choice.
PAID_STAGES = frozenset({"fetch", "transcribe", "screen", "refine"})


def _auto_handler(stage: str):
    """Auto-mode dispatch: real local handlers only.

    Production media stages without their authorized inputs, noop, and
    unknown stages fail loudly with the exact qualification gap. No
    fake success, no silent no-op.
    """
    if stage in AUTO_HANDLERS:
        return AUTO_HANDLERS[stage]
    raise RuntimeError(AUTO_GAPS.get(
        stage, f"no production handler for stage {stage!r}") + "; no fake success")


def _handle_noop(job: dict, args) -> dict:
    return {"output_digest": f"noop:{job['id']}", "empty_ok": False}


def _handle_fail(job: dict, args) -> dict:
    raise RuntimeError(f"handler fail requested for {job['id']}")


def _resolve_local_input(path_value: str, name: str) -> Path:
    """Bounded local inputs only: an existing file inside the repository."""
    if not path_value:
        raise RuntimeError(f"ingest handler requires --{name} <local file>")
    candidate = (REPO / path_value).resolve()
    if REPO not in candidate.parents or not candidate.is_file():
        raise RuntimeError(f"--{name} must be an existing file inside the repository")
    return candidate


def _handle_ingest(job: dict, args) -> dict:
    """Real bounded local source processing: file -> episodes and
    transcript source versions -> transcribe attestations through the
    production write path -> full index rebuild.

    Audio-only or transcript-less entries are reported as gaps, never
    proof: model transcription and downloads stay disabled, so content
    that is not locally present cannot be fabricated. A source that
    yields nothing processable fails the job with the exact gap.
    """
    from earwitness.app import coverage as _coverage
    from earwitness.app import indexing as _indexing
    source_file = _resolve_local_input(
        getattr(args, "source_file", "") or "", "source-file")
    max_entries = max(1, min(200, int(getattr(args, "max_entries", 25) or 25)))
    data = source_file.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise RuntimeError(f"source file exceeds 8 MiB bound: {source_file.name}")
    suffix = source_file.suffix.lower()
    workspace = _ws.resolve_workspace(args.workspace)
    actor = f"worker:{getattr(args, 'profile', 'local')}"
    if suffix == ".json":
        out = _ingest_transcript_file(
            workspace, actor, data, source_file.name,
            getattr(args, "episode_id", "") or "", _coverage, _indexing)
    elif suffix in (".rss", ".xml"):
        out = _ingest_feed_file(workspace, data, source_file.name, max_entries)
    else:
        raise RuntimeError(f"unsupported source kind {suffix or '<none>'}:"
                           " ingest takes a local .json transcript or .rss/.xml feed")
    out["job_id"] = job["id"]
    return out


def _ingest_transcript_file(workspace: Path, actor: str, data: bytes,
                            name: str, episode_id: str,
                            _coverage, _indexing) -> dict:
    """Transcript payload -> new source version pinned on the episode ->
    transcribe attestation -> index rebuild. The full local proof chain."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"transcript file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(
            payload.get("segments"), list) or not payload["segments"]:
        raise RuntimeError("transcript file needs a nonempty segments list;"
                           " nothing processable")
    episode_id = episode_id or str(payload.get("episode_id") or "")
    if not episode_id:
        raise RuntimeError("transcript ingest needs --episode-id or an episode_id field")
    try:
        from earwitness.app import import_sources as _imp
        _imp._validate_segments(payload["segments"], name)
    except Exception as exc:
        raise RuntimeError(f"transcript segments invalid: {exc}") from exc
    segments = payload["segments"]
    extent = max(float(s.get("end", 0.0)) for s in segments)
    canonical = json.dumps({"segments": segments}, sort_keys=True).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    with _db.workspace_lock(workspace, shared=True):
        con = _db.open_existing(workspace / "workspace.sqlite3", readonly=False)
        try:
            _db.require_current_schema(con)
            with _db.transaction(con, write=True):
                now = _db.utcnow()
                ep = con.execute("SELECT id FROM episodes WHERE id=?",
                                 (episode_id,)).fetchone()
                if ep is None:
                    con.execute(
                        "INSERT INTO episodes (id, kind, show_title, ep_title,"
                        " fetch_date, updated_at) VALUES (?, 'transcript-import',"
                        " ?, ?, ?, ?)",
                        (episode_id, str(payload.get("show_title", ""))[:300],
                         str(payload.get("ep_title", ""))[:500], now[:10], now))
                con.execute("UPDATE source_versions SET is_active=0 WHERE source_key=?",
                            (f"transcript:{episode_id}",))
                cur = con.execute(
                    "INSERT INTO source_versions (source_key, version_no, sha256,"
                    " payload, is_active, created_at) VALUES (?,"
                    " COALESCE((SELECT MAX(version_no) FROM source_versions"
                    " WHERE source_key=?), 0) + 1, ?, ?, 1, ?)",
                    (f"transcript:{episode_id}", f"transcript:{episode_id}",
                     digest, json.dumps({"segments": segments}), now))
                version_id = cur.lastrowid
                con.execute("UPDATE episodes SET transcript_version_id=?,"
                            " alignment='unknown', revision=revision+1, updated_at=? WHERE id=?",
                            (version_id, now, episode_id))
                att_id = _coverage.record_attestation(
                    con, episode_id, "transcribe", (0.0, extent), (0.0, extent),
                    source_pin=f"source-version:{version_id}",
                    producer="worker-ingest", actor=actor)
                from earwitness.app import citations, customer
                _db.record_episode_snapshot(con, episode_id, actor, "worker-ingest")
                customer.rebuild_customer_show_map(con)
                citations.publish_source_state(con, kind="episode", record_id=episode_id, actor=actor)
                for affected in con.execute("SELECT id FROM evidence WHERE episode_id=?", (episode_id,)):
                    citations.publish_source_state(con, kind="evidence", record_id=affected["id"], actor=actor)
                revision = _db.bump_revision(con)
                _db.audit(con, actor, "worker_transcript_ingest", "episode", episode_id,
                          {"transcript_version_id": version_id}, revision)
                index_out = _indexing.rebuild_all(con)
        finally:
            con.close()
    out_digest = hashlib.sha256(json.dumps(
        {"file": name, "episode": episode_id, "version": version_id,
         "attestation": att_id, "index": index_out}, sort_keys=True,
        default=str).encode()).hexdigest()
    return {"output_digest": out_digest, "episode_id": episode_id,
            "transcript_version_id": version_id,
            "transcript_attestation_id": att_id,
            "index": index_out, "empty_ok": False}


def _ingest_feed_file(workspace: Path, data: bytes, name: str,
                      max_entries: int) -> dict:
    """Feed entries -> episode metadata registration only. Enclosure
    audio is recorded as remote-unverified; nothing is downloaded and
    no transcript is fabricated, so metadata-only rows carry no
    attestation and each content gap is reported explicitly. A feed
    that yields no entries fails; a feed of already-known entries is
    a quiet re-run."""
    entries = _acq.parse_feed_entries(data, name)
    digest = hashlib.sha256()
    digest.update(data)
    registered: list[str] = []
    gaps: list[str] = []
    truncated = max(0, len(entries) - max_entries)
    with _db.workspace_lock(workspace, shared=True):
        con = _db.open_existing(workspace / "workspace.sqlite3", readonly=False)
        try:
            _db.require_current_schema(con)
            with _db.transaction(con, write=True):
                now = _db.utcnow()
                for entry in entries[:max_entries]:
                    digest.update(str(entry.get("id", entry.get("guid", ""))).encode())
                    digest.update(str(entry.get("title", "")).encode())
                    guid = str(entry.get("id", entry.get("guid", "")) or "")
                    if not guid:
                        gaps.append("entry without usable identity locator")
                        continue
                    ep_id = "feed-" + hashlib.sha256(guid.encode()).hexdigest()[:12]
                    enclosure = None
                    for link in entry.get("enclosures", []) or []:
                        href = link.get("href", "") if isinstance(link, dict) else ""
                        if href:
                            enclosure = href
                            break
                    cur = con.execute(
                        "INSERT OR IGNORE INTO episodes (id, kind, show_title,"
                        " ep_title, pub_date, fetch_date, audio_url, updated_at)"
                        " VALUES (?, 'feed', ?, ?, ?, ?, ?, ?)",
                        (ep_id, str(entry.get("author", entry.get("source", "")))[:300],
                         str(entry.get("title", ""))[:500],
                         str(entry.get("published", ""))[:64], now[:10],
                         (enclosure or "")[:1000], now))
                    if cur.rowcount:
                        registered.append(ep_id)
                        gaps.append(f"{ep_id}: audio/transcript not local;"
                                    " remote-unverified, no attestation")
                from earwitness.app import citations, customer, indexing
                customer.rebuild_customer_show_map(con)
                for episode_id in registered:
                    _db.record_episode_snapshot(con, episode_id, "worker-ingest", "worker-ingest")
                    citations.publish_source_state(con, kind="episode", record_id=episode_id, actor="worker-ingest")
                if registered:
                    revision = _db.bump_revision(con)
                    _db.audit(con, "worker-ingest", "worker_feed_ingest", "feed", name,
                              {"episodes": registered}, revision)
                    indexing.rebuild_all(con)
        finally:
            con.close()
    if not entries:
        raise RuntimeError(f"feed file yielded no entries: {name}")
    return {"output_digest": digest.hexdigest(), "entries": len(entries),
            "registered": registered, "truncated": truncated, "gaps": gaps,
            "empty_ok": not registered}


def _handle_monitor_check(job: dict, args) -> dict:
    """Scheduled-monitor path over stored opt-in alerts only.

    Matches are computed from the local index through each alert's
    stored CustomerQueryV1 scope (never caller-supplied); emissions
    bridge into monitor events + the notification outbox for the
    stored recipients; delivery runs exclusively through the local
    sandbox fixture (live sends stay disabled). Manual alerts without
    an explicit monitor action are never touched. A run with nothing
    due is a legitimate quiet check; due alerts that all fail fail
    the job with the exact gaps.
    """
    from earwitness.app import customer as _customer
    from earwitness.app import monitoring as _mon
    workspace = _ws.resolve_workspace(args.workspace)
    workspace_id = args.workspace_id or "local"
    provider = _mon.SandboxDeliveryProvider()
    ran: list[dict] = []
    gaps: list[str] = []
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            _db.init_schema(con)
            _mon.ensure_aux(con)
            due = _customer.due_monitored_alerts(con, _db.utcnow())
            for alert in due[:20]:
                aid = alert["id"]
                # The transition, its advanced baseline and its queued
                # notifications commit together: a queue failure rolls
                # the check back, so the retry sees the transition again.
                # Provider delivery stays outside this transaction. A
                # transaction that cannot start (busy database) is this
                # alert's gap; a publish failure still propagates.
                started = False
                try:
                    with _db.transaction(con, write=True):
                        started = True
                        try:
                            checked = _customer.check_alert(con, aid, actor="worker:monitor")
                        except Exception as exc:
                            gaps.append(f"alert {aid}: check failed: {exc}")
                            continue
                        if not checked.get("ok"):
                            gaps.append(f"alert {aid}: check not ok:"
                                        f" {checked.get('error', 'unknown')}")
                            continue
                        # Every transition is recorded; the per-run budget
                        # queues the oldest pending ones and defers the rest.
                        queued = _mon.publish_alert_emissions(
                            con, workspace_id, aid, checked.get("emitted_events", []),
                            alert.get("recipients", []),
                            preferences_lookup=lambda m, _c=con: _mon.get_preferences(
                                _c, workspace_id, m),
                            max_events=int(alert.get("max_events", 25)))
                except sqlite3.OperationalError as exc:
                    if started:
                        raise
                    gaps.append(f"alert {aid}: check failed: {exc}")
                    continue
                delivered = _deliver_outbox(con, workspace_id, queued, _mon,
                                            provider)
                advanced = _customer.advance_alert_monitor(
                    con, aid, int(alert.get("interval_s", 21600)),
                    _db.utcnow())
                ran.append({"alert_id": aid, "emitted": checked.get("emitted", 0),
                            "queued": queued, "delivered": delivered,
                            "next_due_at": advanced["next_due_at"]})
            con.commit()
        finally:
            con.close()
    if due and not ran:
        raise RuntimeError("monitor: all due alerts failed: " + "; ".join(gaps))
    digest = hashlib.sha256(json.dumps(
        {"alerts": [(r["alert_id"], r["emitted"], r["queued"].get("queued", 0),
                     r["delivered"].get("sent", 0)) for r in ran],
         "provider": provider.name}, sort_keys=True, default=str).encode())
    return {"output_digest": f"monitor:{digest.hexdigest()[:32]}",
            "alerts": [r["alert_id"] for r in ran], "ran": ran, "gaps": gaps,
            "empty_ok": not ran}


def _deliver_outbox(con, workspace_id: str, queued: dict, _mon,
                    provider) -> dict:
    """Deliver this alert's queued outbox entries through the local fixture.

    Drains the alert schedule's queued backlog oldest first, up to 200
    rows per run, whenever the rows were queued: a restart or the batch
    limit leaves them for this alert's next run, never strands them.
    Only rows published through the pending-publication record are
    sent. This worker never sends alert rows without that record (queued
    before it existed): on the alert's next successful check its
    queued/pending ones are cancelled with the reason "not sent: no
    publication record (queued before the delivery fix)" and counted in
    not_sent_stale; rows awaiting reconciliation keep their state. Rows of other
    schedules and invites are not this handler's to send. Uncertain
    provider outcomes are recorded for reconciliation by
    attempt_delivery itself, never blind-retried; send-time preference
    gates still apply.
    """
    schedule_id = (queued or {}).get("schedule_id")
    stale = 0
    if schedule_id:
        stale = con.execute(
            "UPDATE notification_outbox SET state='cancelled', detail=?"
            " WHERE workspace_id=? AND state IN ('queued', 'pending')"
            " AND event_id IN (SELECT id FROM monitor_events WHERE schedule_id=?"
            " AND json_extract(detail, '$.publication') IS NULL)",
            (json.dumps({"reason": "not sent: no publication record"
                                   " (queued before the delivery fix)"}),
             workspace_id, schedule_id)).rowcount
    rows = con.execute("SELECT o.id FROM notification_outbox o"
                       " JOIN monitor_events e ON e.id=o.event_id"
                       " WHERE o.workspace_id=? AND e.schedule_id=?"
                       " AND json_extract(e.detail, '$.publication')='queued'"
                       " AND o.state IN ('queued', 'pending')"
                       " ORDER BY o.id LIMIT 200",
                       (workspace_id, schedule_id)).fetchall() \
        if schedule_id else []
    outbox_ids = [r[0] for r in rows]
    sent = 0
    uncertain: list[int] = []
    for outbox_id in outbox_ids:
        result = _mon.attempt_delivery(
            con, outbox_id, provider,
            preferences_lookup=lambda m, _c=con, _w=workspace_id: _mon.get_preferences(
                _c, _w, m))
        if result.get("state") == "sent":
            sent += 1
        elif result.get("state") == "uncertain":
            uncertain.append(outbox_id)
    return {"sent": sent, "uncertain": uncertain, "provider": provider.name,
            "not_sent_stale": stale}


AUTO_HANDLERS = {"monitor": _handle_monitor_check, "ingest": _handle_ingest}


HANDLERS = {"auto": None, "noop": _handle_noop, "fail": _handle_fail,
            "ingest": _handle_ingest, "monitor": _handle_monitor_check}


DEFAULT_MAX_JOB_S = 3600.0
PUBLISH_RETRIES = 4
PUBLISH_RETRY_DELAY_S = 0.5


def _keep_lease(workspace: Path, job: dict, max_job_s: float):
    """Renew `job`'s lease every heartbeat interval while its handler runs.

    Uses its own connection so a long handler keeps its live claim.
    Renewal stops when stopped, when the lease is already lost, or after
    `max_job_s`: a hung handler then loses its lease to expiry instead of
    holding it forever. Returns (stop function, lost event); `lost` is set
    when renewal finds the claim gone, so the caller never publishes
    blind. Raises RuntimeError before any work starts if the heartbeat
    cannot open its connection.
    """
    stopped = threading.Event()
    lost = threading.Event()
    ready = threading.Event()
    started = time.monotonic()
    broken: list = []

    def beat():
        try:
            con = _db.connect(workspace)
        except Exception as exc:  # noqa: BLE001 - reported to the claiming loop
            broken.append(exc)
            return
        finally:
            ready.set()
        try:
            while not stopped.wait(_jobs.DEFAULT_HEARTBEAT_S):
                if time.monotonic() - started >= max_job_s:
                    print(f"worker: job {job['id']} exceeded --max-job-seconds"
                          f" ({max_job_s:g}s); lease left to expire", file=sys.stderr)
                    return
                try:
                    if not _jobs.heartbeat(con, job["id"], job["fencing_token"]):
                        print(f"worker: lease for {job['id']} lost mid-run;"
                              " its result will not be published", file=sys.stderr)
                        lost.set()
                        return
                except sqlite3.OperationalError as exc:
                    # Contended; the next beat is still inside the lease.
                    print(f"worker: heartbeat for {job['id']} failed ({exc});"
                          " retrying", file=sys.stderr)
        finally:
            con.close()

    thread = threading.Thread(target=beat, name=f"heartbeat-{job['id']}", daemon=True)
    thread.start()
    ready.wait()
    if broken:
        thread.join()
        raise RuntimeError(f"heartbeat cannot open the workspace: {broken[0]}")

    def stop():
        stopped.set()
        thread.join()
    return stop, lost


def _publish(job: dict, publish) -> bool:
    """Run one ledger publish, retrying lock contention with bounded backoff.

    Returns False when the database stays locked; the caller decides
    what happens next (a successful result is retried on later passes,
    lease expiry is the backstop). Ledger refusals (JobError) propagate.
    """
    for n in range(PUBLISH_RETRIES):
        try:
            publish()
            return True
        except sqlite3.OperationalError as exc:
            if n + 1 == PUBLISH_RETRIES:
                print(f"worker: cannot publish {job['id']} after {PUBLISH_RETRIES}"
                      f" tries ({exc})", file=sys.stderr)
                return False
            time.sleep(PUBLISH_RETRY_DELAY_S * 2 ** n)


def _release(con, job: dict, reason: str) -> None:
    """Hand a claim back with a non-crash reason, retrying lock contention."""
    try:
        if _publish(job, lambda: _jobs.release_lease(
                con, job["id"], job["fencing_token"], reason)):
            return
    except _jobs.JobError as exc:
        print(f"worker: cannot release {job['id']}: {exc}", file=sys.stderr)
        return
    print(f"worker: {job['id']} left to lease expiry ({reason})", file=sys.stderr)


def _settle_unpublished(con, job: dict, publish, final: bool = False):
    """Retry a lock-exhausted publish (success or failure) of a finished job.

    Returns True once published, False when it never will be: the lease
    was lost meanwhile, or this is the worker's final try. Either way the
    claim is released with its true non-crash reason, so a locked database
    never dead-letters a healthy job. Returns None while still locked.
    """
    try:
        publish()
        return True
    except sqlite3.OperationalError:
        if not final:
            return None
        _release(con, job, "publish_failed: database locked")
    except _jobs.JobError as exc:
        _release(con, job, f"publish_failed: database locked, then {exc}")
    return False


def cmd_work(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    workspace_id = args.workspace_id or "local"
    ctx = _worker_ctx(workspace_id, args.profile)
    ctx.require_member("work")
    handler = HANDLERS.get(args.handler or "auto", None)
    max_job_s = float(getattr(args, "max_job_seconds", DEFAULT_MAX_JOB_S))
    max_attempts = int(getattr(args, "max_attempts", _jobs.DEFAULT_MAX_ATTEMPTS))
    done, failed = 0, 0
    # Finished jobs whose publish stayed locked: (job, publish, succeeded),
    # retried each pass.
    unpublished: list = []
    locked_tries = 0
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            _db.init_schema(con)
            _jobs.ensure_tables(con)
            _artifacts.ensure_tables(con)
            while done + failed < args.max_jobs:
                for pending in list(unpublished):
                    settled = _settle_unpublished(con, pending[0], pending[1])
                    if settled is not None:
                        unpublished.remove(pending)
                        if settled and pending[2]:
                            done += 1
                        else:
                            failed += 1
                try:
                    # Reclaim claims of killed or hung workers first; nothing
                    # else returns an expired `running` job to the queue.
                    _jobs.expire_leases(con, max_attempts=max_attempts)
                    # --run filters inside the claim: foreign jobs are never
                    # claimed, held or released by this worker.
                    job = _jobs.claim_next(con, workspace_id=workspace_id,
                                           worker_id=f"worker:{args.profile}",
                                           profiles=([args.profile] if args.profile != "any" else ()),
                                           run_id=args.run or None)
                except sqlite3.OperationalError as exc:
                    locked_tries += 1
                    if locked_tries >= PUBLISH_RETRIES:
                        print(f"worker: cannot claim ({exc}); stopping this pass",
                              file=sys.stderr)
                        break
                    time.sleep(PUBLISH_RETRY_DELAY_S * 2 ** (locked_tries - 1))
                    continue
                locked_tries = 0
                if job is None:
                    break
                try:
                    stop_heartbeat, lease_lost = _keep_lease(workspace, job, max_job_s)
                except RuntimeError as exc:
                    # No renewal means no safe handler run: hand the claim
                    # back with its reason and stop rather than run blind.
                    print(f"worker: {exc}; releasing {job['id']} and stopping",
                          file=sys.stderr)
                    try:
                        _jobs.release_lease(con, job["id"], job["fencing_token"], str(exc))
                    except (_jobs.JobError, sqlite3.OperationalError):
                        pass  # lease expiry reclaims it
                    failed += 1
                    break
                pick = handler or HANDLERS.get("noop")
                auto = (args.handler or "auto") == "auto"
                error = None
                try:
                    try:
                        if auto:
                            if job["stage"] == "fail":
                                pick = HANDLERS["fail"]
                            else:
                                # Raises with the exact qualification gap for
                                # unqualified/unknown stages: auto never reports
                                # noop or digest-only work as production success,
                                # and the gap is published as the job failure.
                                pick = _auto_handler(job["stage"])
                        elif job["stage"] in PAID_STAGES:
                            # An explicit handler never turns a paid stage into
                            # a successful no-op (L4p): fail it visibly.
                            raise RuntimeError(
                                f"{job['stage']} is a paid stage and paid"
                                " processing is unavailable in the pilot;"
                                f" handler {args.handler!r} refused; no fake success")
                        out = pick(job, args)
                    except Exception as exc:  # noqa: BLE001 - worker must record, not crash
                        error = str(exc)
                    if lease_lost.is_set():
                        # Fencing would refuse it anyway; never publish blind.
                        failed += 1
                        continue
                    if error is None:
                        publish = (lambda job=job, out=out: _jobs.publish_success(
                            con, job["id"], job["fencing_token"],
                            out["output_digest"], empty_ok=out.get("empty_ok", False)))
                        try:
                            if _publish(job, publish):
                                done += 1
                            else:
                                unpublished.append((job, publish, True))
                            continue
                        except Exception as exc:  # noqa: BLE001 - recorded as the failure
                            error = str(exc)
                    publish = (lambda job=job, error=error: _jobs.publish_failure(
                        con, job["id"], job["fencing_token"],
                        failure_class="HANDLER_FAILED", error=error, retryable=False))
                    try:
                        if not _publish(job, publish):
                            unpublished.append((job, publish, False))
                            continue
                    except _jobs.JobError as fence_exc:
                        print(f"worker: cannot record failure for {job['id']}: {fence_exc}",
                              file=sys.stderr)
                    failed += 1
                finally:
                    # Renewal covers publish retries too, so lock contention
                    # alone never costs the lease.
                    stop_heartbeat()
        finally:
            for job, publish, succeeded in unpublished:
                if _settle_unpublished(con, job, publish, final=True) and succeeded:
                    done += 1
                else:
                    failed += 1
            con.close()
    print(json.dumps({"worker": args.profile, "done": done, "failed": failed}))
    return 0 if not failed else 1


def cmd_status(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            _db.init_schema(con)
            _jobs.ensure_tables(con)
            if args.run:
                detail = _jobs.run_detail(con, args.run)
                print(json.dumps(detail, indent=2, default=str))
            else:
                rows = con.execute("SELECT id, status, financial_status, revision,"
                                   " updated_at FROM processing_runs ORDER BY rowid").fetchall()
                print(json.dumps({"runs": [dict(r) for r in rows]}, indent=2))
        except KeyError as exc:
            print(f"error: unknown id: {exc}", file=sys.stderr)
            return 1
        finally:
            con.close()
    return 0


def cmd_cancel(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            summary = _jobs.cancel(con, args.run, args.expected_revision)
            print(json.dumps(summary, indent=2))
        except (_jobs.ConflictError, _jobs.JobError, KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            con.close()
    return 0


def cmd_retry(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            summary = _jobs.request_retry(
                con, args.run, (args.jobs or "").split(",") if args.jobs else [],
                args.idempotency_key,
                expected_revision=args.expected_revision)
            print(json.dumps(summary, indent=2))
        except (_jobs.ConflictError, _jobs.BlockedError, _jobs.JobError,
                KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            con.close()
    return 0


def cmd_resume(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            summary = _jobs.resume(
                con, args.run, (args.jobs or "").split(",") if args.jobs else [],
                args.expected_revision)
            print(json.dumps(summary, indent=2))
        except (_jobs.ConflictError, _jobs.BlockedError, _jobs.JobError,
                KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            con.close()
    return 0


def cmd_reconcile(args) -> int:
    workspace = _ws.resolve_workspace(args.workspace)
    workspace_id = args.workspace_id or "local"
    try:
        ctx = _owner_ctx(workspace_id, args.as_role)
        ctx.require_member("reconcile_paid_operation")
    except (SystemExit, _access.AuthorizationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        receipt = json.loads(args.receipt or "{}")
        usage = json.loads(args.usage or "{}")
    except ValueError as exc:
        print(f"error: invalid JSON evidence: {exc}", file=sys.stderr)
        return 1
    request = {"disposition": args.disposition, "receipt": receipt,
               "usage": usage, "rationale": args.rationale or ""}
    with _db.workspace_lock(workspace, shared=True):
        con = _db.connect(workspace)
        try:
            op = _jobs.reconcile(con, args.operation, request,
                                 args.expected_revision, args.idempotency_key,
                                 args.as_role)
            print(json.dumps(op, indent=2, default=str))
        except (_jobs.ConflictError, _jobs.AuthorizationError, _jobs.JobError,
                KeyError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            con.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".muse-product-p01p04/work",
                        help="repository-relative workspace directory")
    parser.add_argument("--workspace-id", default="local")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("work", help="claim and execute runnable jobs")
    p.add_argument("--profile", required=True)
    p.add_argument("--run", default="")
    p.add_argument("--max-jobs", type=int, default=10)
    p.add_argument("--handler", default="auto", choices=["auto", "noop", "fail", "ingest", "monitor"])
    p.add_argument("--source-file", default="",
                   help="repository-relative local source for ingest (.json transcript or .rss/.xml feed)")
    p.add_argument("--episode-id", default="",
                   help="episode id for transcript ingest (or read from the file)")
    p.add_argument("--max-entries", type=int, default=25,
                   help="bound on feed entries processed per ingest (1..200)")
    p.add_argument("--max-job-seconds", type=float, default=DEFAULT_MAX_JOB_S,
                   help="stop renewing a job's lease after this long, so a hung"
                        " handler loses its claim to expiry")
    p.add_argument("--max-attempts", type=int, default=_jobs.DEFAULT_MAX_ATTEMPTS,
                   help="lease expiries before a crash-looping job fails terminally")

    p = sub.add_parser("status", help="show runs or one run detail")
    p.add_argument("--run", default="")

    p = sub.add_parser("cancel", help="request run cancellation")
    p.add_argument("--run", required=True)
    p.add_argument("--expected-revision", type=int, required=True)

    p = sub.add_parser("retry", help="retry selected failed jobs")
    p.add_argument("--run", required=True)
    p.add_argument("--jobs", default="")
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--expected-revision", type=int, default=None)

    p = sub.add_parser("resume", help="recheck blocked jobs")
    p.add_argument("--run", required=True)
    p.add_argument("--jobs", default="")
    p.add_argument("--expected-revision", type=int, required=True)

    p = sub.add_parser("reconcile", help="reconcile a paid operation (owner/operator)")
    p.add_argument("--operation", required=True)
    p.add_argument("--disposition", required=True,
                   choices=sorted(_jobs.RECONCILIATION_DISPOSITIONS))
    p.add_argument("--expected-revision", type=int, required=True)
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--as", dest="as_role", required=True, choices=["owner", "operator"])
    p.add_argument("--receipt", default="{}")
    p.add_argument("--usage", default="{}")
    p.add_argument("--rationale", default="")

    args = parser.parse_args(argv)
    try:
        if args.command == "work":
            return cmd_work(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "cancel":
            return cmd_cancel(args)
        if args.command == "retry":
            return cmd_retry(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "reconcile":
            return cmd_reconcile(args)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
