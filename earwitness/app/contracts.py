"""Shared request/result types for the full-product service contracts.

Design contracts from full-product plan section 4. IDs are opaque strings;
numeric values must be finite. ctx is never accepted from untrusted JSON.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409
HTTP_PRECONDITION_FAILED = 412
HTTP_PRECONDITION_REQUIRED = 428

PROCESSING_MODES = {"new_episodes", "selected_files", "resample_media", "reprocess"}
FINANCIAL_STATES = {"clear", "reserved", "reconciliation_required"}
RECONCILIATION_DISPOSITIONS = {"confirmed_success", "confirmed_nonacceptance", "still_unknown"}
OBSERVATION_OPS = {"revise", "split", "merge"}
PLACEMENT_SCOPES = {"episode", "show_window"}
SHARE_OBJECT_TYPES = {"evidence", "episode", "campaign", "export", "creative"}


class ContractError(Exception):
    """Invalid request or violated precondition; maps to an HTTP status."""

    def __init__(self, message: str, status: int = HTTP_BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _require_finite(value, name: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ContractError(f"{name} must be a finite number", HTTP_BAD_REQUEST)
    return value


def _require_id(value, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ContractError(f"{name} must be a non-empty opaque id", HTTP_BAD_REQUEST)
    return value


def _require_revision(value, name: str = "expected_revision") -> int:
    if value is None:
        raise ContractError(f"missing precondition: {name}", HTTP_PRECONDITION_REQUIRED)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer", HTTP_BAD_REQUEST)
    return value


@dataclass(frozen=True)
class ProcessingRequest:
    source_ids: tuple = ()
    episode_ids: tuple = ()
    artifact_ids: tuple = ()
    mode: str = "new_episodes"
    stage_range: tuple = ()
    backend_profile_id: str = ""
    max_episodes: int = 10
    max_bytes: int = 0
    max_audio_seconds: float = 0.0
    max_cost: float = 0.0
    policy_id: str = ""

    def __post_init__(self):
        if self.mode not in PROCESSING_MODES:
            raise ContractError(f"unknown processing mode: {self.mode!r}", HTTP_BAD_REQUEST)
        _require_finite(self.max_episodes, "max_episodes")
        if self.max_cost:
            _require_finite(self.max_cost, "max_cost")


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    revision: int
    status: str
    financial_status: str = "clear"
    stage_counts: dict = field(default_factory=dict)
    blocking_reasons: tuple = ()
    reserved_cost: float = 0.0
    reported_cost: float = 0.0
    unknown_cost: float = 0.0


@dataclass(frozen=True)
class RunDetail:
    summary: RunSummary
    jobs: tuple = ()
    attempts: tuple = ()
    worker_profiles: tuple = ()
    errors: tuple = ()


@dataclass(frozen=True)
class ReconciliationRequest:
    disposition: str
    receipt: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    rationale: str = ""

    def __post_init__(self):
        if self.disposition not in RECONCILIATION_DISPOSITIONS:
            raise ContractError(f"unknown disposition: {self.disposition!r}", HTTP_BAD_REQUEST)
        if self.disposition != "still_unknown" and not self.receipt:
            raise ContractError("confirmed dispositions require provider receipt evidence",
                                HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class ObservationChange:
    operation: str
    input_revision_ids: tuple = ()
    source_pins: dict = field(default_factory=dict)
    output_intervals: tuple = ()
    attribution_refs: tuple = ()
    rationale: str = ""

    def __post_init__(self):
        if self.operation not in OBSERVATION_OPS:
            raise ContractError(f"unknown observation operation: {self.operation!r}",
                                HTTP_BAD_REQUEST)
        if not self.input_revision_ids:
            raise ContractError("observation change requires input revisions", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class CreativeMatchRequest:
    observation_ids: tuple = ()
    selection_snapshot: str = ""
    config_version: str = ""

    def __post_init__(self):
        if not self.observation_ids and not self.selection_snapshot:
            raise ContractError("creative match requires observations or a snapshot",
                                HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class ResearchSummaryRequest:
    group_by: str = "brand"
    unit: str = "evidence"
    limit: int = 50

    def __post_init__(self):
        if self.group_by not in {"brand", "show", "category", "time", "creative"}:
            raise ContractError(f"unknown group_by: {self.group_by!r}", HTTP_BAD_REQUEST)
        if self.unit not in {"evidence", "episodes", "associations", "reviews"}:
            raise ContractError(f"unknown unit: {self.unit!r}", HTTP_BAD_REQUEST)
        _require_finite(self.limit, "limit")
        if not 1 <= self.limit <= 500:
            raise ContractError("limit must be within 1..500", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class ScheduleChange:
    source_ids: tuple = ()
    mode: str = "discovery"
    interval_s: int = 21600
    profile_id: str = ""
    enabled: bool = True

    def __post_init__(self):
        if self.mode not in {"discovery", "resample"}:
            raise ContractError(f"unknown schedule mode: {self.mode!r}", HTTP_BAD_REQUEST)
        _require_finite(self.interval_s, "interval_s")
        if self.interval_s <= 0:
            raise ContractError("interval_s must be positive", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class AssignmentChange:
    target_type: str
    target_id: str
    member: str = ""

    def __post_init__(self):
        _require_id(self.target_id, "target_id")


@dataclass(frozen=True)
class CommentChange:
    target_type: str
    target_id: str
    body: str = ""

    def __post_init__(self):
        _require_id(self.target_id, "target_id")
        if not self.body or len(self.body) > 5000:
            raise ContractError("comment body must be 1..5000 characters", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class ShareRequest:
    object_type: str
    object_id: str
    audience: str = ""
    fields: tuple = ()
    download: bool = False
    expires_at: str | None = None

    def __post_init__(self):
        if self.object_type not in SHARE_OBJECT_TYPES:
            raise ContractError(f"unknown share object: {self.object_type!r}", HTTP_BAD_REQUEST)
        _require_id(self.object_id, "object_id")
        if not self.audience:
            raise ContractError("shares require a named authenticated audience", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class PlacementChange:
    campaign_id: str
    scope: str = "show_window"
    show_id: str = ""
    episode_id: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    timezone: str = "UTC"
    expected_count: int = 1
    requirements: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        _require_id(self.campaign_id, "campaign_id")
        if self.scope not in PLACEMENT_SCOPES:
            raise ContractError(f"unknown placement scope: {self.scope!r}", HTTP_BAD_REQUEST)
        if self.scope == "episode" and not self.episode_id:
            raise ContractError("episode scope requires an exact episode id", HTTP_BAD_REQUEST)
        _require_finite(self.expected_count, "expected_count")
        if self.expected_count <= 0:
            raise ContractError("expected_count must be positive", HTTP_BAD_REQUEST)
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ContractError("date_to must not precede date_from", HTTP_BAD_REQUEST)


@dataclass(frozen=True)
class PageEnvelope:
    schema_version: int = 1
    request_id: str = ""
    workspace_id: str = ""
    view_revision: str = ""
    as_of: str = ""
    applied_filter: dict = field(default_factory=dict)
    items: tuple = ()
    returned_count: int = 0
    total_matches: int = 0
    total_relation: str = "exact"
    next_cursor: str | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    coverage: dict = field(default_factory=dict)
    limitations: tuple = ()


def error_for(exc: Exception) -> tuple[int, str]:
    """Map a domain exception to an (HTTP status, message) pair."""
    from . import access as _access
    from . import search as _search
    if isinstance(exc, _search.FilterRejected):
        return (HTTP_BAD_REQUEST, str(exc))
    if isinstance(exc, _access.AuthorizationError):
        return (exc.status, str(exc))
    if isinstance(exc, ContractError):
        return (exc.status, str(exc))
    try:
        from earwitness.app import ops as _ops
        if isinstance(exc, _ops.Conflict):
            return (HTTP_PRECONDITION_FAILED, "stale revision")
        if isinstance(exc, _ops.MissingPrecondition):
            return (HTTP_PRECONDITION_REQUIRED, str(exc) or "precondition required")
        if isinstance(exc, _ops.BadInput):
            return (HTTP_BAD_REQUEST, str(exc))
    except ImportError:
        pass
    if isinstance(exc, KeyError):
        return (404, str(exc) or "unknown id")
    return (500, "internal error")
