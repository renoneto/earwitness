"""Shared path/config resolution for EarWitness reference scripts.

Every stage resolves paths from explicit CLI flags first, then the
``EARWITNESS_*`` environment variables, then stable repo-relative defaults.
No hard-coded ``/home/hatch/...`` paths remain anywhere in ``scripts/``.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

# scripts/<name>.py -> repo root is one level up from scripts/
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "data_root": REPO_ROOT / "data",
    "transcript_root": REPO_ROOT / "transcripts",
    "output_root": REPO_ROOT / "output",
    "runs_root": REPO_ROOT / "runs",
    "clips_root": REPO_ROOT / "clips",
    "audio_root": REPO_ROOT / "audio",
}


def resolve(name: str, cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    env = os.environ.get(f"EARWITNESS_{name.upper()}")
    if env:
        return Path(env)
    return DEFAULTS[name]


def add_common_args(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--data-root", default=None, help="Directory holding episodes.json, briefs.json, ...")
    p.add_argument("--transcript-root", default=None, help="Directory holding <ep_id>.json transcripts")
    p.add_argument("--output-root", default=None, help="Directory for generated outputs (default: <repo>/output)")
    p.add_argument("--runs-root", default=None, help="Directory for structured JSONL run logs")
    p.add_argument("--run-id", default=None, help="Run identifier (default: auto timestamp)")
    p.add_argument("--limit", type=int, default=None, help="Cap episodes processed (bounded runs)")
    p.add_argument("--episodes", default=None,
                   help="Comma-separated ep_id allowlist (e.g. s000e0,s002e1)")
    return p


def parse_episode_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    return {e.strip() for e in value.split(",") if e.strip()}


def _valid_fetch_date(value: object) -> bool:
    return bool(value) and str(value).strip() not in ("", "?", "unknown")


def resolve_fetch_date(explicit: str | None,
                       *sources: object) -> tuple[str, str]:
    """Shared explicit fetch-date fallback (T-042 fetch-date criterion).

    Precedence: explicit CLI value > EARWITNESS_FETCH_DATE env > first valid
    source (episode metadata, then transcript body) > current UTC date.
    Returns (date, provenance) where provenance is one of "explicit",
    "source", or "fallback:processing-date". The fallback is the
    package/processing date for supplied transcripts whose source fetch date
    is unavailable (e.g. bench_*.json) — it must never be read as the date
    the benchmark media was fetched.
    """
    from datetime import datetime, timezone
    if explicit and _valid_fetch_date(explicit):
        return str(explicit), "explicit"
    env = os.environ.get("EARWITNESS_FETCH_DATE")
    if env and _valid_fetch_date(env):
        return str(env), "explicit"
    for s in sources:
        if _valid_fetch_date(s):
            return str(s), "source"
    return datetime.now(timezone.utc).date().isoformat(), "fallback:processing-date"
