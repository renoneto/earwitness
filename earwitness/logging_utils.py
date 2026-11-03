"""Structured JSONL run logging: one event per line.

Each event carries run_id, stage, timestamps, input/output paths, status,
error details, and fetch/source dates where an episode is involved.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RunLogger:
    def __init__(self, runs_root: Path, run_id: str, stage: str):
        self.runs_root = Path(runs_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.stage = stage
        self.path = self.runs_root / f"{run_id}.{stage}.jsonl"
        self._n = 0

    @staticmethod
    def new_run_id(stage: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{ts}-{stage}"

    def event(self, status: str, record_id: str = "-",
              inputs: Any = None, outputs: Any = None,
              error: str | None = None, extra: dict | None = None) -> dict:
        self._n += 1
        evt = {
            "run_id": self.run_id,
            "stage": self.stage,
            "seq": self._n,
            "ts": datetime.now(timezone.utc).isoformat(),
            "record_id": record_id,
            "status": status,  # ok | skipped | failed
            "inputs": inputs,
            "outputs": outputs,
            "error": error,
        }
        if extra:
            evt.update(extra)
        with open(self.path, "a") as f:
            f.write(json.dumps(evt) + "\n")
        return evt

    def ok(self, *a, **k): return self.event("ok", *a, **k)
    def skipped(self, *a, **k): return self.event("skipped", *a, **k)
    def failed(self, *a, **k): return self.event("failed", *a, **k)
