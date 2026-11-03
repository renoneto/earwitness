"""Fleet supervisor and post-run chain for one corpus (`corpus_run.py fleet`
and `corpus_run.py post-run`).

The fleet starts `corpus_run.py work` workers from arguments (worker counts
per role, backends, pythons, stop time) and supervises them:

  - a worker that is gone is started again on the next check (every
    --poll-s), except the roles that must not loop (below);
  - GPU fallbacks: the Parakeet worker `gpu-pk` exiting 3 (gap-fill
    breaker) sets <corpus>/.gf_fatal and it restarts without gap fill;
    exiting 2 (preflight / model refused) sets .pk_fatal and the mlx tiny
    worker `gpu-1` takes its place;
  - `jev-*` exiting 2 or 3 (key missing / rejected, payment) sets
    .jev_fatal: no Jev worker is started again;
  - `diarize-*` exiting non-zero (or with an unknown status) sets
    .diar_fatal: no diarize worker is started again (the queue then moves
    waiting episodes on without speakers);
  - --allow-gap-fill-change is passed to gpu-pk only while the corpus
    record (meta parakeet_model) does not yet hold the wanted gap fill; when
    that record cannot be read (lock, corruption) gpu-pk is not started on
    that check, never started with a guess.

Delete a flag file to allow that role again. Every worker is started in its
own session through a small shell wrapper that writes its exit status to
<corpus>/fleet/<worker>.rc, so workers outlive a supervisor restart and
their exit status is still read afterwards. A restarted supervisor adopts
the running `work` processes of the corpus (matched by --corpus and
--worker-id on their command line, whoever started them) instead of
starting them twice; one supervisor per corpus (lock file).

Jev workers get --jev-env-file sourced by their wrapper (`set -a; . file`),
so the file reads exactly as the shell reads it; its values are never
logged or printed.

Logs: <log-dir>/<worker>.log per worker and <log-dir>/supervise.log. An
error inside one check (a long database lock, a failed ps or start) is
logged with its traceback and alerted; the supervisor keeps going.
Alerts: one line in <corpus>/alerts.log and one run of --alert-cmd per
condition, again only after --alert-cooldown-min: the queue stalls (no
stage completed for --stall-min while episodes remain), a worker exits
non-zero --crash-limit times in 30 minutes, the disk is below
--min-free-gb, a check fails. Once only: an episode goes dead, a fallback
flag is set (again only if the flag file changes), the supervisor exits on
an unexpected error, or a new supervisor finds the last one died.

The fleet ends when the queue is drained (every episode done or dead), at
--stop-at, or on SIGTERM / `fleet --stop`: it sends SIGTERM to the workers
(each stops claiming and finishes or gives back its lease), waits up to
--stop-timeout-s (default 1800), then sends a second SIGTERM: a worker
still on a slow lease gives it back at once, uncharged, and the episode is
retried on the next run. At --stop-at the timeout counts from the stop
time: one --stop-timeout-s in total, not one for the wait and one more for
the stop. `fleet --stop` waits with the running supervisor's timeout.
"""
from __future__ import annotations

import _thread
import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from earwitness.pipeline import corpus as cr

ROLES = ("fetch", "cpu", "gpu", "jev", "name", "diarize")
FLAG_FILES = {"gf": ".gf_fatal", "pk": ".pk_fatal", "jev": ".jev_fatal",
              "diarize": ".diar_fatal"}
FLAG_TEXT = {"gf": "gap fill turned off: gpu-pk runs Parakeet without gap fill",
             "pk": "Parakeet refused: the mlx tiny worker gpu-1 replaced gpu-pk",
             "jev": "Jev stopped: no jev worker is started again",
             "diarize": "diarize stopped: no diarize worker is started again"}
GPU_TIMEOUT_FACTOR = "0.2"
CRASH_WINDOW_S = 1800.0
SECOND_TERM_WAIT_S = 30.0
ALERT_CMD_TIMEOUT_S = 30.0
# Every worker runs behind this wrapper: it writes the exit status to $1 (the
# rc file) so a supervisor started later still reads it. The Jev variant
# first sources the env file ($2) like `set -a; . file` (exports, quotes and
# comments as the shell reads them); its values are never logged. The file
# is checked first (env_file_problem): only comments, blank lines and
# KEY=VALUE / export KEY=VALUE lines, so a stray line is never run.
_RC = 'rc=$?; echo $rc > "$f.tmp"; mv -f "$f.tmp" "$f"; exit $rc'
WRAPPER = 'f=$1; shift; "$@"; ' + _RC
WRAPPER_ENV = 'f=$1; e=$2; shift 2; set -a; . "$e"; set +a; "$@"; ' + _RC
ENV_LINE = re.compile(r"\s*(#.*)?|\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*=.*")
GAP_FILL_CORRUPT = "the corpus's gap-fill record is corrupt"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local time
    return dt.timestamp()


# ---------------------------------------------------------------- arguments

