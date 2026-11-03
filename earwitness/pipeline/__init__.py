"""EarWitness processing pipeline package (P01-P13 stage logic lives here).

P00 establishes this package parent only; stage modules (jobs, acquisition,
transcription, detection, artifacts) land with their owning leaf tasks.
"""
from __future__ import annotations

PIPELINE_API_VERSION = 1

__all__ = ["PIPELINE_API_VERSION"]
