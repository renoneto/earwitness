"""Slack posting and alert dedupe for the host watch and the Mini (fresh-data
design 12.2-12.3). Stdlib only.

The webhook URL is a credential. It reaches curl only on stdin, as a curl
config (`curl -sS --max-time 10 -K -`), never on argv, so it does not show in
process lists. The body is fixed: a source name, codes and counts. The
response is discarded. Nothing here prints or returns the URL.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import tempfile

CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")
URL_RE = re.compile(r"https://hooks\.slack\.com/[A-Za-z0-9/_.-]{1,300}")
CURL_ARGV = ("-sS", "--max-time", "10", "-K", "-")
MAX_PENDING = 200


def code(value) -> str:
    """A code as given, or `invalid_code`: never free text."""
    return value if isinstance(value, str) and CODE_RE.fullmatch(value) else "invalid_code"


def valid_url(url) -> bool:
    return isinstance(url, str) and bool(URL_RE.fullmatch(url))


def body(source: str, codes) -> dict:
    counts = collections.Counter(code(c) for c in codes)
    parts = [c if n == 1 else f"{c} x{n}" for c, n in sorted(counts.items())]
    total = sum(counts.values())
    return {"text": f"EarWitness {source}: {total} alert{'s' if total != 1 else ''}: "
                    + ", ".join(parts)}


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def post(url: str, source: str, codes, *, curl: str = "curl", timeout_s: int = 30) -> str:
    """`ok`, `invalid_url` or `failed:<curl exit or error name>`."""
    if not valid_url(url):
        return "invalid_url"
    config = "\n".join([
        f"url = {_quote(url)}",
        'header = "Content-Type: application/json"',
        f"data = {_quote(json.dumps(body(source, codes), sort_keys=True))}",
        'output = "/dev/null"',
        "fail", ""])
    try:
        result = subprocess.run([curl, *CURL_ARGV], input=config.encode(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout_s, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"failed:{type(exc).__name__}"
    return "ok" if result.returncode == 0 else f"failed:{result.returncode}"


# ------------------------------------------------------------------ dedupe

def load_state(path: str) -> dict:
    try:
        with open(path) as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    """Atomic, mode 0600 (it holds codes and cursors, never the URL)."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def to_post(state: dict, conditions: dict, events: list) -> list:
    """Codes to post now: conditions not yet posted, then events not yet
    posted (kept from a failed post), then new events.

    conditions: key -> code for every condition that holds now. A key stays
    posted while it holds, and can post again after it cleared.
    events: codes read once from a log (journal, alerts.log).
    """
    sent = set(state.get("sent") or [])
    return ([c for k, c in sorted(conditions.items()) if k not in sent]
            + [code(c) for c in state.get("pending") or []] + [code(c) for c in events])


def settle(state: dict, conditions: dict, events: list, posted: bool) -> dict:
    """The state after a post attempt (or no post when nothing was due).

    After a failed post, everything that was due (conditions included) moves
    to `pending`, and the conditions' keys count as sent: they post with the
    next successful run even if they cleared meanwhile, and are not added a
    second time while they hold. A key drops from `sent` once it clears.
    """
    pending = [] if posted else to_post(state, conditions, events)
    out = dict(state)
    out["sent"] = sorted(conditions)
    out["pending"] = pending[-MAX_PENDING:]
    return out