def add_fleet_args(s) -> None:
    s.add_argument("--corpus", required=True)
    s.add_argument("--stop-at", default=None,
                   help="ISO8601; workers claim nothing new after this and the fleet ends "
                        "(default: run until the queue is drained)")
    s.add_argument("--fetch", type=int, default=1, help="fetch workers")
    s.add_argument("--cpu", type=int, default=1,
                   help="CPU workers (transcribe,detect,clip with faster-whisper)")
    s.add_argument("--cpu-model", default="tiny")
    s.add_argument("--cpu-threads", type=int, default=2)
    s.add_argument("--gpu", choices=("parakeet", "mlx", "none"), default="none",
                   help="one GPU worker (transcribe,clip): parakeet = gpu-pk (falls back to "
                        "mlx tiny gpu-1 on exit 2), mlx = gpu-1 (mlx-whisper tiny)")
    s.add_argument("--gap-fill", action=argparse.BooleanOptionalAction, default=False,
                   help="parakeet: turbo gap fill (turned off for good on exit 3)")
    s.add_argument("--gap-fill-model", default="large-v3-turbo")
    s.add_argument("--jev", type=int, default=0, help="Jev workers (TypeSafe API)")
    s.add_argument("--jev-env-file", default=None,
                   help="KEY=VALUE file loaded into the Jev workers' environment only "
                        "(e.g. TYPESAFE_API_KEY); never printed")
    s.add_argument("--name", type=int, default=0, help="brand-name workers (local)")
    s.add_argument("--diarize", type=int, default=0,
                   help="diarize workers (never restarted after a non-zero exit)")
    s.add_argument("--diarize-device", choices=("mps", "cpu"), default="mps")
    s.add_argument("--python", default=sys.executable,
                   help="python for fetch, cpu, gpu-pk, jev and diarize workers")
    s.add_argument("--gpu-python", default=None,
                   help="python with mlx-whisper for gpu-1 (default: --python)")
    s.add_argument("--stt-python", default=None,
                   help="python with parakeet-mlx (and mlx-whisper for gap fill) for gpu-pk's "
                        "transcribe child (default: --python)")
    s.add_argument("--name-python", default=None,
                   help="python with GLiNER for name workers (default: --python)")
    s.add_argument("--diarize-python", default=None,
                   help="python with requirements-diarize.txt for the diarize child "
                        "(default: --python)")
    s.add_argument("--max-buffered", type=cr.buffer_cap, default=8)
    s.add_argument("--max-buffered-transcript", type=cr.buffer_cap, default=None,
                   help="every worker: a separate fetch buffer for transcript-tier audio; "
                        "--max-buffered then counts full-tier audio only (default: one "
                        "shared buffer). 0 pauses transcript-tier fetch")
    s.add_argument("--yield-while-pgrep", default=None, metavar="PATTERN",
                   help="every worker: claim nothing new while a process matching this "
                        "(e.g. 'release-builds/build/') runs (default: off)")
    s.add_argument("--min-free-gb", type=float, default=10.0,
                   help="disk guard passed to every worker, and the disk alert (0 = off)")
    s.add_argument("--nice", type=int, default=10,
                   help="nice level of fetch and cpu workers (0 = none)")
    s.add_argument("--hf-offline", action=argparse.BooleanOptionalAction, default=True,
                   help="HF_HUB_OFFLINE=1 for gpu-pk, name and diarize workers")
    s.add_argument("--extra", action="append", default=[], metavar="ROLE=ARGS",
                   help=f"more `work` flags for one role ({', '.join(ROLES)}) or all, e.g. "
                        "--extra 'cpu=--mem-cap-mb 3000'; repeatable, appended last")
    s.add_argument("--log-dir", default=None, help="worker and supervisor logs "
                   "(default: the corpus dir)")
    s.add_argument("--poll-s", type=float, default=60.0, help="check / restart interval")
    s.add_argument("--stop-timeout-s", type=float, default=1800.0,
                   help="on stop: how long each worker may keep finishing its current "
                        "lease; then a second SIGTERM makes it give the lease back at once "
                        "(slow episodes are then retried later, uncharged)")
    s.add_argument("--alert-cmd", default=None,
                   help="shell command run once per alert; the message is in $1 and "
                        "$EW_ALERT, its key in $EW_ALERT_KEY")
    s.add_argument("--alert-cooldown-min", type=float, default=180.0)
    s.add_argument("--stall-min", type=float, default=30.0,
                   help="alert when no stage completed for this long while episodes remain")
    s.add_argument("--crash-limit", type=int, default=3,
                   help="alert when one worker exits non-zero this often in 30 minutes")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="print the worker commands the fleet would start now; start nothing")
    g.add_argument("--stop", action="store_true",
                   help="stop this corpus's fleet: the supervisor and every worker")
    s.add_argument("--worker-script", default=None, help=argparse.SUPPRESS)  # tests


def add_post_run_args(s) -> None:
    s.add_argument("--corpus", required=True)
    s.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True,
                   help="first wait until every episode is done or dead")
    s.add_argument("--deadline", default=None,
                   help="ISO8601: stop waiting at this time and run the chain anyway")
    s.add_argument("--poll-s", type=float, default=600.0)
    s.add_argument("--refine-max", type=int, default=0,
                   help="refine at most this many episodes (0 = skip refine)")
    s.add_argument("--refine-model", default="large-v3-turbo")
    s.add_argument("--refine-backend", default="mlx")
    s.add_argument("--refine-python", default=None,
                   help="python with GLiNER for refine's naming (default: --python)")
    s.add_argument("--stt-python", default=None,
                   help="python with mlx-whisper for refine's speech child")
    s.add_argument("--canon", action=argparse.BooleanOptionalAction, default=True)
    s.add_argument("--canon-corpus", action="append", default=[],
                   help="build the brand canon over this corpus too (repeatable; listed "
                        "before --corpus)")
    s.add_argument("--canon-out", default=None, help="review copy of the canon map")
    s.add_argument("--show-identity-passes", type=int, default=3,
                   help="show-identity runs on --corpus (each fetches at most "
                        "--show-identity-max-fetches feeds; cached shows are skipped)")
    s.add_argument("--other-show-identity-passes", type=int, default=0,
                   help="show-identity runs on each --canon-corpus")
    s.add_argument("--show-identity-max-fetches", type=int, default=200)
    s.add_argument("--export", default=None,
                   help="export dir; written beside it, then swapped in whole")
    s.add_argument("--python", default=sys.executable)
    s.add_argument("--hf-offline", action=argparse.BooleanOptionalAction, default=True)
    s.add_argument("--log", default=None, help="default: <corpus>/post-run.log")
    s.add_argument("--worker-script", default=None, help=argparse.SUPPRESS)  # tests


def fleet_options(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="corpus_run.py fleet")
    add_fleet_args(p)
    return p.parse_args(argv)


def post_run_options(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="corpus_run.py post-run")
    add_post_run_args(p)
    return p.parse_args(argv)


# ---------------------------------------------------------------- worker commands

@dataclass
class Spec:
    wid: str
    role: str
    cmd: list
    env: dict = field(default_factory=dict)   # added to the supervisor's environment
    env_file: str | None = None                # sourced by the wrapper; values never shown

    def display(self) -> str:
        pre = [f"{k}={v}" for k, v in self.env.items()]
        if self.env_file:
            pre.insert(0, f"<env from {self.env_file}>")
        return " ".join(pre + [shlex.join(self.cmd)])


