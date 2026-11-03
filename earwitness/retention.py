"""Transcript/audio retention controls.

Defaults are safe for a local experiment: generated artifacts can be purged
without touching supplied source data (``data/``, ``transcripts/``,
``clips/s002e1_shopify_1276s.mp3`` reference clip).

Modes (also exposed as CLI flags on transcribe.py):
- retain audio for clip generation:  --retain-audio / RETAIN_AUDIO=1
- discard audio after transcription:  --discard-audio (default)
- retain transcripts: default, always kept
- purge generated artifacts: ``purge_generated(repo_root)`` deletes only
  ``output/``, ``runs/``, ``labels/__generated__`` — never ``data/`` or
  ``transcripts/``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# Supplied source data: never deleted by any retention helper.
SOURCE_PATHS = ("data", "transcripts", "docs", "scripts", "review-pack")


def retention_from_args(retain_audio: bool = False, discard_audio: bool = True) -> dict:
    """Default is discard (safe local default); any explicit retain signal wins."""
    env = os.environ.get("EARWITNESS_RETAIN_AUDIO")
    if env is not None:
        return {"retain_audio": env.strip().lower() in ("1", "true", "yes")}
    if retain_audio:
        return {"retain_audio": True}
    return {"retain_audio": not bool(discard_audio)}


def purge_generated(repo_root: Path) -> list[str]:
    """Delete regenerable outputs only. Returns removed relative paths.

    Raises ValueError if repo layout is unexpected (safety rail against
    deleting supplied source data).
    """
    root = Path(repo_root)
    if not (root / "data" / "episodes.json").exists():
        raise ValueError(f"refusing purge: {root} does not look like the experiment repo")
    removed: list[str] = []
    for name in ("output", "runs"):
        p = root / name
        if p.is_dir():
            shutil.rmtree(p)
            removed.append(name + "/")
    for pat in ("labels/*.scored.json",):
        for f in sorted(root.glob(pat)):
            f.unlink()
            removed.append(str(f.relative_to(root)))
    return removed


def describe() -> str:
    return (
        "Safe to delete: output/, runs/, labels/*.scored.json, audio/ (reproducible via ingest.py).\n"
        "Required for resumption: data/episodes.json, transcripts/*.json, data/briefs.json.\n"
        "Never auto-deleted: data/, transcripts/, docs/, scripts/, review-pack/, clips/ reference clip."
    )
