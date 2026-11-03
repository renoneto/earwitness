"""When the data last updated (fresh-data design 11 and 12.1).

The nightly window runner (scripts/pilot_window.py) writes
<records_dir>/last-success.json only after a data success:
{"run_key", "outcome": "success" | "success_no_change", "at": <finish time>}.
Overview shows that time as "Data updated". Anything else is unknown:
never "now", never a database time.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat

# records_dir in /etc/earwitness/sync/window.json (window.example.json).
DEFAULT_LAST_SUCCESS_FILE = "/var/lib/earwitness/sync/runs/last-success.json"
SUCCESS_OUTCOMES = ("success", "success_no_change")
CLOCK_SKEW_S = 300  # a time further ahead of this host's clock is not trusted
MAX_BYTES = 4096


def data_updated_at(path, now: float):
    """The `at` of the last successful import, as written, or None."""
    if not path:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        record = json.loads(os.read(fd, MAX_BYTES + 1))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(record, dict) or not isinstance(record.get("run_key"), str) \
            or record.get("outcome") not in SUCCESS_OUTCOMES \
            or not isinstance(record.get("at"), str):
        return None
    try:
        at = dt.datetime.fromisoformat(record["at"])
    except ValueError:
        return None
    if at.tzinfo is None or at.timestamp() > now + CLOCK_SKEW_S:
        return None
    return record["at"]