def flags_set(corpus) -> set:
    return {k for k, name in FLAG_FILES.items() if (Path(corpus) / name).exists()}


class GapFillCorrupt(ValueError):
    """The Parakeet record or its database is damaged: it does not fix itself."""


def env_file_problem(path) -> str | None:
    """Why the env file must not be sourced, or None. Names a line number,
    never a line (the file holds secrets)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot read it ({type(exc).__name__})"
    for n, line in enumerate(lines, 1):
        if not ENV_LINE.fullmatch(line):
            return f"line {n} is not KEY=VALUE, export KEY=VALUE, a comment or blank"
    return None


def held_gap_fill(corpus) -> bool | None:
    """Does the corpus's Parakeet record hold gap fill on? No queue or no
    record: off (False). A locked database: unknown (None), never "off", so
    a transient error cannot start gpu-pk with the wrong
    --allow-gap-fill-change and trip .pk_fatal. A damaged record or
    database raises GapFillCorrupt."""
    db = Path(corpus).resolve() / "queue.sqlite3"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=15.0)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='parakeet_model'").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None
    except sqlite3.DatabaseError as exc:
        raise GapFillCorrupt(f"the corpus database cannot be read ({exc})") from exc
    if not row:
        return False
    try:
        return isinstance(json.loads(row[0]).get("gap_fill"), dict)
    except (ValueError, AttributeError, TypeError) as exc:
        raise GapFillCorrupt("meta parakeet_model is not a JSON object") from exc


def _extras(o) -> dict:
    out = defaultdict(list)
    for item in o.extra:
        role, sep, args = item.partition("=")
        if not sep or role not in ROLES + ("all",):
            raise ValueError(f"--extra {item!r}: use ROLE=ARGS with ROLE in "
                             f"{', '.join(ROLES)} or all")
        out[role] += shlex.split(args)
    return out


def configured_wids(o) -> set:
    """Every worker id the configured roles can run (both GPU ids)."""
    return ({f"fetch-{i}" for i in range(1, o.fetch + 1)}
            | {f"cpu-{i}" for i in range(1, o.cpu + 1)}
            | ({"gpu-pk", "gpu-1"} if o.gpu != "none" else set())
            | {f"jev-{i}" for i in range(1, o.jev + 1)}
            | {f"name-{i}" for i in range(1, o.name + 1)}
            | {f"diarize-{i}" for i in range(1, o.diarize + 1)})


def worker_specs(o, script=None, skipped=None) -> list:
    """The workers this fleet runs now, given the fallback flags and the
    corpus's held gap fill. Workers left out this time (the gap-fill record
    could not be read) are appended to `skipped` as (worker id, reason)."""
    corpus = str(Path(o.corpus).resolve())
    script = script or o.worker_script or str(cr.SCRIPT)
    flags = flags_set(corpus)
    extras = _extras(o)
    common = ["--max-buffered", str(o.max_buffered),
              *(["--max-buffered-transcript", str(o.max_buffered_transcript)]
                if o.max_buffered_transcript is not None else []),
              *(["--yield-while-pgrep", o.yield_while_pgrep] if o.yield_while_pgrep else []),
              "--wait-forever",
              *(["--stop-at", o.stop_at] if o.stop_at else []),
              *(["--min-free-gb", f"{o.min_free_gb:g}"] if o.min_free_gb is not None else [])]
    nice = ["nice", "-n", str(o.nice)] if o.nice else []
    offline = {"HF_HUB_OFFLINE": "1"} if o.hf_offline else {}
    specs = []

    def add(role, wid, python, stages, flags_, *, pre=(), py_flags=(), env=None,
            env_file=None):
        cmd = [*pre, python, *py_flags, script, "work", "--corpus", corpus,
               "--worker-id", wid, "--stages", stages, *flags_, *common,
               *extras["all"], *extras[role]]
        specs.append(Spec(wid, role, cmd, dict(env or {}), env_file))

    for i in range(1, o.fetch + 1):
        add("fetch", f"fetch-{i}", o.python, "fetch", [], pre=nice)
    for i in range(1, o.cpu + 1):
        add("cpu", f"cpu-{i}", o.python, "transcribe,detect,clip",
            ["--model", o.cpu_model, "--threads", str(o.cpu_threads)], pre=nice)
    gpu = o.gpu
    if gpu == "parakeet" and "pk" in flags:
        gpu = "mlx"
    if gpu == "parakeet":
        want = o.gap_fill and "gf" not in flags
        problem = None
        try:
            held = held_gap_fill(corpus)
        except GapFillCorrupt as exc:
            held, problem = None, f"{GAP_FILL_CORRUPT} ({exc})"
        gf = ["--gap-fill-model", o.gap_fill_model] if want else []
        if held is None:
            if skipped is not None:
                skipped.append(("gpu-pk", problem or "the corpus's gap-fill record cannot "
                                "be read"))
        else:
            if want != held:
                gf.append("--allow-gap-fill-change")
            add("gpu", "gpu-pk", o.python, "transcribe,clip",
                ["--backend", "parakeet", "--stt-python", o.stt_python or o.python, *gf,
                 "--timeout-factor", GPU_TIMEOUT_FACTOR], env=offline)
    elif gpu == "mlx":
        add("gpu", "gpu-1", o.gpu_python or o.python, "transcribe,clip",
            ["--backend", "mlx", "--model", "tiny", "--timeout-factor", GPU_TIMEOUT_FACTOR])
    if "jev" not in flags:
        for i in range(1, o.jev + 1):
            add("jev", f"jev-{i}", o.python, "jev", ["--allow-no-clip"],
                env_file=str(Path(o.jev_env_file).resolve()) if o.jev_env_file else None)
    for i in range(1, o.name + 1):
        add("name", f"name-{i}", o.name_python or o.python, "name",
            ["--allow-no-clip", "--name-gliner", "small"], py_flags=("-W", "ignore"),
            env=offline)
    if "diarize" not in flags:
        for i in range(1, o.diarize + 1):
            add("diarize", f"diarize-{i}", o.python, "diarize",
                ["--allow-no-clip", "--diarize-python", o.diarize_python or o.python,
                 "--diarize-device", o.diarize_device], env=offline)
    return specs


def role_of(wid: str) -> str:
    head = wid.split("-", 1)[0]
    return head if head in ROLES else "other"


# ---------------------------------------------------------------- processes

def _ps() -> list:
    """(pid, command) of this host's processes, without this process and its
    ancestors: a shell that started us shows our own command line. A failing
    `ps` raises (an empty list would read as "nothing runs")."""
    out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True,
                         text=True, check=True).stdout
    rows, parent = [], {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        parent[int(parts[0])] = int(parts[1])
        rows.append((int(parts[0]), parts[2] if len(parts) > 2 else ""))
    mine, pid = set(), os.getpid()
    while pid > 1 and pid not in mine:
        mine.add(pid)
        pid = parent.get(pid, 0)
    return [(pid, cmd) for pid, cmd in rows if pid not in mine]


def corpus_processes(corpus, subcommand: str = "work") -> list:
    """(pid, argv tokens) of `corpus_run.py <subcommand> --corpus <corpus>`
    processes on this host (the fleet's shell wrappers included)."""
    want = os.path.realpath(corpus)
    found = []
    for pid, cmd in _ps():
        if "corpus_run.py" not in cmd:
            continue
        toks = cmd.split()
        at = [k for k, t in enumerate(toks[:-1])
              if t.endswith("corpus_run.py") and toks[k + 1] == subcommand]
        if not at:
            continue
        rest = toks[at[0] + 2:]
        try:
            path = rest[rest.index("--corpus") + 1]
        except (ValueError, IndexError):
            continue
        if os.path.realpath(os.path.expanduser(path)) == want:
            found.append((pid, rest))
    return found


_INTERPRETER = re.compile(r"(ba|z|da|k)?sh|python[0-9.]*", re.I)


def is_refine_batch(cmd: str) -> bool:
    """Is this command line a running refine-batch script (run directly or by
    a shell / python), not merely a mention of one (`tail -f refine-batch.log`)?"""
    toks = cmd.split()
    if not toks:
        return False
    script = toks[0]
    if _INTERPRETER.fullmatch(os.path.basename(script)):
        script = next((t for t in toks[1:] if not t.startswith("-")), "")
    return "refine-batch" in os.path.basename(script)


def legacy_batches() -> list:
    """Pids of hand-written refine-batch scripts (any corpus; they name none)."""
    return [pid for pid, cmd in _ps() if is_refine_batch(cmd)]


def scan_workers(corpus) -> dict:
    """{worker_id: [pid, ...]} of the corpus's running `work` processes."""
    out = defaultdict(list)
    for pid, rest in corpus_processes(corpus, "work"):
        if "--worker-id" in rest and rest.index("--worker-id") + 1 < len(rest):
            out[rest[rest.index("--worker-id") + 1]].append(pid)
    return dict(out)


def _signal(pids, sig) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def leases_held(corpus, wids) -> int:
    if not wids:
        return 0
    try:
        con = cr.connect(corpus)
        try:
            return con.execute("SELECT COUNT(*) FROM episodes WHERE state='leased' AND"
                               " lease_owner IN (%s)" % ",".join("?" * len(wids)),
                               list(wids)).fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return -1


def stop_workers(corpus, timeout_s: float, log=print, reap=lambda: None,
                 poll_s: float = 0.5) -> dict:
    """SIGTERM every `work` process of the corpus, wait up to timeout_s for
    them to finish or give back their leases, then SIGTERM again (they
    release at once) and wait SECOND_TERM_WAIT_S more."""
    alive = scan_workers(corpus)
    wids = sorted(alive)
    if not alive:
        return {"stopped": [], "still_running": [], "leases_held": 0}
    log(f"stopping {', '.join(wids)} (SIGTERM)")
    termed = {p for pids in alive.values() for p in pids}
    _signal(sorted(termed), signal.SIGTERM)
    for wait_s, last in ((timeout_s, False), (SECOND_TERM_WAIT_S, True)):
        deadline = time.monotonic() + wait_s
        while alive and time.monotonic() < deadline:
            time.sleep(poll_s)
            reap()
            alive = scan_workers(corpus)
            # A process the first scan missed (ps shows "(Python)" while it
            # cannot read the arguments, e.g. during exec) gets its SIGTERM now,
            # not one whole timeout later.
            late = {p for pids in alive.values() for p in pids} - termed
            if late:
                _signal(sorted(late), signal.SIGTERM)
                termed |= late
                wids = sorted(set(wids) | set(alive))
        if not alive or last:
            break
        log(f"still running after {wait_s:g} s: {', '.join(sorted(alive))}; second SIGTERM")
        _signal([p for pids in alive.values() for p in pids], signal.SIGTERM)
    held = leases_held(corpus, wids)
    rep = {"stopped": wids, "still_running": sorted(alive), "leases_held": held}
    log(f"stop: {json.dumps(rep)}" + (" (leases expire and are reclaimed)" if held else ""))
    return rep


# ---------------------------------------------------------------- alerts

class Alerts:
    """One line in <corpus>/alerts.log and one --alert-cmd run per key, then
    silence for the cool-down (state kept across supervisor restarts)."""

    def __init__(self, corpus, cmd=None, cooldown_s=10800.0, clock=time.time, log=print):
        self.corpus = Path(corpus)
        self.cmd, self.cooldown_s, self.clock, self.log = cmd, cooldown_s, clock, log
        self.path = self.corpus / "fleet" / "alert-state.json"
        try:
            self.state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("last", {})
        self.state.setdefault("dead_seen", [])
        self.state.setdefault("once", {})

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError as exc:
            self.log(f"cannot save alert state ({exc})")

    def fire(self, key: str, message: str, cooldown_s: float | None = None) -> bool:
        now = self.clock()
        last = self.state["last"].get(key)
        cool = self.cooldown_s if cooldown_s is None else cooldown_s
        if last is not None and now - last < cool:
            return False
        self.state["last"][key] = now
        self.save()
        self._send(key, message)
        return True

    def fire_once(self, key: str, message: str, token) -> bool:
        """Alert once per (key, token): again only when the token changes
        (e.g. a flag file's mtime), never on a cool-down."""
        if self.state["once"].get(key) == token:
            return False
        self.state["once"][key] = token
        self.save()
        self._send(key, message)
        return True

    def _send(self, key: str, message: str) -> None:
        try:
            with open(self.corpus / "alerts.log", "a", encoding="utf-8") as f:
                f.write(f"{utc()} {key}: {message}\n")
        except OSError as exc:
            self.log(f"cannot write alerts.log ({exc})")
        self.log(f"ALERT {key}: {message}")
        if self.cmd:
            try:
                subprocess.run(["/bin/sh", "-c", self.cmd, "ew-alert", message],
                               env={**os.environ, "EW_ALERT": message, "EW_ALERT_KEY": key},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=ALERT_CMD_TIMEOUT_S,
                               check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                self.log(f"alert command failed: {exc}")

    def new_dead(self, rows) -> list:
        seen = set(self.state["dead_seen"])
        fresh = [r for r in rows if r[0] not in seen]
        if fresh:
            self.state["dead_seen"] = sorted(seen | {r[0] for r in fresh})
            self.save()
        return fresh


# ---------------------------------------------------------------- supervisor

class FleetBusy(RuntimeError):
    pass


def _write_replace(path: Path, text: str) -> None:
    """Write a whole new file and rename it over `path`: a reader sees the
    old content or the new, never an empty or half-written file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _halt_on_signal(event: threading.Event):
    """A SIGTERM/SIGINT handler that sets `event`. Not event.set() in the
    handler itself: the signal can land while this thread holds the event's
    lock inside event.wait(), and set() would wait for that lock forever (the
    supervisor would never stop). A new thread sets it once the lock is free.
    Once set, a repeated signal is a no-op (is_set() takes no lock; a new
    thread would raise during interpreter shutdown)."""
    return lambda *_: event.is_set() or _thread.start_new_thread(event.set, ())


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


class Fleet:
    def __init__(self, o, *, clock=time.time, disk_free=None, log=None):
        self.o = o
        self.corpus = Path(o.corpus).resolve()
        if not (self.corpus / "queue.sqlite3").exists():
            raise FileNotFoundError(f"{self.corpus}/queue.sqlite3 not found (run init first)")
        _extras(o)   # ValueError on a bad --extra before anything starts
        if o.jev and o.jev_env_file and not Path(o.jev_env_file).is_file():
            raise FileNotFoundError(f"--jev-env-file {o.jev_env_file} not found")
        self.state = self.corpus / "fleet"
        self.state.mkdir(exist_ok=True)
        self.log_dir = Path(o.log_dir).resolve() if o.log_dir else self.corpus
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.disk_free = disk_free or (lambda: shutil.disk_usage(self.corpus).free)
        self._log = log
        self.children = {}            # wid -> Popen (started by this supervisor)
        self.seen = set()             # wids seen running
        self.crashes = defaultdict(deque)
        self.started_at = clock()
        self.halt = threading.Event()
        self.skipped = set()          # (wid, reason) left out on the last check
        self.foreign = set()          # running wids outside the roles, already logged
        self.alerts = Alerts(self.corpus, o.alert_cmd, o.alert_cooldown_min * 60, clock,
                             self.log)

    def log(self, msg: str) -> None:
        """supervise.log and stdout; stderr when the log cannot be written."""
        line = f"{utc()} {msg}"
        try:
            with open(self.log_dir / "supervise.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"{line}  [supervise.log not writable: {exc}]", file=sys.stderr, flush=True)
        try:
            (self._log or (lambda m: print(m, flush=True)))(line)
        except Exception:   # noqa: BLE001 - a closed stdout must not kill the supervisor
            pass

    def failed(self, what: str) -> None:
        """An unexpected error in a check: log the traceback, alert (with the
        cool-down), keep supervising."""
        import traceback
        tb = traceback.format_exc()
        self.log(f"ERROR during {what} (supervisor continues):\n{tb.rstrip()}")
        try:
            self.alerts.fire("supervisor_error", f"error during {what}: "
                             f"{tb.strip().splitlines()[-1][:300]}; see supervise.log")
        except Exception:   # noqa: BLE001
            pass

    # -- state
    def rc_path(self, wid: str) -> Path:
        return self.state / f"{wid}.rc"

    def take_rc(self, wid: str):
        path = self.rc_path(wid)
        try:
            text = path.read_text().strip()
        except OSError:
            return None
        path.unlink(missing_ok=True)
        try:
            return int(text)
        except ValueError:
            return None

    def queue_left(self) -> int:
        con = cr.connect(self.corpus)
        try:
            return con.execute("SELECT COUNT(*) FROM episodes WHERE stage NOT IN"
                               " ('done','dead')").fetchone()[0]
        finally:
            con.close()

    def reap(self) -> None:
        for wid, p in list(self.children.items()):
            if p.poll() is not None:
                del self.children[wid]

    # -- workers
    def start(self, spec: Spec) -> None:
        problem = env_file_problem(spec.env_file) if spec.env_file else None
        if problem:
            self.alerts.fire(f"env_file:{spec.env_file}", f"{spec.wid} not started: env file "
                             f"{spec.env_file}: {problem}")
            return
        self.rc_path(spec.wid).unlink(missing_ok=True)
        wrapper = [WRAPPER_ENV, "ew-fleet", str(self.rc_path(spec.wid)), spec.env_file] \
            if spec.env_file else [WRAPPER, "ew-fleet", str(self.rc_path(spec.wid))]
        with open(self.log_dir / f"{spec.wid}.log", "ab") as out:
            p = subprocess.Popen(["/bin/sh", "-c", *wrapper, *spec.cmd],
                                 cwd=str(cr.REPO), env={**os.environ, **spec.env},
                                 stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        self.children[spec.wid] = p
        self.seen.add(spec.wid)
        self.log(f"started {spec.wid} (pid {p.pid}): {spec.display()}")

    def set_flag(self, key: str, why: str) -> None:
        path = self.corpus / FLAG_FILES[key]
        if not path.exists():
            path.touch()
            self.log(f"set {FLAG_FILES[key]} ({why}): {FLAG_TEXT[key]}")

    def on_exit(self, wid: str, rc) -> None:
        self.log(f"{wid} exit {rc if rc is not None else 'unknown'}")
        role = role_of(wid)
        if wid == "gpu-pk" and rc == 2:
            self.set_flag("pk", f"{wid} exit 2")
        elif wid == "gpu-pk" and rc == 3:
            self.set_flag("gf", f"{wid} exit 3")
        elif role == "jev" and rc in (2, 3):
            self.set_flag("jev", f"{wid} exit {rc}")
        elif role == "diarize" and rc != 0:
            self.set_flag("diarize", f"{wid} exit {rc if rc is not None else 'unknown'}")
        if rc != 0:
            now = self.clock()
            q = self.crashes[wid]
            q.append(now)
            while q and now - q[0] > CRASH_WINDOW_S:
                q.popleft()
            if len(q) >= self.o.crash_limit:
                self.alerts.fire(f"crashloop:{wid}", f"{wid} exited non-zero {len(q)} times "
                                 f"in {CRASH_WINDOW_S / 60:g} min (last exit "
                                 f"{rc if rc is not None else 'unknown'}); see "
                                 f"{self.log_dir / (wid + '.log')}")

    def tick(self) -> str | None:
        """One check: read exits, apply fallbacks, start missing workers,
        raise alerts. Returns "drained" when no episode is left."""
        self.reap()
        alive = scan_workers(self.corpus)
        rc_wids = {p.stem for p in self.state.glob("*.rc")}
        for wid in sorted((self.seen | rc_wids) - set(alive)):
            rc = self.take_rc(wid)
            if wid in self.seen or rc is not None:
                self.on_exit(wid, rc)
            self.seen.discard(wid)
        self.seen |= set(alive)
        for wid in sorted(set(alive) - configured_wids(self.o) - self.foreign):
            self.foreign.add(wid)
            self.log(f"{wid} runs outside the configured roles: left running, not restarted")
        left = self.queue_left()
        if left == 0:
            return "drained"
        skipped = []
        for spec in worker_specs(self.o, skipped=skipped):
            if spec.wid not in alive:
                self.start(spec)
        for wid, why in sorted(set(skipped) - self.skipped):
            self.log(f"{wid} not started: {why}; retrying on the next check")
            if why.startswith(GAP_FILL_CORRUPT):
                self.alerts.fire("gap_fill_corrupt", f"{wid} stays off until the record is "
                                 f"fixed: {why}")
        self.skipped = set(skipped)
        self.check_alerts(left)
        return None

    def check_alerts(self, left: int) -> None:
        o, now = self.o, self.clock()
        con = cr.connect(self.corpus)
        try:
            last_ok = con.execute("SELECT MAX(finished_at) FROM stage_runs"
                                  " WHERE outcome='ok'").fetchone()[0]
            dead = con.execute("SELECT episode_id, dead_stage, last_error FROM episodes"
                               " WHERE stage='dead' ORDER BY episode_id").fetchall()
            counts = dict(con.execute("SELECT stage, COUNT(*) FROM episodes WHERE stage"
                                      " NOT IN ('done','dead') GROUP BY 1").fetchall())
        finally:
            con.close()
        last = max(parse_iso(last_ok) if last_ok else 0.0, self.started_at)
        if left and now - last >= o.stall_min * 60:
            queue = " ".join(f"{s}={counts[s]}" for s in cr.STAGES if counts.get(s))
            self.alerts.fire("stall", f"no stage completed for {(now - last) / 60:.0f} min; "
                             f"{left} episodes left ({queue})")
        fresh = self.alerts.new_dead([tuple(r) for r in dead])
        if fresh:
            shown = "; ".join(f"{e} at {s}: {(err or '')[:120]}" for e, s, err in fresh[:5])
            more = f" (+{len(fresh) - 5} more)" if len(fresh) > 5 else ""
            self.alerts.fire(f"dead:{fresh[0][0]}", f"{len(fresh)} episode(s) went dead: "
                             f"{shown}{more}", cooldown_s=0)
        for key, name in sorted(FLAG_FILES.items()):
            try:
                token = (self.corpus / name).stat().st_mtime_ns
            except OSError:
                continue
            self.alerts.fire_once(f"fallback:{name}", FLAG_TEXT[key], token)
        if o.min_free_gb:
            try:
                free_gb = self.disk_free() / 2 ** 30
            except OSError:
                free_gb = None
            if free_gb is not None and free_gb < o.min_free_gb:
                self.alerts.fire("disk_low", f"{free_gb:.1f} GB free on {self.corpus} "
                                 f"(floor {o.min_free_gb:g} GB): workers claim no fetch")

    # -- loop
    def run(self) -> str:
        lock = _lock(self.state / "supervisor.lock")
        pid_file = self.state / "supervisor.pid"
        if lock is None:
            pid = pid_file.read_text().strip() if pid_file.exists() else "?"
            raise FleetBusy(f"a fleet supervisor already runs on {self.corpus} (pid {pid})")
        # From here a `fleet --stop` may come at any moment: halt on SIGTERM
        # before the pid is visible (the default action would kill this
        # supervisor half-started and leave a stale pid and its workers).
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, _halt_on_signal(self.halt))
        try:
            old = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            old = None
        # Until the next line a stop reads a dead supervisor's pid (stop_fleet
        # keeps re-reading it). Both files are replaced whole, never torn,
        # the timeout first, and before the alert command (up to
        # ALERT_CMD_TIMEOUT_S) runs.
        timeout_file = self.state / "stop-timeout-s"
        _write_replace(timeout_file, f"{self.o.stop_timeout_s:g}")
        _write_replace(pid_file, str(os.getpid()))
        if old and old != os.getpid():   # the lock was free: it did not exit cleanly
            self.alerts.fire("supervisor_died", f"the previous fleet supervisor (pid {old}) "
                             "ended without a clean exit; "
                             "this one adopts its workers", cooldown_s=0)
        stop_at = parse_iso(self.o.stop_at) if self.o.stop_at else None
        reason = stop_deadline = None
        try:
            adopted = {}
            try:
                adopted = scan_workers(self.corpus)
            except Exception:   # noqa: BLE001
                self.failed("the first worker scan")
            self.seen = set(adopted)
            self.log(f"supervisor start (pid {os.getpid()}) on {self.corpus}"
                     + (f"; adopted running {', '.join(sorted(adopted))}" if adopted else ""))
            while not self.halt.is_set():
                if stop_at is not None and self.clock() >= stop_at:
                    self.log("stop time reached; waiting for workers to finish their leases")
                    stop_deadline = time.monotonic() + self.o.stop_timeout_s
                    last_error = None
                    while time.monotonic() < stop_deadline and not self.halt.is_set():
                        try:
                            if not scan_workers(self.corpus):
                                break
                            self.reap()
                            last_error = None
                        except Exception as exc:   # noqa: BLE001
                            if f"{type(exc).__name__}: {exc}" != last_error:
                                self.failed("the stop-time wait")   # once per error change
                            last_error = f"{type(exc).__name__}: {exc}"
                        self.halt.wait(min(self.o.poll_s, 5.0))
                    reason = "stop_at"
                    break
                try:
                    if self.tick() == "drained":
                        self.log("queue drained: every episode is done or dead")
                        reason = "drained"
                        break
                except Exception:   # noqa: BLE001 - one bad check must not end the fleet
                    self.failed("a check")
                self.halt.wait(self.o.poll_s)
            reason = reason or "stopped"
            # After the stop-time wait only what is left of the same timeout.
            stop_workers(self.corpus, self.o.stop_timeout_s if stop_deadline is None
                         else max(0.0, stop_deadline - time.monotonic()), self.log, self.reap)
            self.log(f"supervisor exit: {reason}")
            return reason
        except BaseException as exc:
            import traceback
            self.log(f"supervisor exiting on an unexpected {type(exc).__name__}:\n"
                     f"{traceback.format_exc().rstrip()}")
            try:
                self.alerts.fire("supervisor_exit", f"the fleet supervisor exited unexpectedly "
                                 f"({type(exc).__name__}: {exc}); workers keep running "
                                 "without restarts until it is started again", cooldown_s=0)
            except Exception:   # noqa: BLE001
                pass
            raise
        finally:
            pid_file.unlink(missing_ok=True)
            timeout_file.unlink(missing_ok=True)
            os.close(lock)


def supervisor_stop_timeout(corpus, default: float) -> float:
    """The running supervisor's --stop-timeout-s (default: none runs)."""
    try:
        return float((Path(corpus).resolve() / "fleet" / "stop-timeout-s").read_text())
    except (OSError, ValueError):
        return default


def stop_fleet(corpus, timeout_s: float = 1800.0, log=print) -> dict:
    """`fleet --stop`: SIGTERM the supervisor and wait for it to stop its
    workers; without a supervisor, stop the workers here."""
    corpus = Path(corpus).resolve()
    state = corpus / "fleet"
    lock = _lock(state / "supervisor.lock")
    if lock is not None:
        try:
            return stop_workers(corpus, timeout_s, log)
        finally:
            os.close(lock)
    # A supervisor that just took the lock shows the dead one's pid (or none)
    # until it writes its own: re-read the pid while waiting, signal each new
    # one, and wait with that supervisor's own timeout.
    pid, signaled, deadline = None, set(), None
    while True:
        try:
            now = int((state / "supervisor.pid").read_text().strip())
        except (OSError, ValueError):
            now = None
        if now and now not in signaled:
            pid = now
            signaled.add(pid)
            log(f"sending SIGTERM to the fleet supervisor (pid {pid})")
            _signal([pid], signal.SIGTERM)
            deadline = None
        if deadline is None:
            wait_s = supervisor_stop_timeout(corpus, timeout_s)
            deadline = time.monotonic() + wait_s + SECOND_TERM_WAIT_S + 60
        lock = _lock(state / "supervisor.lock")
        if lock is not None:
            os.close(lock)
            break
        if time.monotonic() >= deadline:
            log("the supervisor did not stop in time")
            break
        time.sleep(0.5)
    alive = scan_workers(corpus)
    return {"supervisor_pid": pid, "still_running": sorted(alive),
            "leases_held": leases_held(corpus, sorted(alive)) if alive else 0}


def cmd_fleet(o) -> int:
    if o.stop:
        print(json.dumps(stop_fleet(o.corpus, o.stop_timeout_s)), flush=True)
        return 0
    if o.dry_run:
        skipped = []
        try:
            specs = worker_specs(o, skipped=skipped)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        for spec in specs:
            print(spec.display())
        for wid, why in skipped:
            print(f"# {wid} would not start now: {why}", file=sys.stderr)
        return 0
    try:
        fleet = Fleet(o)
        return 0 if fleet.run() else 1
    except (FleetBusy, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------- post-run

class PostRun:
    def __init__(self, o, *, runner=None, clock=time.time, sleep=time.sleep, log=None):
        self.o = o
        self.corpus = Path(o.corpus).resolve()
        self.script = o.worker_script or str(cr.SCRIPT)
        self.log_path = Path(o.log) if o.log else self.corpus / "post-run.log"
        self.runner = runner or self._run
        self.clock, self.sleep, self._log = clock, sleep, log
        self.env = {**os.environ, **({"HF_HUB_OFFLINE": "1"} if o.hf_offline else {})}

    def log(self, msg: str) -> None:
        line = f"{utc()} {msg}"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"{line}  [{self.log_path} not writable: {exc}]", file=sys.stderr, flush=True)
        (self._log or (lambda m: print(m, flush=True)))(line)

    def _run(self, cmd: list) -> int:
        with open(self.log_path, "ab") as out:
            return subprocess.run(cmd, cwd=str(cr.REPO), env=self.env, stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=subprocess.STDOUT, check=False).returncode

    def left(self) -> int:
        con = cr.connect(self.corpus)
        try:
            return con.execute("SELECT COUNT(*) FROM episodes WHERE stage NOT IN"
                               " ('done','dead')").fetchone()[0]
        finally:
            con.close()

    def cli(self, python, *args) -> list:
        return [python, self.script, *args]

    def steps(self) -> list:
        o, c = self.o, str(self.corpus)
        out = []
        if o.refine_max > 0:
            out.append(("refine", [
                "nice", "-n", "5", o.refine_python or o.python, "-W", "ignore", self.script,
                "refine", "--corpus", c, "--model", o.refine_model,
                "--backend", o.refine_backend,
                *(["--stt-python", o.stt_python] if o.stt_python else []),
                "--max-episodes", str(o.refine_max)]))
        if o.canon:
            corpora = [str(Path(p).resolve()) for p in o.canon_corpus] + [c]
            out.append(("canon", self.cli(o.python, "canon",
                                          *[a for p in corpora for a in ("--corpus", p)],
                                          *(["--out", o.canon_out] if o.canon_out else []),
                                          "--apply")))
        for corpus, passes in [(c, o.show_identity_passes)] + [
                (str(Path(p).resolve()), o.other_show_identity_passes) for p in o.canon_corpus]:
            for k in range(passes):
                out.append((f"show-identity {Path(corpus).name} {k + 1}/{passes}", self.cli(
                    o.python, "show-identity", "--corpus", corpus,
                    "--max-fetches", str(o.show_identity_max_fetches))))
        return out

    def wait(self, deadline: float | None) -> int | None:
        """Episodes left once the run settles or the deadline comes (None:
        the queue could not be read)."""
        while True:
            try:
                left = self.left()
            except Exception as exc:   # noqa: BLE001 - e.g. a long lock: try again later
                self.log(f"cannot read the queue ({type(exc).__name__}: {exc}); retrying")
                left = None
            if left == 0 or (left is not None and not self.o.wait):
                return left
            if deadline is not None and self.clock() >= deadline:
                self.log("deadline reached with "
                         f"{'an unknown number of' if left is None else left} episodes left")
                return left
            self.sleep(self.o.poll_s if left is not None else min(self.o.poll_s, 60.0))

    def busy(self) -> str | None:
        """A refine on this corpus, or a legacy refine-batch script. (Another
        post-run cannot run: it holds the post-run lock.)"""
        if corpus_processes(self.corpus, "refine"):
            return "a running refine on this corpus"
        if legacy_batches():
            return "a running refine-batch script"
        return None

    def wait_idle(self, deadline: float | None) -> bool:
        """Wait while busy(). True when the deadline came first."""
        waiting = None
        while True:
            try:
                why = self.busy()
            except Exception as exc:   # noqa: BLE001 - e.g. ps failed: check again
                why = f"a process check that failed ({type(exc).__name__}: {exc})"
            if why is None:
                return False
            if why != waiting:
                self.log(f"waiting for {why}")
                waiting = why
            if deadline is not None and self.clock() >= deadline:
                self.log(f"deadline reached while waiting for {why}; going on without "
                         "this post-run's refine")
                return True
            self.sleep(60)

    def run(self) -> int:
        lock = _lock(self.corpus / "fleet" / "post-run.lock")
        if lock is None:
            raise FleetBusy(f"another post-run already runs on {self.corpus}")
        try:
            return self._run_locked()
        finally:
            os.close(lock)

    def _run_locked(self) -> int:
        deadline = parse_iso(self.o.deadline) if self.o.deadline else None
        left = self.wait(deadline)
        self.log(f"run settled (left={'unknown' if left is None else left}); "
                 "starting post-run")
        skip_refine = self.wait_idle(deadline)
        failed = []
        for name, cmd in self.steps():
            if name == "refine" and skip_refine:
                self.log("refine: skipped (another refine still ran at the deadline)")
                failed.append("refine (skipped)")
                continue
            failed += self.step(name, cmd)
        if self.o.export:
            failed += self.export(Path(self.o.export).resolve())
        failed += self.step("status", self.cli(self.o.python, "status", "--corpus",
                                               str(self.corpus)))
        self.log("POST-RUN DONE" + (f" with failed steps: {', '.join(failed)}"
                                    if failed else ""))
        return 1 if failed else 0

    def step(self, name: str, cmd: list) -> list:
        self.log(f"{name}: {shlex.join(cmd)}")
        try:
            rc = self.runner(cmd)
        except Exception as exc:   # noqa: BLE001 - one failed step must not end the chain
            self.log(f"{name}: could not run ({type(exc).__name__}: {exc})")
            return [name]
        self.log(f"{name}: exit {rc}")
        return [] if rc == 0 else [name]

    def export(self, out: Path) -> list:
        """Export beside `out`, then swap it in whole: a reader never sees a
        half-written export, and a failed export leaves the last one."""
        tmp = out.with_name(f".{out.name}.new")
        old = out.with_name(f".{out.name}.old")
        shutil.rmtree(tmp, ignore_errors=True)
        failed = self.step("export", self.cli(self.o.python, "export", "--corpus",
                                              str(self.corpus), "--out", str(tmp)))
        if failed:
            shutil.rmtree(tmp, ignore_errors=True)
            return failed
        try:
            shutil.rmtree(old, ignore_errors=True)
            if out.exists():
                os.replace(out, old)
            os.replace(tmp, out)
            shutil.rmtree(old, ignore_errors=True)
        except OSError as exc:
            self.log(f"export: could not swap in {out} ({exc}); the new export is at {tmp}")
            return ["export swap"]
        self.log(f"export: {out} replaced")
        return []


def cmd_post_run(o) -> int:
    if not (Path(o.corpus) / "queue.sqlite3").exists():
        print(f"ERROR: {o.corpus}/queue.sqlite3 not found", file=sys.stderr)
        return 2
    try:
        return PostRun(o).run()
    except FleetBusy as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
