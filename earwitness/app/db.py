"""EarWitness product workspace: SQLite schema and connections (stdlib only)."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import wraps
from itertools import count as _itertools_count
import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 8

_SCHEMA_V3 = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  payload TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (source_key, version_no)
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  alias_of TEXT,
  show_title TEXT,
  ep_title TEXT,
  category TEXT,
  pub_date TEXT,
  duration_min REAL,
  fetch_date TEXT,
  audio_url TEXT,
  media_id TEXT,
  transcript_version_id INTEGER REFERENCES source_versions(id),
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_version_id INTEGER REFERENCES source_versions(id),
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  orig_start_s REAL,
  orig_end_s REAL,
  excerpt TEXT NOT NULL DEFAULT '',
  attrs TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
  judgment TEXT,
  eff_start_s REAL,
  eff_end_s REAL,
  notes TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brand_assocs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  brand TEXT NOT NULL,
  basis TEXT NOT NULL,
  origin TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  aggregate_rev INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  brand TEXT,
  date_from TEXT,
  date_to TEXT,
  target_shows TEXT NOT NULL DEFAULT '[]',
  message TEXT NOT NULL DEFAULT '',
  offer TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  manual_conclusion TEXT,
  manual_reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_evidence (
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  evidence_id TEXT NOT NULL,
  PRIMARY KEY (campaign_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS campaign_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  check_type TEXT NOT NULL,
  evidence_id TEXT,
  params TEXT NOT NULL DEFAULT '{}',
  manual_verdict TEXT,
  manual_reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  format TEXT NOT NULL,
  filter TEXT NOT NULL DEFAULT '{}',
  aggregate_rev INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence (episode_id);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence (source);
CREATE INDEX IF NOT EXISTS idx_assocs_evidence ON brand_assocs (evidence_id);
CREATE INDEX IF NOT EXISTS idx_assocs_brand ON brand_assocs (brand);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_idempotency ON audit (action, target_type,
  json_extract(detail, '$.idempotency_key'), json_extract(detail, '$.digest'));
"""

# Additive-only schema v4 extension points for P01-P13. Every new table
# carries a workspace_id so hosted mode can enforce composite workspace FKs;
# local mode uses the single-owner workspace id 'local'.
_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS processing_runs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  status TEXT NOT NULL DEFAULT 'queued',
  financial_status TEXT NOT NULL DEFAULT 'clear',
  mode TEXT NOT NULL DEFAULT 'new_episodes',
  selection TEXT NOT NULL DEFAULT '{}',
  config_digest TEXT NOT NULL DEFAULT '',
  initiator TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  run_id TEXT NOT NULL REFERENCES processing_runs(id),
  stage TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  input_digest TEXT NOT NULL DEFAULT '',
  lease_token TEXT,
  failure_class TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  job_id TEXT NOT NULL REFERENCES processing_jobs(id),
  attempt_no INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'running',
  heartbeat_at TEXT,
  reported_cost TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (job_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS paid_operations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  job_id TEXT REFERENCES processing_jobs(id),
  reservation TEXT NOT NULL DEFAULT '{}',
  provider_key TEXT NOT NULL DEFAULT '',
  disposition TEXT NOT NULL DEFAULT 'unknown',
  receipt TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_artifacts (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  digest TEXT NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  duration_s REAL,
  format TEXT NOT NULL DEFAULT '',
  locator TEXT NOT NULL DEFAULT '',
  rights_state TEXT NOT NULL DEFAULT 'unknown',
  fetch_date TEXT,
  fetch_date_source TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (workspace_id, digest)
);
CREATE TABLE IF NOT EXISTS transcript_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT REFERENCES episodes(id),
  media_artifact_id TEXT REFERENCES media_artifacts(id),
  parent_id INTEGER REFERENCES transcript_versions(id),
  backend TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  config_digest TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  segments TEXT NOT NULL DEFAULT '[]',
  source_offsets TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ad_observations (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT REFERENCES episodes(id),
  media_artifact_id TEXT REFERENCES media_artifacts(id),
  transcript_version_id INTEGER REFERENCES transcript_versions(id),
  source_version_id INTEGER REFERENCES source_versions(id),
  status TEXT NOT NULL DEFAULT 'proposed',
  start_s REAL,
  end_s REAL,
  clip_media_id TEXT,
  clip_start_s REAL,
  clip_end_s REAL,
  clip_sha256 TEXT,
  superseded_by TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  observation_id TEXT NOT NULL REFERENCES ad_observations(id),
  revision INTEGER NOT NULL,
  values_json TEXT NOT NULL DEFAULT '{}',
  source_pins TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (observation_id, revision)
);
CREATE TABLE IF NOT EXISTS observation_attributions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  observation_id TEXT NOT NULL REFERENCES ad_observations(id),
  entity TEXT NOT NULL DEFAULT '',
  raw_phrase TEXT NOT NULL DEFAULT '',
  channel TEXT NOT NULL DEFAULT 'unspecified',
  method TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'suggested',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS creatives (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'suggested',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS creative_memberships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  creative_id TEXT NOT NULL REFERENCES creatives(id),
  observation_id TEXT NOT NULL REFERENCES ad_observations(id),
  method TEXT NOT NULL DEFAULT '',
  score REAL,
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (creative_id, observation_id)
);
CREATE TABLE IF NOT EXISTS occurrences (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT REFERENCES episodes(id),
  correspondence TEXT NOT NULL DEFAULT 'confirmed',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrence_observations (
  occurrence_id TEXT NOT NULL REFERENCES occurrences(id),
  observation_id TEXT NOT NULL REFERENCES ad_observations(id),
  basis TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (occurrence_id, observation_id)
);
CREATE TABLE IF NOT EXISTS coverage_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  source_key TEXT NOT NULL DEFAULT '',
  episode_id TEXT REFERENCES episodes(id),
  definition_revision INTEGER NOT NULL DEFAULT 1,
  states TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_attestations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  source_pin TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT '',
  req_start_s REAL NOT NULL DEFAULT 0.0,
  req_end_s REAL NOT NULL DEFAULT 0.0,
  succ_start_s REAL NOT NULL DEFAULT 0.0,
  succ_end_s REAL NOT NULL DEFAULT 0.0,
  status TEXT NOT NULL DEFAULT 'success',
  producer TEXT NOT NULL DEFAULT '',
  producer_rev INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL DEFAULT '',
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  mode TEXT NOT NULL DEFAULT 'discovery',
  source_ids TEXT NOT NULL DEFAULT '[]',
  interval_s INTEGER NOT NULL DEFAULT 21600,
  enabled INTEGER NOT NULL DEFAULT 1,
  definition_revision INTEGER NOT NULL DEFAULT 1,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  schedule_id TEXT NOT NULL REFERENCES schedules(id),
  event_key TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (schedule_id, event_key)
);
CREATE TABLE IF NOT EXISTS notification_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  event_id INTEGER REFERENCES monitor_events(id),
  recipient TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  member TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  object_type TEXT NOT NULL,
  object_id TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT '',
  fields TEXT NOT NULL DEFAULT '[]',
  download INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expected_placements (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  scope TEXT NOT NULL DEFAULT 'show_window',
  show_id TEXT NOT NULL DEFAULT '',
  episode_id TEXT,
  date_from TEXT,
  date_to TEXT,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  expected_count INTEGER NOT NULL DEFAULT 1,
  requirements TEXT NOT NULL DEFAULT '{}',
  provenance TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placement_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  placement_id TEXT NOT NULL REFERENCES expected_placements(id),
  campaign_revision INTEGER NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'unknown',
  reasons TEXT NOT NULL DEFAULT '[]',
  evidence_pins TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON processing_jobs (run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON job_attempts (job_id);
CREATE INDEX IF NOT EXISTS idx_obs_episode ON ad_observations (episode_id);
CREATE INDEX IF NOT EXISTS idx_occ_episode ON occurrences (episode_id);
CREATE INDEX IF NOT EXISTS idx_events_schedule ON monitor_events (schedule_id);
"""

# Agent history/policy tables (schema v5, package C). Full-value revision
# snapshots for evidence/episode/review/association/campaign state, the
# source content policy, and retained check evaluations with evaluator
# identity. history_origin marks backfilled current state ('legacy_snapshot',
# regenerable) versus mutation-time rows ('mutation'). No proposal,
# idempotency, export-manifest, or hosted tenant tables: G unselected, E owns
# hosted identity/tenancy.
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS evidence_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL,
  evidence_revision INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT '',
  episode_id TEXT NOT NULL DEFAULT '',
  orig_start_s REAL,
  orig_end_s REAL,
  excerpt TEXT NOT NULL DEFAULT '',
  attrs TEXT NOT NULL DEFAULT '{}',
  source_version_id INTEGER REFERENCES source_versions(id),
  transcript_version_id INTEGER REFERENCES source_versions(id),
  record_kind TEXT NOT NULL DEFAULT 'candidate',
  assoc_revision INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, evidence_id, evidence_revision)
);
CREATE TABLE IF NOT EXISTS episode_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT NOT NULL,
  episode_revision INTEGER NOT NULL,
  show_title TEXT,
  ep_title TEXT,
  category TEXT,
  pub_date TEXT,
  duration_min REAL,
  fetch_date TEXT,
  audio_url TEXT,
  media_id TEXT,
  transcript_version_id INTEGER REFERENCES source_versions(id),
  alignment TEXT NOT NULL DEFAULT 'unknown',
  media_edited INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, episode_id, episode_revision)
);
CREATE TABLE IF NOT EXISTS review_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL,
  review_revision INTEGER NOT NULL,
  judgment TEXT,
  eff_start_s REAL,
  eff_end_s REAL,
  notes TEXT NOT NULL DEFAULT '',
  bounds_verified INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, evidence_id, review_revision)
);
CREATE TABLE IF NOT EXISTS association_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL,
  association_revision INTEGER NOT NULL,
  assocs_json TEXT NOT NULL DEFAULT '[]',
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, evidence_id, association_revision)
);
CREATE TABLE IF NOT EXISTS campaign_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  campaign_revision INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  brand TEXT,
  date_from TEXT,
  date_to TEXT,
  target_shows TEXT NOT NULL DEFAULT '[]',
  message TEXT NOT NULL DEFAULT '',
  offer TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  manual_conclusion TEXT,
  manual_reason TEXT NOT NULL DEFAULT '',
  evidence_pins_json TEXT NOT NULL DEFAULT '[]',
  checks_json TEXT NOT NULL DEFAULT '[]',
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, campaign_id, campaign_revision)
);
CREATE TABLE IF NOT EXISTS source_content_policy (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  source_key TEXT NOT NULL,
  content_category TEXT NOT NULL,
  agent_disclosure TEXT NOT NULL DEFAULT 'allow',
  export_eligible INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, source_key, content_category)
);
CREATE TABLE IF NOT EXISTS check_evaluations (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  check_id INTEGER NOT NULL REFERENCES campaign_checks(id),
  check_revision INTEGER NOT NULL,
  evaluator_id TEXT NOT NULL,
  evidence_pins_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, campaign_id, check_id, check_revision, evaluator_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_history_id ON evidence_history (evidence_id);
CREATE INDEX IF NOT EXISTS idx_episode_history_id ON episode_history (episode_id);
CREATE INDEX IF NOT EXISTS idx_review_history_id ON review_history (evidence_id);
CREATE INDEX IF NOT EXISTS idx_assoc_history_id ON association_history (evidence_id);
CREATE INDEX IF NOT EXISTS idx_campaign_history_id ON campaign_history (campaign_id);
"""

# Backwards-compatible alias: the full base schema (v3 tables).
_SCHEMA = _SCHEMA_V3

_V3_COLUMNS = {
    "evidence": {
        "transcript_version_id": "INTEGER REFERENCES source_versions(id)",
        "source_record_key": "TEXT",
        "assoc_revision": "INTEGER NOT NULL DEFAULT 0",
    },
    "reviews": {"bounds_verified": "INTEGER NOT NULL DEFAULT 0"},
    "episodes": {
        "alignment": "TEXT NOT NULL DEFAULT 'unknown'",
        "media_edited": "INTEGER NOT NULL DEFAULT 0",
    },
    "brand_assocs": {"channel": "TEXT NOT NULL DEFAULT 'unspecified'"},
}

# v4 column extensions on legacy tables (extension points only).
_V4_COLUMNS = {
    "evidence": {
        "record_kind": "TEXT NOT NULL DEFAULT 'candidate'"
                       " CHECK (record_kind IN ('candidate','observation'))",
        "observation_revision_id": "INTEGER REFERENCES observation_revisions(id)",
    },
    "episodes": {
        "media_artifact_id": "TEXT REFERENCES media_artifacts(id)",
    },
}

# P01 stage-key column on processing_jobs (nullable text; NULL/legacy rows
# match nothing in the reuse path). Kept out of _V4_COLUMNS (legacy-table
# extensions) so verify_schema stays a subset check both ways; applied by
# ensure_processing_stage_key instead.
_STAGE_KEY_COLUMNS = {
    "processing_jobs": {"stage_key": "TEXT"},
}

# Composite workspace FKs the plan requires on hosted joins. SQLite enforces
# the single-column REFERENCES above; these compound identities are declared
# here so the hosted PostgreSQL module and reviewers share one list.
COMPOSITE_WORKSPACE_KEYS = (
    ("processing_jobs", "workspace_id", "run_id", "processing_runs", "workspace_id", "id"),
    ("job_attempts", "workspace_id", "job_id", "processing_jobs", "workspace_id", "id"),
    ("observation_revisions", "workspace_id", "observation_id", "ad_observations", "workspace_id", "id"),
    ("observation_attributions", "workspace_id", "observation_id", "ad_observations", "workspace_id", "id"),
    ("creative_memberships", "workspace_id", "creative_id", "creatives", "workspace_id", "id"),
    ("occurrence_observations", "workspace_id", "occurrence_id", "occurrences", "workspace_id", "id"),
    ("monitor_events", "workspace_id", "schedule_id", "schedules", "workspace_id", "id"),
    ("placement_evaluations", "workspace_id", "placement_id", "expected_placements", "workspace_id", "id"),
)

_V4_TABLES = (
    "processing_runs", "processing_jobs", "job_attempts", "paid_operations",
    "media_artifacts", "transcript_versions", "ad_observations",
    "observation_revisions", "observation_attributions", "creatives",
    "creative_memberships", "occurrences", "occurrence_observations",
    "coverage_records", "schedules", "monitor_events", "notification_outbox",
    "assignments", "comments", "shares", "expected_placements",
    "placement_evaluations",
)

_V5_TABLES = (
    "evidence_history", "episode_history", "review_history",
    "association_history", "campaign_history", "source_content_policy",
    "check_evaluations",
)

# Workspace-scoped identities for the v5 history/policy tables (local mode
# uses the single-owner workspace id 'local'; hosted composite enforcement
# is E's). Kept separate from P00's COMPOSITE_WORKSPACE_KEYS; do not edit
# that tuple.
_V5_COMPOSITE_KEYS = (
    ("evidence_history", "workspace_id", "evidence_id", "evidence", "workspace_id", "id"),
    ("episode_history", "workspace_id", "episode_id", "episodes", "workspace_id", "id"),
    ("review_history", "workspace_id", "evidence_id", "evidence", "workspace_id", "id"),
    ("association_history", "workspace_id", "evidence_id", "evidence", "workspace_id", "id"),
    ("campaign_history", "workspace_id", "campaign_id", "campaigns", "workspace_id", "id"),
)

_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS customer_saved_searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  query_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_show_identities (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  display_title TEXT NOT NULL DEFAULT '',
  authority_key TEXT NOT NULL DEFAULT '',
  authority_basis TEXT NOT NULL DEFAULT '',
  source_version_id INTEGER REFERENCES source_versions(id),
  unresolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_episode_shows (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  show_identity_id TEXT NOT NULL REFERENCES customer_show_identities(id),
  basis TEXT NOT NULL DEFAULT '',
  source_version_id INTEGER REFERENCES source_versions(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, episode_id)
);
CREATE TABLE IF NOT EXISTS customer_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL DEFAULT '',
  query_json TEXT NOT NULL DEFAULT '{}',
  definition_revision INTEGER NOT NULL DEFAULT 1,
  origin_route TEXT NOT NULL DEFAULT '',
  event_types TEXT NOT NULL DEFAULT '[]',
  delivery_json TEXT NOT NULL DEFAULT '{}',
  lifecycle TEXT NOT NULL DEFAULT 'active_local'
    CHECK (lifecycle IN ('active_local', 'paused', 'stopped')),
  revision INTEGER NOT NULL DEFAULT 1,
  baseline_json TEXT NOT NULL DEFAULT '{}',
  last_attempted_at TEXT,
  last_successful_at TEXT,
  coverage_state TEXT NOT NULL DEFAULT 'unknown',
  monitor_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  alert_id INTEGER NOT NULL REFERENCES customer_alerts(id),
  definition_revision INTEGER NOT NULL DEFAULT 1,
  kind TEXT NOT NULL,
  logical_identity TEXT NOT NULL DEFAULT '',
  transition_key TEXT NOT NULL DEFAULT '',
  transition_ordinal INTEGER NOT NULL DEFAULT 0,
  before_ref TEXT,
  after_ref TEXT,
  observed_at TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  historical INTEGER NOT NULL DEFAULT 0,
  read_state INTEGER NOT NULL DEFAULT 0,
  UNIQUE (alert_id, definition_revision, kind, logical_identity, transition_key)
);
CREATE TABLE IF NOT EXISTS customer_mistake_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  ref TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'acknowledged', 'resolved')),
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placement_evidence (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  placement_id TEXT NOT NULL REFERENCES expected_placements(id),
  ref TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  attached_by TEXT NOT NULL DEFAULT 'owner',
  PRIMARY KEY (workspace_id, campaign_id, placement_id, ref)
);
CREATE TABLE IF NOT EXISTS campaign_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  report_no INTEGER NOT NULL,
  campaign_revision INTEGER NOT NULL DEFAULT 0,
  pins_json TEXT NOT NULL DEFAULT '{}',
  report_json TEXT NOT NULL DEFAULT '{}',
  export_id INTEGER REFERENCES exports(id),
  export_digest TEXT NOT NULL DEFAULT '',
  renderer_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (workspace_id, campaign_id, report_no)
);
CREATE INDEX IF NOT EXISTS idx_customer_alert_events_alert ON customer_alert_events (alert_id);
CREATE INDEX IF NOT EXISTS idx_customer_mistake_ref ON customer_mistake_reports (ref);
CREATE INDEX IF NOT EXISTS idx_placement_evidence_placement ON placement_evidence (placement_id);
CREATE INDEX IF NOT EXISTS idx_campaign_reports_campaign ON campaign_reports (campaign_id);
"""

_V6_TABLES = (
    "customer_saved_searches", "customer_show_identities",
    "customer_episode_shows", "customer_alerts", "customer_alert_events",
    "customer_mistake_reports", "placement_evidence", "campaign_reports",
)

# Schema v7: dedicated local show-artwork cache (owner-approved 2026-09-29).
# One row per customer show identity. Image bytes and their validation
# metadata commit atomically; failed attempts record status/error and
# attempt time separately and never overwrite last-good bytes with
# partial or mismatched data. No fetch ever runs from a page request;
# refresh is explicit CLI-only (scripts/app_workspace.py).
_SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS show_artwork (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  show_identity_id TEXT NOT NULL,
  source_version_id INTEGER REFERENCES source_versions(id),
  feed_url TEXT NOT NULL DEFAULT '',
  artwork_url TEXT NOT NULL DEFAULT '',
  image_bytes BLOB,
  mime TEXT NOT NULL DEFAULT '',
  width INTEGER NOT NULL DEFAULT 0,
  height INTEGER NOT NULL DEFAULT 0,
  sha256 TEXT NOT NULL DEFAULT '',
  feed_etag TEXT NOT NULL DEFAULT '',
  feed_last_modified TEXT NOT NULL DEFAULT '',
  art_etag TEXT NOT NULL DEFAULT '',
  art_last_modified TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT '',
  validated_at TEXT,
  status TEXT NOT NULL DEFAULT 'missing',
  last_error TEXT NOT NULL DEFAULT '',
  attempted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, show_identity_id)
);
CREATE INDEX IF NOT EXISTS idx_show_artwork_status ON show_artwork (status);
"""

_V7_TABLES = ("show_artwork",)

# Schema v8 (K01): retained B1–B5 resource tables plus the thin immutable
# source-state pin index. All new tables carry workspace scope and standard
# entity revisions; JSON columns hold bounded domain data only.
_SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS source_states (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  kind TEXT NOT NULL CHECK (kind IN ('evidence','episode')),
  record_id TEXT NOT NULL,
  publication_order INTEGER NOT NULL,
  pins_json TEXT NOT NULL,
  bounds_json TEXT NOT NULL,
  projection_digest TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  UNIQUE (workspace_id,kind,record_id,publication_order)
);
CREATE INDEX IF NOT EXISTS idx_source_states_record
  ON source_states(workspace_id,kind,record_id,publication_order);
CREATE TABLE IF NOT EXISTS customer_saved_items (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  type TEXT NOT NULL CHECK (type IN ('excerpt','query','bookmark')),
  title TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  target_json TEXT NOT NULL DEFAULT '{}',
  query_json TEXT NOT NULL DEFAULT '{}',
  selector_json TEXT,
  captured_json TEXT NOT NULL DEFAULT '{}',
  saved_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_items_ws_type ON customer_saved_items (workspace_id, type, saved_at, id);
CREATE TABLE IF NOT EXISTS provided_briefs (
  brief_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  head_revision INTEGER NOT NULL DEFAULT 1,
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provided_brief_versions (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  brief_id TEXT NOT NULL REFERENCES provided_briefs(brief_id),
  version INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  provided_text TEXT NOT NULL DEFAULT '',
  digest TEXT NOT NULL DEFAULT '',
  captured_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (workspace_id,brief_id,version)
);
CREATE TABLE IF NOT EXISTS campaign_targets (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  scope TEXT NOT NULL CHECK (scope IN ('episode','show_window')),
  show_id TEXT,
  episode_id TEXT,
  date_from TEXT,
  date_to TEXT,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  note TEXT NOT NULL DEFAULT '',
  source_scope_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_evidence (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  target_id TEXT NOT NULL REFERENCES campaign_targets(id),
  ref TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  attached_by TEXT NOT NULL DEFAULT 'owner',
  PRIMARY KEY (workspace_id, target_id, ref)
);
CREATE TABLE IF NOT EXISTS p01p04_holds (
  artifact_id TEXT NOT NULL, job_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  PRIMARY KEY (artifact_id,job_id)
);
CREATE TABLE IF NOT EXISTS p01p04_retention (
  artifact_id TEXT PRIMARY KEY, retain_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_conversion_lineage (
  search_id INTEGER PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  action TEXT NOT NULL CHECK (action IN ('converted','dropped','quarantined','qualified')),
  item_id TEXT,
  original_json TEXT NOT NULL DEFAULT '{}',
  notes_json TEXT NOT NULL DEFAULT '[]',
  definition_key TEXT UNIQUE,
  definition_digest TEXT NOT NULL DEFAULT '',
  canonical_json TEXT NOT NULL DEFAULT '{}',
  membership_json TEXT NOT NULL DEFAULT '{}',
  actor TEXT NOT NULL DEFAULT '',
  converted_at TEXT NOT NULL
);
"""

_V8_TABLES = (
    "source_states", "customer_saved_items", "provided_briefs",
    "provided_brief_versions", "campaign_targets", "target_evidence",
    "p01p04_holds", "p01p04_retention", "customer_conversion_lineage",
)

# v8 column extensions on existing tables (applied idempotently; legacy
# rows keep neutral defaults and are never reinterpreted as new semantics).
_V8_COLUMNS = {
    "campaigns": {
        "mode": "TEXT CHECK (mode IN ('research','supplied_requirements'))",
        "brief_ref_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "placement_evidence": {
        "binding": "TEXT NOT NULL DEFAULT 'eligible' CHECK (binding IN ('eligible','citation_only'))",
    },
    "customer_alerts": {
        "query_version": "INTEGER NOT NULL DEFAULT 1",
        "live_selector_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_attempt_result": "TEXT NOT NULL DEFAULT ''",
        "last_error": "TEXT NOT NULL DEFAULT ''",
        "resource_revision": "INTEGER NOT NULL DEFAULT 1",
    },
    "customer_alert_events": {
        "source_event_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "customer_mistake_reports": {
        "created_by": "TEXT NOT NULL DEFAULT ''",
    },
    "episode_history": {
        "kind": "TEXT",
        "alias_of": "TEXT",
        "media_artifact_id": "TEXT",
    },
    "exports": {
        "workspace_id": "TEXT NOT NULL DEFAULT 'local'",
        "title": "TEXT NOT NULL DEFAULT ''",
        "digest": "TEXT NOT NULL DEFAULT ''",
        "count_units": "TEXT NOT NULL DEFAULT ''",
        "captured_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}

# Artwork fetch bounds shared by the refresh command and its tests.
# The feed cap bounds only the streamed feed fetch; image limits are separate.
ARTWORK_MAX_FEED_BYTES = 20_000_000
ARTWORK_MAX_IMAGE_BYTES = 1_000_000
ARTWORK_MAX_DIM = 1400
ARTWORK_MIME_ALLOW = ("image/jpeg", "image/png", "image/webp")


def artwork_sniff_mime(data) -> str | None:
    """Image type from its magic bytes: JPEG, PNG or WebP, else None.

    The Content-Type header never decides: SVG, HTML, XML and anything
    else are unknown here, whatever a server or a stored row claims.
    """
    if not data:
        return None
    head = bytes(data[:12])
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(head) == 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None

# Local default source content policy applied at backfill: research metadata,
# excerpts and transcripts are agent-disclosable and export-eligible; audio
# is never redistributed to agents and never bundled in exports (§9).
_POLICY_DEFAULTS = (
    ("metadata", "allow", 1),
    ("excerpt", "allow", 1),
    ("transcript", "allow", 1),
    ("audio", "deny", 0),
)


def utcnow() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


class _Connection(sqlite3.Connection):
    """A workspace connection: a plain sqlite3 connection that can carry the
    table-name memo of _table_names."""


def _table_names(con: sqlite3.Connection) -> frozenset:
    """Names of this database's tables. Workspace connections memo them by
    the schema cookie (PRAGMA schema_version moves on every schema change,
    and back on rollback), so a load asking per record reads sqlite_master
    once, not once per record."""
    version = con.execute("PRAGMA schema_version").fetchone()[0]
    memo = getattr(con, "_ew_tables", None)
    if memo is not None and memo[0] == version:
        return memo[1]
    names = frozenset(r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    if isinstance(con, _Connection):
        con._ew_tables = (version, names)
    return names


def connect(workspace: Path) -> sqlite3.Connection:
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    db_path = workspace / "workspace.sqlite3"
    con = sqlite3.connect(str(db_path), timeout=15.0, isolation_level=None, factory=_Connection)
    db_path.chmod(0o600)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA busy_timeout=15000;")
    return con


_savepoint_ids = _itertools_count()


@contextmanager
def transaction(con, *, write):
    """Caller-owned transaction primitive (K01).

    The outer owner begins/commits/rolls back; nested operations use
    controlled savepoints and never commit the caller. Commit exceptions
    enter the same rollback path as body exceptions.
    """
    nested = con.in_transaction
    name = f"ew_{next(_savepoint_ids)}" if nested else None
    con.execute(f"SAVEPOINT {name}" if nested else
                ("BEGIN IMMEDIATE" if write else "BEGIN"))
    try:
        yield con
        if nested:
            con.execute(f"RELEASE {name}")
        else:
            con.commit()
    except BaseException:
        if nested:
            con.execute(f"ROLLBACK TO {name}")
            con.execute(f"RELEASE {name}")
        else:
            con.rollback()
        raise


def atomic(fn):
    """Lock before reading revisions; roll back mutation and audit together."""
    @wraps(fn)
    def run(con, *args, **kwargs):
        with transaction(con, write=True):
            return fn(con, *args, **kwargs)
    return run


def open_existing(path, *, readonly):
    """Open only an existing verified DB; create nothing, initialize nothing.

    Both handles connect through a no-create SQLite URI: mode=ro for
    reads, mode=rw for mutations. The pre-connect existence check is a
    fast refusal only, never the no-create guarantee: a file that moves
    between validation and connect fails the connect itself instead of
    materializing an empty replacement. Mutation handles additionally
    use explicit autocommit plus the transaction primitive. Both require
    the current schema before business dispatch.
    """
    from pathlib import Path as _Path
    target = _Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"workspace database does not exist: {target}")
    exact = target.resolve()
    # as_uri() percent-encodes the exact path so directory names
    # containing '#', '?' or similar never change URI meaning; the
    # opened file is verified against PRAGMA database_list below.
    uri = exact.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=15.0,
                              isolation_level=None, factory=_Connection)
    except sqlite3.OperationalError as exc:
        raise FileNotFoundError(
            f"workspace database does not exist: {target}") from exc
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON;")
        con.execute("PRAGMA busy_timeout=15000;")
        require_current_schema(con)
        opened = con.execute("PRAGMA database_list").fetchone()
        if Path(opened[2]).resolve() != exact:
            raise RuntimeError(
                f"admission opened an unexpected database: {opened[2]}")
    except Exception:
        con.close()
        raise
    return con

@contextmanager
def workspace_lock(workspace, shared=False, name="workspace.lock"):
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (workspace / name).open("a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("workspace is in use; stop its server before restoring")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _schema_version(con: sqlite3.Connection) -> int:
    existing = con.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
    if not existing:
        return 0
    return con.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]


def _add_missing_columns(con: sqlite3.Connection, additions: dict) -> None:
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table, columns in additions.items():
            existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _rebuild_relations(con: sqlite3.Connection, base_schema: str, additions: dict) -> None:
    # SQLite cannot add REFERENCES to existing columns. Rebuild only the
    # affected relations, preserving every column and row inside one transaction.
    relations = ("episodes", "evidence", "reviews", "brand_assocs", "campaign_evidence", "campaign_checks")
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(base_schema)
        for table, columns in additions.items():
            existing = {r[1] for r in expected.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    expected.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        deficient = [table for table in relations
                     if not {tuple(r)[2:] for r in expected.execute(f"PRAGMA foreign_key_list({table})")}
                     <= {tuple(r)[2:] for r in con.execute(f"PRAGMA foreign_key_list({table})")}]
    finally:
        expected.close()
    if deficient:
        con.execute("PRAGMA foreign_keys=OFF")
        try:
            with con:
                con.execute("BEGIN IMMEDIATE")
                for table in relations:
                    statement = next(s.strip() for s in base_schema.split(";")
                                     if s.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table} ("))
                    temporary = table + "_migration"
                    con.execute(statement.replace(f"IF NOT EXISTS {table}", temporary, 1))
                    original_columns = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]
                    new_columns = {r["name"] for r in con.execute(f"PRAGMA table_info({temporary})")}
                    for name, declaration in additions.get(table, {}).items():
                        if name not in new_columns:
                            con.execute(f"ALTER TABLE {temporary} ADD COLUMN {name} {declaration}")
                    names = ",".join(original_columns)
                    con.execute(f"INSERT INTO {temporary} ({names}) SELECT {names} FROM {table}")
                    con.execute(f"DROP TABLE {table}")
                    con.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
                if con.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("existing workspace has orphaned records; migration rolled back")
        finally:
            con.execute("PRAGMA foreign_keys=ON")
        for statement in base_schema.split(";"):
            if statement.strip().startswith("CREATE INDEX"):
                con.execute(statement)


def _backfill_v1(con: sqlite3.Connection, version: int) -> None:
    if version == 1:
        con.execute("UPDATE evidence SET transcript_version_id="
                    "(SELECT transcript_version_id FROM episodes WHERE id=evidence.episode_id),"
                    " source_record_key=id, assoc_revision=COALESCE("
                    "(SELECT MAX(revision) FROM brand_assocs WHERE evidence_id=evidence.id),0)")


# Auxiliary tables created idempotently (init_schema, upgrade, restore) and never
# required of an older workspace or backup. The corpus tables (customer-ad side
# tables, transcript index, research data token, with their FTS5 shadow tables)
# are derived: restore builds them from the restored source versions.
_FTS_SHADOWS = ("config", "data", "docsize", "idx")
OPTIONAL_TABLES = ("evidence_source_membership", "p01_job_profiles",
                   "ad_reads", "ad_brand_aliases", "ad_read_links", "ad_clips",
                   "episode_screening", "ad_reads_meta", "research_state",
                   "transcript_segments", "transcript_index_versions", "evidence_lineage",
                   "evidence_review_order", "ad_rules", "ad_rules_removed", "ad_labels", "ad_reports", "ad_show_ranks",
                   "ad_fallthrough", "ad_label_undo", "billing_trials",
                   # Ask (a backup made before them restores; delete_ask_citing
                   # and the Ask routes treat a missing table as no Ask rows).
                   "ask_threads", "ask_turns", "ask_passages", "ask_reports",
                   # Meaning alerts (meaning_schema; delete_citing and the
                   # routes treat a missing table as no rows).
                   "meaning_alerts", "meaning_runs", "meaning_matches",
                   # Judge decisions on a placement's episode reads (FUNC-2; the
                   # verdict read path treats a missing table as no decisions).
                   "placement_episode_reviews",
                   *(f"{fts}{suffix}" for fts in ("ad_reads_fts", "transcript_segments_fts")
                     for suffix in ("", *(f"_{s}" for s in _FTS_SHADOWS))))


def ensure_evidence_membership(con: sqlite3.Connection) -> None:
    """Which source versions an unchanged importer row also belongs to (member=1),
    or was retired from as a duplicate (member=0); the row's own pin never moves."""
    con.execute("CREATE TABLE IF NOT EXISTS evidence_source_membership ("
                " evidence_id TEXT NOT NULL REFERENCES evidence(id),"
                " source_version_id INTEGER NOT NULL REFERENCES source_versions(id),"
                " member INTEGER NOT NULL, PRIMARY KEY (evidence_id, source_version_id))")
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    ensure_view_state_guards(con)


# -- Ask (Meta AI features, ASK-CONTRACT section 4) ---------------------------
#
# One thread per question chain, asked by one member (asker only); each turn
# keeps the passages it was given (cite-or-drop) and its spend. Retention is
# the workspace's. A takedown (an episode leaving the load) deletes every turn
# that cites it and every later turn of its thread (delete_ask_citing).

ASK_TABLES = ("ask_threads", "ask_turns", "ask_passages", "ask_reports")
ASK_TURN_STATES = ("reserving", "working", "answered", "refused_budget", "failed", "cancelled")
_ASK_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS ask_threads ("
    " id TEXT PRIMARY KEY, asker_principal TEXT NOT NULL, created_at TEXT NOT NULL,"
    " rights_level TEXT NOT NULL, record_revision TEXT NOT NULL,"
    " addendum_revision TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS ask_turns ("
    " id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, seq INTEGER NOT NULL,"
    " question TEXT NOT NULL, prepared TEXT,"
    " state TEXT NOT NULL CHECK (state IN ('reserving', 'working', 'answered',"
    " 'refused_budget', 'failed', 'cancelled')),"
    " step INTEGER NOT NULL DEFAULT 0, outcome TEXT,"
    " answer_json TEXT CHECK (answer_json IS NULL OR json_valid(answer_json)),"
    " coverage_json TEXT CHECK (coverage_json IS NULL OR json_valid(coverage_json)),"
    " dropped INTEGER NOT NULL DEFAULT 0, searches INTEGER NOT NULL DEFAULT 0,"
    " passages_read INTEGER NOT NULL DEFAULT 0, model TEXT NOT NULL,"
    " usd_reserved REAL NOT NULL DEFAULT 0, usd_settled REAL, reservation_id TEXT,"
    " error_code TEXT, cancel INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
    " finished_at TEXT, UNIQUE (thread_id, seq))",
    "CREATE TABLE IF NOT EXISTS ask_passages ("
    " turn_id TEXT NOT NULL, pid TEXT NOT NULL, kind TEXT NOT NULL,"
    " in_ad INTEGER NOT NULL, held INTEGER NOT NULL,"
    " brands_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(brands_json)),"
    " brand_channel TEXT, episode_id TEXT NOT NULL, version_id TEXT, evidence_id TEXT,"
    " evidence_revision INTEGER, start_s REAL NOT NULL, end_s REAL NOT NULL,"
    " show TEXT NOT NULL, episode_title TEXT NOT NULL, pub_date TEXT NOT NULL,"
    " fetch_date TEXT NOT NULL, fetch_source TEXT NOT NULL,"
    " disclosure_level TEXT NOT NULL, text TEXT NOT NULL,"
    " PRIMARY KEY (turn_id, pid))",
    "CREATE INDEX IF NOT EXISTS idx_ask_passages_episode ON ask_passages (episode_id)",
    "CREATE TABLE IF NOT EXISTS ask_reports ("
    " id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, reporter_principal TEXT NOT NULL,"
    " reason TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_ask_reports_turn ON ask_reports (turn_id)",
)


def ensure_ask_schema(con: sqlite3.Connection) -> None:
    """Create the Ask tables and the meaning-alert tables (additive; part of
    the pinned workspace schema). The restore path (app_workspace restore)
    calls this one function, so both model features' tables come back with
    a backup made before them."""
    for statement in _ASK_SCHEMA:
        con.execute(statement)
    from . import meaning_schema
    meaning_schema.ensure_schema(con)  # ends with ensure_view_state_guards


def ensure_episode_review_schema(con: sqlite3.Connection) -> None:
    """Judge decisions on one episode's reads for one placement (FUNC-2
    Addendum A). Append-only: every confirm, reject and undo is a row; the
    latest row per read is its decision (undo clears it), and the episode's
    review revision is its highest row revision."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS placement_episode_reviews ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " workspace_id TEXT NOT NULL DEFAULT 'local',"
        " campaign_id INTEGER NOT NULL, placement_id TEXT NOT NULL,"
        " episode_id TEXT NOT NULL, evidence_id TEXT NOT NULL,"
        " decision TEXT NOT NULL CHECK (decision IN ('confirm', 'reject', 'undo')),"
        " code TEXT, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,"
        " revision INTEGER NOT NULL,"
        " UNIQUE (workspace_id, campaign_id, placement_id, episode_id, revision))")
    # Last: a new or rebuilt guarded table gets its view-state guards, or
    # view_state_key stays None and no research read is reused (UPD3-SLOW).
    ensure_view_state_guards(con)


def delete_ask_citing(con: sqlite3.Connection, episode_ids) -> int:
    """Takedown (chat doc S4/S5, base design 8.4), in the caller's
    transaction: delete every Ask passage that cites one of `episode_ids`,
    every turn that had one, every later turn of the same thread (a
    follow-up carries the earlier turns' text), the passages and reports of
    those turns, and the threads this leaves with no turn. Returns the
    number of turns deleted; 0 when the list is empty or the workspace has
    no Ask tables. With only some of the tables it deletes what they hold
    (a backup made before them restores without them)."""
    ids = sorted({str(e) for e in episode_ids})
    present = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)"
        % ",".join("?" * len(ASK_TABLES)), ASK_TABLES)}
    if not ids or "ask_passages" not in present:
        return 0
    removed = json.dumps(ids)
    if "ask_turns" not in present:
        con.execute("DELETE FROM ask_passages WHERE episode_id IN"
                    " (SELECT value FROM json_each(?))", (removed,))
        return 0
    doomed = con.execute(
        "WITH hit AS (SELECT t.thread_id, MIN(t.seq) AS seq FROM ask_turns t"
        " JOIN ask_passages p ON p.turn_id = t.id"
        " WHERE p.episode_id IN (SELECT value FROM json_each(?))"
        " GROUP BY t.thread_id)"
        " SELECT t.id, t.thread_id FROM ask_turns t"
        " JOIN hit ON hit.thread_id = t.thread_id AND t.seq >= hit.seq",
        (removed,)).fetchall()
    turns = json.dumps([r[0] for r in doomed])
    threads = json.dumps(sorted({r[1] for r in doomed}))
    con.execute("DELETE FROM ask_passages WHERE episode_id IN (SELECT value FROM json_each(?))"
                " OR turn_id IN (SELECT value FROM json_each(?))", (removed, turns))
    if "ask_reports" in present:
        con.execute("DELETE FROM ask_reports WHERE turn_id IN"
                    " (SELECT value FROM json_each(?))", (turns,))
    con.execute("DELETE FROM ask_turns WHERE id IN (SELECT value FROM json_each(?))", (turns,))
    if "ask_threads" in present:
        con.execute("DELETE FROM ask_threads WHERE id IN (SELECT value FROM json_each(?))"
                    " AND NOT EXISTS (SELECT 1 FROM ask_turns t"
                    " WHERE t.thread_id = ask_threads.id)", (threads,))
    return len(doomed)


def has_evidence_membership(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                       " AND name='evidence_source_membership'").fetchone() is not None


def init_schema(con: sqlite3.Connection) -> None:
    version = _schema_version(con)
    if version > SCHEMA_VERSION:
        raise RuntimeError("workspace requires a newer application")
    if 0 < version < SCHEMA_VERSION:
        try:
            path = Path(con.execute("PRAGMA database_list").fetchone()[2])
        except (sqlite3.Error, IndexError, TypeError):
            path = None
        if path is not None and str(path) not in ("", ":memory:"):
            backup_path = path.with_name(f"before-schema-{version}.sqlite3")
            if not backup_path.exists():
                backup = sqlite3.connect(str(backup_path))
                try:
                    con.backup(backup)
                finally:
                    backup.close()
                backup_path.chmod(0o600)
    con.executescript(_SCHEMA_V3)
    _add_missing_columns(con, _V3_COLUMNS)
    with con:
        con.execute("BEGIN IMMEDIATE")
        _backfill_v1(con, version)
    merged = {table: {**_V3_COLUMNS.get(table, {}), **_V4_COLUMNS.get(table, {})}
              for table in set(_V3_COLUMNS) | set(_V4_COLUMNS)}
    _rebuild_relations(con, _SCHEMA_V3, merged)
    if _schema_version(con) < 4:
        migrate_up(con)
    if _schema_version(con) < 5:
        migrate_4_to_5(con)
    if _schema_version(con) < 6:
        migrate_5_to_6(con)
    if _schema_version(con) < 7:
        migrate_6_to_7(con)
    if _schema_version(con) < 8:
        migrate_7_to_8(con)
    _add_missing_columns(con, _V8_COLUMNS)
    ensure_processing_stage_key(con)
    ensure_alert_monitor_column(con)
    ensure_workspace_identity(con)
    from . import collaboration as _collab
    _collab.setup_aux_schema(con)
    from . import coverage as _coverage
    _coverage.ensure_attestation_schema(con)
    from . import observations
    observations.ensure_confirmation_schema(con)
    from . import creatives
    creatives.setup_aux_schema(con)
    from earwitness.pipeline import jobs, artifacts
    jobs.ensure_tables(con)
    artifacts.ensure_tables(con)
    from . import monitoring, indexing
    monitoring.ensure_aux(con)
    from . import billing
    billing.ensure_billing_schema(con)
    indexing.ensure_schema(con)
    from . import ad_reads, lineage, transcript_index
    ad_reads.ensure_schema(con)
    transcript_index.ensure_schema(con)
    # The seed path's lineage tables (one active row per stable record key),
    # so a corpus load into a pinned pilot workspace needs no runtime DDL.
    lineage.ensure_schema(con)
    con.execute("CREATE TABLE IF NOT EXISTS p09_feed_sources ("
                " id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL DEFAULT 'local',"
                " url TEXT NOT NULL, host TEXT NOT NULL, created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS p09_run_episodes ("
                " run_id TEXT NOT NULL, job_id TEXT NOT NULL, episode_id TEXT NOT NULL,"
                " PRIMARY KEY (run_id, job_id))")
    ensure_evidence_membership(con)
    ensure_ask_schema(con)
    ensure_episode_review_schema(con)
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (SCHEMA_VERSION, utcnow()))
    if con.execute("SELECT value FROM meta WHERE key='aggregate_rev'").fetchone() is None:
        con.execute("INSERT INTO meta (key, value) VALUES ('aggregate_rev', '0')")
    # FTS5 full-text index over evidence search text; literal LIKE fallback otherwise.
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5"
                    "(evidence_id UNINDEXED, text)")
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('fts_available', '1')")
    except sqlite3.OperationalError:
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('fts_available', '0')")
    # Last: every table above (including rebuilt ones) gets its guards and
    # the workspace a fresh research view state token.
    ensure_view_state_guards(con)


def migrate_up(con: sqlite3.Connection) -> None:
    """Additive-only v3 -> v4 migration. Safe to re-run (idempotent)."""
    con.executescript(_SCHEMA_V4)
    _add_missing_columns(con, _V4_COLUMNS)
    ensure_processing_stage_key(con)
    with con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE evidence SET record_kind='candidate' WHERE record_kind IS NULL")
        bad = con.execute(
            "SELECT COUNT(*) FROM evidence WHERE record_kind NOT IN ('candidate','observation')").fetchone()[0]
        if bad:
            raise RuntimeError("evidence.record_kind holds unexpected values; migration rolled back")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("existing workspace has orphaned records; migration rolled back")
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (4, utcnow()))
    # Keep a MAX(version) reader compatible: schema_version holds one row per
    # applied version; v4 workspaces therefore contain rows for 3 and 4.
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (3, utcnow()))


_ALERT_MONITOR_COLUMNS = {
    "customer_alerts": {
        "monitor_json": "TEXT NOT NULL DEFAULT '{}'",
    },
}


def ensure_alert_monitor_column(con: sqlite3.Connection) -> None:
    """Idempotent additive migration: monitor_json on customer_alerts.

    Safe to re-run; existing rows keep '{}' (manual alerts never
    silently become schedules). Called from init_schema.
    """
    if con.in_transaction:
        con.commit()
    _add_missing_columns(con, _ALERT_MONITOR_COLUMNS)


def ensure_processing_stage_key(con: sqlite3.Connection) -> None:
    """Idempotent additive migration: nullable stage_key on processing_jobs.

    Safe to re-run; existing rows keep NULL (legacy rows match nothing in
    the P01 reuse path). Called from init_schema and migrate_up.
    """
    if con.in_transaction:
        # Prior bookkeeping INSERTs may hold an implicit transaction under
        # legacy isolation; they are idempotent, so settle them first.
        con.commit()
    _add_missing_columns(con, _STAGE_KEY_COLUMNS)


def drop_processing_stage_key(con: sqlite3.Connection) -> None:
    """Reversible down-migration: drop the stage_key column again.

    Refuses when populated rows exist so derived reuse keys are never
    silently discarded; re-run ensure_processing_stage_key to restore.
    """
    cols = {r["name"] for r in con.execute("PRAGMA table_info(processing_jobs)")}
    if "stage_key" not in cols:
        return
    populated = con.execute(
        "SELECT COUNT(*) FROM processing_jobs WHERE stage_key IS NOT NULL").fetchone()[0]
    if populated:
        raise RuntimeError("cannot drop stage_key with populated reuse keys present")
    con.execute("ALTER TABLE processing_jobs DROP COLUMN stage_key")


def migrate_down(con: sqlite3.Connection) -> None:
    """Reversible v4 -> v3 down-migration. Drops only v4 extension tables/columns.

    Refuses to run when v4 extension tables hold rows, so no P01-P13 data is
    silently discarded. Refuses when v5 agent tables are present at all: run
    migrate_5_to_4 first so history/policy state is handled explicitly.
    Restores schema_version to 3 afterwards.
    """
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    lingering7 = [t for t in _V7_TABLES if t in tables]
    if lingering7:
        raise RuntimeError(f"cannot downgrade to v3 with v7 artwork tables present: {lingering7};"
                           " run migrate_7_to_6 first")
    lingering6 = [t for t in _V6_TABLES if t in tables]
    if lingering6:
        raise RuntimeError(f"cannot downgrade to v3 with v6 customer tables present: {lingering6};"
                           " run migrate_6_to_5 first")
    lingering = [t for t in _V5_TABLES if t in tables]
    if lingering:
        raise RuntimeError(f"cannot downgrade to v3 with v5 agent tables present: {lingering};"
                           " run migrate_5_to_4 first")
    with con:
        con.execute("BEGIN IMMEDIATE")
        holding = [t for t in _V4_TABLES
                   if con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0]
        if holding:
            raise RuntimeError(f"cannot downgrade with v4 extension data present: {holding}")
        linked = con.execute(
            "SELECT COUNT(*) FROM evidence WHERE observation_revision_id IS NOT NULL").fetchone()[0]
        if linked:
            raise RuntimeError("cannot downgrade with evidence linked to observation revisions")
        media_used = con.execute(
            "SELECT COUNT(*) FROM episodes WHERE media_artifact_id IS NOT NULL").fetchone()[0]
        if media_used:
            raise RuntimeError("cannot downgrade with episodes linked to media artifacts")
    con.execute("PRAGMA foreign_keys=OFF")
    # RENAME TABLE otherwise rewrites incoming FK references to the
    # temporary name; keep references pointed at the canonical table.
    con.execute("PRAGMA legacy_alter_table=ON")
    try:
        with con:
            con.execute("BEGIN IMMEDIATE")
            for table in _V4_TABLES:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            for table, columns in _V4_COLUMNS.items():
                kept = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")
                        if r["name"] not in columns]
                names = ",".join(kept)
                con.execute(f"ALTER TABLE {table} RENAME TO {table}_downgrade")
                statement = next(s.strip() for s in _SCHEMA_V3.split(";")
                                 if s.strip().startswith(f"CREATE TABLE IF NOT EXISTS {table} ("))
                con.execute(statement)
                for name, declaration in _V3_COLUMNS.get(table, {}).items():
                    if name not in {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                con.execute(f"INSERT INTO {table} ({names}) SELECT {names} FROM {table}_downgrade")
                con.execute(f"DROP TABLE {table}_downgrade")
            con.execute("DELETE FROM schema_version WHERE version >= 4")
            con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (3, utcnow()))
            if con.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("downgraded workspace has orphaned records; migration rolled back")
    finally:
        con.execute("PRAGMA legacy_alter_table=OFF")
        con.execute("PRAGMA foreign_keys=ON")
    for statement in _SCHEMA_V3.split(";"):
        if statement.strip().startswith("CREATE INDEX"):
            con.execute(statement)


def migrate_4_to_5(con: sqlite3.Connection) -> None:
    """Additive-only v4 -> v5 agent history/policy migration. Safe to re-run."""
    con.executescript(_SCHEMA_V5)
    ensure_workspace_identity(con)
    con.commit()
    with con:
        con.execute("BEGIN IMMEDIATE")
        backfill_history(con)
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("existing workspace has orphaned records; migration rolled back")
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (5, utcnow()))
    # Keep a MAX(version) reader compatible: v5 workspaces hold rows for 3, 4, 5.
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (4, utcnow()))
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (3, utcnow()))


def migrate_5_to_6(con: sqlite3.Connection) -> None:
    """Additive-only v5 -> v6 customer migration. Safe to re-run.

    Creates the customer tables; preserves every existing row, the
    established agent profile, and workspace identity. Never widens
    permissions: no meta.agent_profile write happens here.
    """
    if con.in_transaction:
        # Legacy-isolation callers may hold an implicit transaction from
        # prior bookkeeping INSERTs; settle it before the DDL batch.
        con.commit()
    con.executescript(_SCHEMA_V6)
    con.commit()
    with con:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("existing workspace has orphaned records; migration rolled back")
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (6, utcnow()))
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (5, utcnow()))
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (4, utcnow()))
    con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (3, utcnow()))


def migrate_6_to_7(con: sqlite3.Connection) -> None:
    """Additive-only v6 -> v7 artwork-cache migration. Safe to re-run.

    One transaction covers the table/index DDL, the foreign-key check,
    and every schema-version marker: any DDL, integrity, foreign-key,
    or marker failure rolls the whole v7 migration back, leaving the
    v6 schema and version intact. Creates only the empty show_artwork
    cache; preserves every existing row, review state, source-version
    association, and metadata. Never fetches: page loads and migration
    perform no network I/O.
    """
    if con.in_transaction:
        # Legacy-isolation callers may hold an implicit transaction from
        # prior bookkeeping INSERTs; settle it before the DDL batch.
        con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        # executescript would implicitly commit between the DDL and the
        # version markers, so each DDL statement runs inside this
        # transaction instead.
        for stmt in _SCHEMA_V7.split(";"):
            if stmt.strip():
                con.execute(stmt)
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("existing workspace has orphaned records; migration rolled back")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (7, utcnow()))
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (6, utcnow()))
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (5, utcnow()))
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (4, utcnow()))
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (3, utcnow()))
        con.commit()
    except Exception:
        con.rollback()
        raise


def migrate_7_to_6(con: sqlite3.Connection) -> None:
    """Reversible v7 -> v6 down-migration. Drops only the artwork cache.

    Refuses when validated artwork bytes exist, so fetched images are
    never silently discarded (re-run the explicit refresh after
    re-upgrade instead). Failure-status rows are regenerable and drop
    with the table. Restores schema_version to 6.
    """
    if con.in_transaction:
        con.commit()
    with con:
        con.execute("BEGIN IMMEDIATE")
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "show_artwork" in tables and con.execute(
                "SELECT COUNT(*) FROM show_artwork WHERE status='ok'").fetchone()[0]:
            raise RuntimeError("cannot downgrade with validated artwork bytes present:"
                               " clear the cache explicitly first")
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table in _V7_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DELETE FROM schema_version WHERE version >= 7")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (6, utcnow()))
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("downgraded workspace has orphaned records; migration rolled back")


def has_artwork_table(con: sqlite3.Connection) -> bool:
    """True when the v7 artwork cache table exists on this workspace."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return all(t in tables for t in _V7_TABLES)


def _detach_retained_live_keys(con):
    """Retained history and declared bindings outlive their live source rows."""
    definitions = {
        "evidence_history": "evidence", "review_history": "evidence",
        "association_history": "evidence", "episode_history": "episodes",
        "campaign_evidence": "evidence", "campaign_checks": "evidence",
        "expected_placements": "episodes", "campaign_targets": "episodes"}
    for table, parent in definitions.items():
        if not any(row[2] == parent for row in con.execute(f"PRAGMA foreign_key_list({table})")):
            continue
        original = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone()[0]
        indexes = [row[0] for row in con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,))]
        retained = original.replace(f" REFERENCES {parent}(id)", "")
        temporary = "retained_" + table
        opener = retained.index("(")
        con.execute(f"CREATE TABLE {temporary} " + retained[opener:])
        columns = ",".join(row[1] for row in con.execute(f"PRAGMA table_info({table})"))
        con.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
        con.execute(f"DROP TABLE {table}")
        con.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
        for index in indexes:
            con.execute(index)


def migrate_7_to_8(con: sqlite3.Connection) -> None:
    """Atomic v7 -> v8 migration for retained B1–B5 resources (K01).

    One transaction covers all new table/index DDL, v8 column additions,
    the foreign-key check and every schema-version marker: any DDL,
    integrity, foreign-key or marker failure rolls the whole migration
    back, leaving the v6/v7 schema and version intact. Creates only empty
    resource tables; preserves every existing row, review state,
    source-version association and metadata. Legacy campaign rows keep
    mode NULL (unresolved legacy state, never a fabricated default).

    Caller-transaction ownership is explicit: an open caller transaction
    refuses the upgrade instead of committing unrelated sentinel writes.
    Commit it or roll it back first, then re-run the upgrade.
    """
    if con.in_transaction:
        raise RuntimeError("schema upgrade requires no open caller transaction;"
                           " commit or roll back first")
    with transaction(con, write=True):
        _detach_retained_live_keys(con)
        for stmt in _SCHEMA_V8.split(";"):
            if stmt.strip():
                con.execute(stmt)
        for table, columns in _V8_COLUMNS.items():
            existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        from . import creatives
        creatives.setup_aux_schema(con)
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("existing workspace has orphaned records; migration rolled back")
        for version in (8, 7, 6, 5, 4, 3):
            con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (version, utcnow()))
        if _schema_version(con) != 8:
            raise RuntimeError("v8 migration version verification failed; migration rolled back")


def migrate_8_to_7(con: sqlite3.Connection) -> None:
    """Down-rehearsal for v8 -> v7. Refuses loss of populated new data.

    Restoration from a private consistent backup is the owner rollback
    path; this rehearsal only drops empty v8 tables/columns. Any row in
    a v8 resource table, or any non-default v8 column value on an
    existing table, refuses the downgrade instead of silently discarding
    retained customer state.
    """
    if con.in_transaction:
        con.commit()
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for table in _V8_TABLES:
        if table in tables and con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
            raise RuntimeError(f"cannot downgrade with populated v8 table {table} present:"
                               " restore from a private consistent backup instead")
    # Neutral migration defaults per v8 column: only a value that differs
    # from the backfilled default indicates real v8 authorship worth
    # refusing to discard. Freshly migrated rows always match neutral.
    _V8_COLUMN_NEUTRAL = {
        ("campaigns", "mode"): "mode IS NULL",
        ("campaigns", "brief_ref_json"): "brief_ref_json IN ('{}')",
        ("placement_evidence", "binding"): "binding IN ('eligible')",
        ("customer_alerts", "query_version"): "query_version IN (1)",
        ("customer_alerts", "live_selector_json"): "live_selector_json IN ('{}')",
        ("customer_alerts", "last_attempt_result"): "last_attempt_result IN ('')",
        ("customer_alerts", "last_error"): "last_error IN ('')",
        ("customer_alerts", "resource_revision"): "resource_revision IN (1)",
        ("customer_alert_events", "source_event_json"): "source_event_json IN ('{}')",
        ("exports", "workspace_id"): "workspace_id IN ('local')",
        ("exports", "title"): "title IN ('')",
        ("exports", "digest"): "digest IN ('')",
        ("exports", "count_units"): "count_units IN ('')",
        ("exports", "captured_json"): "captured_json IN ('{}')",
    }
    for table, columns in _V8_COLUMNS.items():
        if table not in tables:
            continue
        existing = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for name in columns:
            if name not in existing:
                continue
            neutral = _V8_COLUMN_NEUTRAL.get((table, name))
            if neutral is None:
                continue
            try:
                filled = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE NOT ({neutral})").fetchone()[0]
            except sqlite3.Error:
                filled = 0
            if filled:
                raise RuntimeError(f"cannot downgrade with populated v8 column {table}.{name} present:"
                                   " restore from a private consistent backup instead")
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table in _V8_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DELETE FROM schema_version WHERE version >= 8")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (7, utcnow()))
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("downgraded workspace has orphaned records; migration rolled back")


def has_v8_tables(con: sqlite3.Connection) -> bool:
    """True when the v8 retained-resource tables exist on this workspace."""
    tables = _table_names(con)
    return all(t in tables for t in _V8_TABLES)


def upsert_show_artwork(con: sqlite3.Connection, show_id: str, fields: dict) -> None:
    """Atomically replace one show's cache row (bytes + all metadata).

    Must be called with no network I/O in flight: callers fetch and
    validate first, then commit here. An interrupted or rejected fetch
    must call record_artwork_attempt instead, which preserves last-good
    bytes. Commits in its own transaction when the caller holds none.
    """
    allowed = {"source_version_id", "feed_url", "artwork_url", "image_bytes",
               "mime", "width", "height", "sha256", "feed_etag",
               "feed_last_modified", "art_etag", "art_last_modified",
               "fetched_at", "validated_at", "status", "last_error",
               "attempted_at"}
    row = {k: v for k, v in (fields or {}).items() if k in allowed}
    now = utcnow()
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        prev = con.execute("SELECT created_at FROM show_artwork"
                           " WHERE workspace_id='local' AND show_identity_id=?",
                           (show_id,)).fetchone()
        created = prev["created_at"] if prev else now
        con.execute(
            "INSERT INTO show_artwork (workspace_id, show_identity_id,"
            " source_version_id, feed_url, artwork_url, image_bytes, mime,"
            " width, height, sha256, feed_etag, feed_last_modified, art_etag,"
            " art_last_modified, fetched_at, validated_at, status, last_error,"
            " attempted_at, created_at, updated_at)"
            " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?) ON CONFLICT(workspace_id, show_identity_id)"
            " DO UPDATE SET source_version_id=excluded.source_version_id,"
            " feed_url=excluded.feed_url, artwork_url=excluded.artwork_url,"
            " image_bytes=excluded.image_bytes, mime=excluded.mime,"
            " width=excluded.width, height=excluded.height,"
            " sha256=excluded.sha256, feed_etag=excluded.feed_etag,"
            " feed_last_modified=excluded.feed_last_modified,"
            " art_etag=excluded.art_etag,"
            " art_last_modified=excluded.art_last_modified,"
            " fetched_at=excluded.fetched_at, validated_at=excluded.validated_at,"
            " status=excluded.status, last_error=excluded.last_error,"
            " attempted_at=excluded.attempted_at, updated_at=excluded.updated_at",
            (show_id, row.get("source_version_id"),
             row.get("feed_url", ""), row.get("artwork_url", ""),
             row.get("image_bytes"), row.get("mime", ""),
             int(row.get("width") or 0), int(row.get("height") or 0),
             row.get("sha256", ""), row.get("feed_etag", ""),
             row.get("feed_last_modified", ""), row.get("art_etag", ""),
             row.get("art_last_modified", ""), row.get("fetched_at", ""),
             row.get("validated_at"), row.get("status", "missing"),
             row.get("last_error", ""), row.get("attempted_at"),
             created, now))
    finally:
        if own:
            con.commit()


def record_artwork_attempt(con: sqlite3.Connection, show_id: str, error: str,
                           feed_url: str = "") -> None:
    """Record a failed/rejected attempt without touching last-good bytes.

    Keeps failed-attempt time (attempted_at) separate from last
    successful validation (validated_at). Never stores partial bytes.
    """
    now = utcnow()
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        prev = con.execute("SELECT * FROM show_artwork"
                           " WHERE workspace_id='local' AND show_identity_id=?",
                           (show_id,)).fetchone()
        if prev is None:
            con.execute(
                "INSERT INTO show_artwork (workspace_id, show_identity_id,"
                " feed_url, status, last_error, attempted_at, fetched_at,"
                " created_at, updated_at) VALUES ('local', ?, ?, 'rejected',"
                " ?, ?, '', ?, ?)",
                (show_id, feed_url, error[:500], now, now, now))
        else:
            keep = dict(prev)
            con.execute("UPDATE show_artwork SET status=?, last_error=?,"
                        " attempted_at=?, updated_at=? WHERE workspace_id='local'"
                        " AND show_identity_id=?",
                        ("rejected" if not keep.get("image_bytes") else
                         keep.get("status") or "rejected",
                         error[:500], now, now, show_id))
            if feed_url:
                con.execute("UPDATE show_artwork SET feed_url=?"
                            " WHERE workspace_id='local' AND show_identity_id=?",
                            (feed_url, show_id))
    finally:
        if own:
            con.commit()


def migrate_6_to_5(con: sqlite3.Connection) -> None:
    """Reversible v6 -> v5 down-migration. Drops only v6 customer tables.

    Refuses when customer rows, placement evidence, or retained campaign
    reports exist, so no customer state is silently discarded. Restores
    schema_version to 5.
    """
    if con.in_transaction:
        con.commit()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    lingering7 = [t for t in _V7_TABLES if t in tables]
    if lingering7:
        raise RuntimeError(f"cannot downgrade to v5 with v7 artwork tables present: {lingering7};"
                           " run migrate_7_to_6 first")
    with con:
        con.execute("BEGIN IMMEDIATE")
        holding = [t for t in _V6_TABLES
                   if con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0]
        if holding:
            raise RuntimeError(f"cannot downgrade with v6 customer data present: {holding}")
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table in _V6_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DELETE FROM schema_version WHERE version >= 6")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (5, utcnow()))
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("downgraded workspace has orphaned records; migration rolled back")


def has_customer_tables(con: sqlite3.Connection) -> bool:
    """True when the v6 customer tables exist on this workspace."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return all(t in tables for t in _V6_TABLES)


def migrate_5_to_4(con: sqlite3.Connection) -> None:
    """Reversible v5 -> v4 down-migration. Drops only v5 agent tables.

    Backfilled current-state rows (history_origin='legacy_snapshot') are
    regenerable and removed with the tables. Refuses when mutation-time
    history, policy edits, or retained evaluations exist, so no real agent
    provenance is silently discarded. Restores schema_version to 4.
    """
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    lingering7 = [t for t in _V7_TABLES if t in tables]
    if lingering7:
        raise RuntimeError(f"cannot downgrade to v4 with v7 artwork tables present: {lingering7};"
                           " run migrate_7_to_6 first")
    lingering = [t for t in _V6_TABLES if t in tables]
    if lingering:
        raise RuntimeError(f"cannot downgrade to v4 with v6 customer tables present: {lingering};"
                           " run migrate_6_to_5 first")
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table in ("evidence_history", "episode_history", "review_history",
                      "association_history", "campaign_history",
                      "source_content_policy"):
            con.execute(f"DELETE FROM {table} WHERE history_origin='legacy_snapshot'")
            remaining = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if remaining:
                raise RuntimeError(f"cannot downgrade with operator/mutation v5 data present: {table}")
        if con.execute("SELECT COUNT(*) FROM check_evaluations").fetchone()[0]:
            raise RuntimeError("cannot downgrade with retained check evaluations present")
    with con:
        con.execute("BEGIN IMMEDIATE")
        for table in _V5_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DELETE FROM schema_version WHERE version >= 5")
        con.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)", (4, utcnow()))
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("downgraded workspace has orphaned records; migration rolled back")


RETIRED_SAVED_SEARCH_STORE = "customer_saved_searches"


def saved_search_store_retired(con: sqlite3.Connection) -> bool:
    """True when customer_saved_searches is absent because the customer
    conversion retired it: schema 8, the conversion marker complete and no
    definition pending. The one table a converted workspace lacks; schema
    verification and restore share this rule, judged on the database
    itself (for restore, the backup's own marker)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if RETIRED_SAVED_SEARCH_STORE in tables or _schema_version(con) != 8:
        return False
    from . import conversion
    state = conversion.status(con)
    return bool(state["marker"] and state["marker"].get("state") == "complete"
                and not state["pending_definitions"])


def verify_schema(con: sqlite3.Connection) -> dict:
    """Check v8 workspace integrity: version, tables, columns, relations."""
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript(_SCHEMA_V3)
        expected.executescript(_SCHEMA_V4)
        expected.executescript(_SCHEMA_V5)
        expected.executescript(_SCHEMA_V6)
        expected.executescript(_SCHEMA_V7)
        expected.executescript(_SCHEMA_V8)
        for table, columns in {**_V3_COLUMNS, **_V4_COLUMNS, **_V8_COLUMNS}.items():
            for name, declaration in columns.items():
                if name not in {r[1] for r in expected.execute(f"PRAGMA table_info({table})")}:
                    expected.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        required = {r[0] for r in expected.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'evidence_fts%'")}
        problems = []
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if saved_search_store_retired(con):
            required.discard(RETIRED_SAVED_SEARCH_STORE)
        for table in sorted(required):
            if table not in tables:
                problems.append(f"missing table: {table}")
                continue
            columns = {tuple(r)[1:6] for r in expected.execute(f"PRAGMA table_info({table})")}
            actual = {tuple(r)[1:6] for r in con.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                problems.append(f"incompatible columns: {table}")
        version = _schema_version(con)
        if version != SCHEMA_VERSION:
            problems.append(f"schema version {version} != {SCHEMA_VERSION}")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            problems.append("invalid references")
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            problems.append("integrity check failed")
    finally:
        expected.close()
    return {"schema_version": _schema_version(con), "problems": problems}


def table_counts(con: sqlite3.Connection) -> dict:
    counts = {}
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in sorted(tables):
        try:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = -1
    return counts


def fts_available(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT value FROM meta WHERE key='fts_available'").fetchone()
    return bool(row and row["value"] == "1")


def bump_revision(con: sqlite3.Connection) -> int:
    con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='aggregate_rev'")
    row = con.execute("SELECT value FROM meta WHERE key='aggregate_rev'").fetchone()
    return int(row["value"])


def aggregate_revision(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key='aggregate_rev'").fetchone()
    return int(row["value"]) if row else 0


# -- Research view state token ------------------------------------------------
#
# A database-wide write marker for reusing derived research state across
# requests, connections and processes (customer_query._view_revision).
#
# Why not an existing revision: meta aggregate_rev and data_revision are
# bumped by hand, and many writers of the research view's inputs bump
# neither (the customer show-map rebuild, media registration and
# retention, source publication, imports' source/transcript rows, raw CLI
# SQL). PRAGMA data_version is per connection and never sees the
# connection's own writes. So every ordinary table carries one AFTER
# INSERT/UPDATE/DELETE trigger per operation that replaces the token with
# 128 fresh random bits: any row write by any writer through SQLite
# (application, CLI, sqlite3 shell, another process) changes it, a
# rolled-back write rolls it back with the data, and because it is random
# rather than a counter, a restored or copied file can never present an
# earlier token for different contents. DDL fires no trigger, so readers
# also key on a digest of the schema (view_state_key).
#
# Unguarded on purpose: sqlite_* internals, virtual tables (no triggers
# possible), FTS5 shadow tables (evidence_fts is written by imports and
# read only by search), and VIEW_STATE_UNREAD: bulk or high-churn tables
# (search index docs, the job ledger, monitoring, alert run state, audit,
# billing, share audit, lazily created feature tables) that no function
# the view computation runs names and an authorizer never sees it read
# (both pinned by tests on the full acceptance fixture). meta is guarded
# except for the token row and VIEW_STATE_UNREAD_META_KEYS (the
# aggregate_rev counter most writes bump); the view reads no meta row at
# all (pinned by the same authorizer test). Guards cost one meta update
# per written row, so these keep index rebuilds, worker heartbeats,
# webhook writes and alert checks cheap and stop them invalidating the
# view. Any other table without its exact guards (new,
# rebuilt, or guards dropped) makes view_state_key None: readers then
# recompute every time until ensure_view_state_guards repairs it, which
# also issues a fresh token.

VIEW_STATE_KEY = "research_view_state"
VIEW_STATE_TRIGGER_PREFIX = "ew_view_state_"
_FTS5_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
VIEW_STATE_UNREAD = frozenset({
    "evidence_search_docs", "transcript_segment_docs", "index_state",
    "processing_jobs", "job_attempts", "processing_runs", "p01_idempotency",
    "p01_job_deps", "p01_job_profiles", "p01_reconcile_log", "p09_feed_sources",
    "p09_run_episodes", "paid_operations", "schedules",
    "monitor_events", "monitor_baselines", "monitor_schedule_state",
    "monitor_preferences", "notification_outbox", "delivery_attempts",
    "billing_events", "billing_entitlement", "billing_bindings",
    "billing_subscriptions", "billing_trials", "share_audit",
    "manual_assessments", "saved_comparisons", "assignment_status", "comment_pins",
    "customer_alerts", "customer_alert_events", "audit",
    # Full-text segment index (transcript_index): bulk, derived from
    # source_versions, never read by the view computation.
    "transcript_segments", "transcript_index_versions",
    # Ad review Undo snapshots (corrections.UNDO_SCHEMA, created on the
    # first save): read only by corrections.undo, never by the view.
    "ad_label_undo",
    # Ask threads, turns, passages and reports: never read by the view.
    "ask_threads", "ask_turns", "ask_passages", "ask_reports",
    # Meaning alerts, runs and matches: never read by the view.
    "meaning_alerts", "meaning_runs", "meaning_matches",
    # Brief checks (model_brief_checks, brief_check_live_schema's
    # brief_check_runs): read only by the campaign draft's brief-check read
    # (brief_checks.with_model_checks) and the live brief check, never by
    # the view (GUARD-FIX, Fable F7). Guards on them made every brief-check
    # import refused (its authorizer allows writes to model_brief_checks
    # only, not the guard's meta update) and their rows hashed by every
    # research_view_state read.
    "model_brief_checks", "brief_check_runs",
})
# meta rows whose writes never change the token (besides the token row).
VIEW_STATE_UNREAD_META_KEYS = frozenset({"aggregate_rev"})


def _view_state_guarded_tables(schema_rows) -> list[str]:
    """Tables whose row writes must change the token, from sqlite_master
    rows (type, name, tbl_name, rootpage, sql)."""
    tables = [(r[1], r[4] or "") for r in schema_rows if r[0] == "table"]
    virtual = {name for name, sql in tables
               if sql.upper().startswith("CREATE VIRTUAL TABLE")}
    shadow = {v + suffix for v in virtual for suffix in _FTS5_SHADOW_SUFFIXES}
    return sorted(name for name, _sql in tables
                  if not name.startswith("sqlite_") and name not in virtual
                  and name not in shadow and name not in VIEW_STATE_UNREAD)


def _view_state_triggers(table: str) -> dict[str, str]:
    """Exact guard trigger SQL by trigger name for one table. Writes of the
    token row itself are excluded so a guard never re-fires on meta, and
    so are writes of VIEW_STATE_UNREAD_META_KEYS rows (an update fires
    unless both its old and new key are excluded)."""
    quoted = '"' + table.replace('"', '""') + '"'
    body = (" BEGIN UPDATE meta SET value=lower(hex(randomblob(16)))"
            f" WHERE key='{VIEW_STATE_KEY}'; END")
    whens = {"insert": "", "update": "", "delete": ""}
    if table == "meta":
        keys = [VIEW_STATE_KEY] + sorted(VIEW_STATE_UNREAD_META_KEYS - {VIEW_STATE_KEY})

        def guarded(row):
            return "(" + " AND ".join(f"{row}.key IS NOT '{key}'" for key in keys) + ")"
        whens = {"insert": f" WHEN {guarded('NEW')}",
                 "update": f" WHEN {guarded('NEW')} OR {guarded('OLD')}",
                 "delete": f" WHEN {guarded('OLD')}"}
    out = {}
    for op, when in whens.items():
        name = f"{VIEW_STATE_TRIGGER_PREFIX}{op}_{table}"
        out[name] = (f'CREATE TRIGGER "{name}" AFTER {op.upper()} ON {quoted}'
                     f"{when}{body}")
    return out


def _schema_rows(con: sqlite3.Connection) -> list[tuple]:
    return [tuple(r) for r in con.execute(
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master"
        " ORDER BY type, name")]


def ensure_view_state_guards(con: sqlite3.Connection, *, fresh_token: bool = False) -> None:
    """Install (or repair) every guard and drop stale ones. Any repair, or
    a missing token, issues a fresh token: writes made while a guard was
    missing were not tracked, so no earlier token may be reused. With
    every guard already in place every write since the token was issued
    was tracked, and the token stands (so routine schema setup, e.g. each
    worker start, does not invalidate servers' memos) unless fresh_token.
    Explicit producers only (schema setup, upgrade, restore); reads never
    call this."""
    with transaction(con, write=True):
        _ensure_research_state_table(con)
        rows = _schema_rows(con)
        existing = {r[1]: r[4] for r in rows if r[0] == "trigger"
                    and r[1].startswith(VIEW_STATE_TRIGGER_PREFIX)}
        wanted: dict[str, str] = {}
        for table in _view_state_guarded_tables(rows):
            wanted.update(_view_state_triggers(table))
        repaired = fresh_token
        for name, sql in existing.items():
            if wanted.get(name) != sql:
                con.execute(f'DROP TRIGGER "{name}"')
                repaired = True
        for name, sql in wanted.items():
            if existing.get(name) != sql:
                con.execute(sql)
                repaired = True
        token = con.execute("SELECT value FROM meta WHERE key=?", (VIEW_STATE_KEY,)).fetchone()
        if repaired or token is None or not token[0]:
            con.execute("INSERT INTO meta (key, value) VALUES (?, lower(hex(randomblob(16))))"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (VIEW_STATE_KEY,))
        ensure_research_state_guards(con)


def view_state_guard_problems(con: sqlite3.Connection) -> dict:
    """Read only: the guarded tables whose guards are missing or altered,
    {"missing": [table], "altered": [table]} (sorted, each table once).
    Both empty means view_state_key can be computed (given a token)."""
    rows = _schema_rows(con)
    triggers = {r[1]: r[4] for r in rows if r[0] == "trigger"
                and r[1].startswith(VIEW_STATE_TRIGGER_PREFIX)}
    missing, altered = set(), set()
    for table in _view_state_guarded_tables(rows):
        for name, sql in _view_state_triggers(table).items():
            if name not in triggers:
                missing.add(table)
            elif triggers[name] != sql:
                altered.add(table)
    return {"missing": sorted(missing), "altered": sorted(altered)}


def view_state_key(con: sqlite3.Connection) -> str | None:
    """The current research view state, read inside the caller's snapshot:
    a digest of the write token and the full schema, or None when any
    guard is missing or altered (then nothing may be reused)."""
    import hashlib
    rows = _schema_rows(con)
    triggers = {r[1]: r[4] for r in rows if r[0] == "trigger"
                and r[1].startswith(VIEW_STATE_TRIGGER_PREFIX)}
    for table in _view_state_guarded_tables(rows):
        for name, sql in _view_state_triggers(table).items():
            if triggers.get(name) != sql:
                return None
    row = con.execute("SELECT value FROM meta WHERE key=?", (VIEW_STATE_KEY,)).fetchone()
    if row is None or not row[0]:
        return None
    digest = hashlib.sha256()
    digest.update(str(row[0]).encode())
    for entry in rows:
        digest.update(b"\0" + repr(entry).encode())
    return digest.hexdigest()


# -- Research data state token -------------------------------------------------
#
# The research view revision (customer_query._view_revision) is a digest of
# this token and the caller's visibility, not a rescan of every result. The
# token is 128 fresh random bits that an AFTER INSERT/UPDATE/DELETE trigger
# on each research input table replaces whenever a row really changes (an
# UPDATE that rewrites equal values does not count), so any writer through
# SQLite (application, CLI, another process) moves it, a rolled-back write
# rolls it back, and a copied or restored file can never present an earlier
# token for other contents (same reasoning as the view state guards above).
# The tables are the inputs of research results: records, reviews,
# attributions, episodes and their show mapping, source versions and
# publications, history, policy, media and retention, and the customer-ad
# side tables. Writes elsewhere (saved items, trackers, campaigns, audit,
# jobs, search indexes) never move it. A table without its exact triggers
# makes research_state_token None: the view revision is then computed by
# the full rescan until ensure_view_state_guards repairs it. The token
# lives in its own one-row table, an ordinary guarded table, so the view
# computation reads no partly guarded row.

RESEARCH_STATE_TABLE = "research_state"
RESEARCH_TRIGGER_PREFIX = "ew_research_state_"
RESEARCH_TABLES = (
    "episodes", "evidence", "reviews", "brand_assocs", "source_versions",
    "evidence_source_membership", "source_states", "customer_episode_shows",
    "customer_show_identities", "media_artifacts", "transcript_versions",
    "source_content_policy", "p01p04_retention", "p01p04_holds",
    "evidence_history", "episode_history", "review_history", "association_history",
    "ad_observations", "observation_revisions", "observation_attributions",
    "field_confirmations", "ad_reads", "ad_brand_aliases", "ad_read_links", "ad_clips",
    "episode_screening", "ad_reads_meta",
)


def _research_triggers(con: sqlite3.Connection, table: str) -> dict[str, str]:
    """Exact research-state trigger SQL by name for one existing table."""
    columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    quoted = '"' + table.replace('"', '""') + '"'
    body = (f" BEGIN UPDATE {RESEARCH_STATE_TABLE} SET token=lower(hex(randomblob(16)))"
            " WHERE id=1; END")
    changed = " OR ".join(f'OLD."{c}" IS NOT NEW."{c}"' for c in columns) or "1"
    whens = {"insert": "", "update": f" WHEN {changed}", "delete": ""}
    out = {}
    for op, when in whens.items():
        name = f"{RESEARCH_TRIGGER_PREFIX}{op}_{table}"
        out[name] = (f'CREATE TRIGGER "{name}" AFTER {op.upper()} ON {quoted}'
                     f"{when}{body}")
    return out


def _research_tables_present(con: sqlite3.Connection) -> list[str]:
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [t for t in RESEARCH_TABLES if t in names]


def ensure_research_state_guards(con: sqlite3.Connection) -> None:
    """Install (or repair) the research-state triggers; any repair or a
    missing token issues a fresh token (writes made while a trigger was
    missing were not tracked). Joins the caller's transaction."""
    existing = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
        (RESEARCH_TRIGGER_PREFIX + "%",))}
    wanted: dict[str, str] = {}
    for table in _research_tables_present(con):
        wanted.update(_research_triggers(con, table))
    repaired = False
    for name, sql in existing.items():
        if wanted.get(name) != sql:
            con.execute(f'DROP TRIGGER "{name}"')
            repaired = True
    for name, sql in wanted.items():
        if existing.get(name) != sql:
            con.execute(sql)
            repaired = True
    token = con.execute(f"SELECT token FROM {RESEARCH_STATE_TABLE} WHERE id=1").fetchone()
    if repaired or token is None or not token[0]:
        con.execute(f"INSERT INTO {RESEARCH_STATE_TABLE} (id, token)"
                    " VALUES (1, lower(hex(randomblob(16))))"
                    " ON CONFLICT(id) DO UPDATE SET token=excluded.token")


def _ensure_research_state_table(con: sqlite3.Connection) -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {RESEARCH_STATE_TABLE} ("
                " id INTEGER PRIMARY KEY CHECK (id = 1), token TEXT NOT NULL)")


def research_state_token(con: sqlite3.Connection) -> str | None:
    """The research data token, or None when any research trigger is
    missing or altered (then the view revision is recomputed by rescan)."""
    triggers = {r[0]: r[1] for r in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
        (RESEARCH_TRIGGER_PREFIX + "%",))}
    for table in _research_tables_present(con):
        for name, sql in _research_triggers(con, table).items():
            if triggers.get(name) != sql:
                return None
    try:
        row = con.execute(f"SELECT token FROM {RESEARCH_STATE_TABLE} WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def audit(con: sqlite3.Connection, actor: str, action: str, target_type: str,
          target_id: str, detail: dict, revision: int) -> None:
    import json
    con.execute("INSERT INTO audit (at, actor, action, target_type, target_id, detail, aggregate_rev)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (utcnow(), actor, action, target_type, target_id, json.dumps(detail), revision))


# -- Agent history (schema v5, package C) ------------------------------------

def has_history_tables(con: sqlite3.Connection) -> bool:
    """True when the v5 agent history/policy tables exist on this workspace."""
    tables = _table_names(con)
    return all(t in tables for t in _V5_TABLES)


def require_current_schema(con: sqlite3.Connection) -> int:
    """Read-path schema gate: validate, never migrate/seed/reindex.

    Raises an actionable error naming the explicit upgrade command when the
    workspace is missing or behind the current schema.
    """
    version = _schema_version(con)
    if version != SCHEMA_VERSION:
        raise RuntimeError(
            f"workspace schema is v{version}, this build requires v{SCHEMA_VERSION};"
            " reads never migrate: run python3 scripts/app_workspace.py upgrade"
            " --workspace <workspace> first")
    return version


def ensure_workspace_identity(con: sqlite3.Connection) -> str:
    """Stable local workspace identity for recovery authority (idempotent)."""
    row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    if row is None:
        identity = "ws-" + uuid.uuid4().hex[:16]
        con.execute("INSERT INTO meta (key, value) VALUES ('workspace_id', ?)", (identity,))
        return identity
    return row["value"]


def workspace_identity(con: sqlite3.Connection) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key='workspace_id'").fetchone()
    return row["value"] if row else None


def data_revision(con: sqlite3.Connection) -> int:
    """Internal monotonic research revision, separate from aggregate_rev."""
    row = con.execute("SELECT value FROM meta WHERE key='data_revision'").fetchone()
    return int(row["value"]) if row else 0


def _bump_data_revision(con: sqlite3.Connection) -> int:
    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('data_revision', '0')")
    con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='data_revision'")
    return data_revision(con)


def _snapshot_bump(con: sqlite3.Connection) -> tuple[str, int]:
    return (utcnow(), _bump_data_revision(con))


def record_evidence_snapshot(con: sqlite3.Connection, evidence_id: str,
                             actor: str, origin: str = "mutation") -> int:
    """Persist the full current evidence card. Runs inside the caller's
    mutation transaction; call AFTER bumping in-transaction counters."""
    row = con.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
    if row is None:
        raise KeyError(evidence_id)
    at, _ = _snapshot_bump(con)
    con.execute("INSERT INTO evidence_history (workspace_id, evidence_id, evidence_revision,"
                " source, episode_id, orig_start_s, orig_end_s, excerpt, attrs,"
                " source_version_id, transcript_version_id, record_kind, assoc_revision,"
                " revision, history_origin, actor, created_at)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, row["revision"], row["source"], row["episode_id"],
                 row["orig_start_s"], row["orig_end_s"], row["excerpt"], row["attrs"],
                 row["source_version_id"], row["transcript_version_id"],
                 row["record_kind"] if "record_kind" in row.keys() else "candidate",
                 row["assoc_revision"], row["revision"], origin, actor, at))
    return row["revision"]


def record_episode_snapshot(con: sqlite3.Connection, episode_id: str,
                            actor: str, origin: str = "mutation") -> int:
    """Persist the full current episode card. Runs inside the caller's
    mutation transaction; call AFTER applying the mutation and bumping
    in-transaction counters (the pre-update state stays covered by the
    prior snapshot, keeping revision-keyed rows unique)."""
    row = con.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        raise KeyError(episode_id)
    at, _ = _snapshot_bump(con)
    con.execute("INSERT INTO episode_history (workspace_id, episode_id, episode_revision,"
                " show_title, ep_title, category, pub_date, duration_min, fetch_date,"
                " audio_url, media_id, transcript_version_id, alignment, media_edited,"
                " revision, history_origin, actor, created_at, kind, alias_of, media_artifact_id)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, row["revision"], row["show_title"], row["ep_title"],
                 row["category"], row["pub_date"], row["duration_min"], row["fetch_date"],
                 row["audio_url"], row["media_id"], row["transcript_version_id"],
                 row["alignment"], row["media_edited"], row["revision"],
                 origin, actor, at, row["kind"], row["alias_of"], row["media_artifact_id"]))
    return row["revision"]


def record_review_snapshot(con: sqlite3.Connection, evidence_id: str,
                           actor: str, origin: str = "mutation") -> int:
    """Snapshot current review values; revision 0 captures the explicitly
    empty initial state when no review row exists yet."""
    row = con.execute("SELECT * FROM reviews WHERE evidence_id=?", (evidence_id,)).fetchone()
    if row is None:
        revision, values = 0, {"judgment": None, "eff_start_s": None, "eff_end_s": None,
                               "notes": "", "bounds_verified": 0}
    else:
        revision, values = row["revision"], row
    at, _ = _snapshot_bump(con)
    con.execute("INSERT INTO review_history (workspace_id, evidence_id, review_revision,"
                " judgment, eff_start_s, eff_end_s, notes, bounds_verified,"
                " revision, history_origin, actor, created_at)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, revision, values["judgment"], values["eff_start_s"],
                 values["eff_end_s"], values["notes"], values["bounds_verified"],
                 revision, origin, actor, at))
    return revision


def record_association_snapshot(con: sqlite3.Connection, evidence_id: str,
                                actor: str, origin: str = "mutation") -> int:
    """Snapshot the COMPLETE brand/channel/basis/origin set, including an
    explicitly empty set: absence of rows is data, never a gap."""
    import json
    cur = con.execute("SELECT assoc_revision FROM evidence WHERE id=?",
                      (evidence_id,)).fetchone()
    if cur is None:
        raise KeyError(evidence_id)
    # PODS: model rows are display-only
    assocs = [dict(a) for a in con.execute(
        "SELECT brand, basis, origin, channel, revision FROM brand_assocs"
        " WHERE evidence_id=? AND basis<>'model' ORDER BY brand, channel",
        (evidence_id,)).fetchall()]
    at, _ = _snapshot_bump(con)
    con.execute("INSERT INTO association_history (workspace_id, evidence_id,"
                " association_revision, assocs_json, revision, history_origin, actor,"
                " created_at) VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                (evidence_id, cur["assoc_revision"], json.dumps(assocs),
                 cur["assoc_revision"], origin, actor, at))
    return cur["assoc_revision"]


def record_campaign_snapshot(con: sqlite3.Connection, campaign_id: int,
                             actor: str, origin: str = "mutation") -> int:
    """Snapshot brief, membership pins, and check definitions/assessments."""
    import json
    row = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if row is None:
        raise KeyError(campaign_id)
    pins = [{"evidence_id": r["evidence_id"],
             "evidence_revision": con.execute("SELECT revision FROM evidence WHERE id=?",
                                              (r["evidence_id"],)).fetchone()["revision"]}
            for r in con.execute("SELECT evidence_id FROM campaign_evidence WHERE campaign_id=?",
                                 (campaign_id,)).fetchall()]
    checks = [dict(c) for c in con.execute(
        "SELECT id, check_type, evidence_id, params, manual_verdict, manual_reason,"
        " revision FROM campaign_checks WHERE campaign_id=? ORDER BY id",
        (campaign_id,)).fetchall()]
    at, _ = _snapshot_bump(con)
    con.execute("INSERT INTO campaign_history (workspace_id, campaign_id, campaign_revision,"
                " name, status, brand, date_from, date_to, target_shows, message, offer,"
                " notes, manual_conclusion, manual_reason, evidence_pins_json, checks_json,"
                " revision, history_origin, actor, created_at)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, row["revision"], row["name"], row["status"], row["brand"],
                 row["date_from"], row["date_to"], row["target_shows"], row["message"],
                 row["offer"], row["notes"], row["manual_conclusion"], row["manual_reason"],
                 json.dumps(pins), json.dumps(checks), row["revision"], origin, actor, at))
    return row["revision"]


def retain_check_evaluation(con: sqlite3.Connection, campaign_id: int, check_id: int,
                            check_revision: int, evaluator_id: str,
                            evidence_pins: list, result: dict) -> None:
    """Retain one computed check result under its exact evaluator identity.
    Idempotent: re-retaining the same revision/evaluator is a no-op."""
    import json
    con.execute("INSERT OR IGNORE INTO check_evaluations (workspace_id, campaign_id, check_id,"
                " check_revision, evaluator_id, evidence_pins_json, result_json, created_at)"
                " VALUES ('local', ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, check_id, check_revision, evaluator_id,
                 json.dumps(evidence_pins), json.dumps(result), utcnow()))


def backfill_history(con: sqlite3.Connection) -> dict:
    """Snapshot all current state with history_origin='legacy_snapshot'.

    Backfills only the current existing state; never invents historical
    versions. Idempotent via INSERT OR IGNORE on history primary keys.
    """
    import json
    counts = {"evidence": 0, "episodes": 0, "reviews": 0, "associations": 0,
              "campaigns": 0, "policy": 0}
    at = utcnow()
    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('data_revision', '0')")
    for row in con.execute("SELECT * FROM evidence").fetchall():
        con.execute("INSERT OR IGNORE INTO evidence_history (workspace_id, evidence_id,"
                    " evidence_revision, source, episode_id, orig_start_s, orig_end_s,"
                    " excerpt, attrs, source_version_id, transcript_version_id, record_kind,"
                    " assoc_revision, revision, history_origin, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " 'legacy_snapshot', 'system:backfill', ?)",
                    (row["id"], row["revision"], row["source"], row["episode_id"],
                     row["orig_start_s"], row["orig_end_s"], row["excerpt"], row["attrs"],
                     row["source_version_id"], row["transcript_version_id"],
                     row["record_kind"] if "record_kind" in row.keys() else "candidate",
                     row["assoc_revision"], row["revision"], at))
        counts["evidence"] += 1
    for row in con.execute("SELECT * FROM episodes").fetchall():
        con.execute("INSERT OR IGNORE INTO episode_history (workspace_id, episode_id,"
                    " episode_revision, show_title, ep_title, category, pub_date,"
                    " duration_min, fetch_date, audio_url, media_id, transcript_version_id,"
                    " alignment, media_edited, revision, history_origin, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " 'legacy_snapshot', 'system:backfill', ?)",
                    (row["id"], row["revision"], row["show_title"], row["ep_title"],
                     row["category"], row["pub_date"], row["duration_min"], row["fetch_date"],
                     row["audio_url"], row["media_id"], row["transcript_version_id"],
                     row["alignment"], row["media_edited"], row["revision"], at))
        counts["episodes"] += 1
    for row in con.execute("SELECT * FROM reviews").fetchall():
        con.execute("INSERT OR IGNORE INTO review_history (workspace_id, evidence_id,"
                    " review_revision, judgment, eff_start_s, eff_end_s, notes,"
                    " bounds_verified, revision, history_origin, actor, created_at)"
                    " VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, 'legacy_snapshot',"
                    " 'system:backfill', ?)",
                    (row["evidence_id"], row["revision"], row["judgment"],
                     row["eff_start_s"], row["eff_end_s"], row["notes"],
                     row["bounds_verified"], row["revision"], at))
        counts["reviews"] += 1
    # PODS: model rows are display-only
    assoc_rows = con.execute("SELECT evidence_id, brand, basis, origin, channel, revision"
                             " FROM brand_assocs WHERE basis<>'model'"
                             " ORDER BY evidence_id, brand, channel").fetchall()
    # NOTE: backfill keeps the evidence_id key inside assocs_json (mutation-time
    # snapshots store the 5 display columns); resolvers tolerate both shapes.
    by_evidence: dict = {}
    for assoc in assoc_rows:
        by_evidence.setdefault(assoc["evidence_id"], []).append(dict(assoc))
    for row in con.execute("SELECT id, assoc_revision FROM evidence").fetchall():
        con.execute("INSERT OR IGNORE INTO association_history (workspace_id, evidence_id,"
                    " association_revision, assocs_json, revision, history_origin, actor,"
                    " created_at) VALUES ('local', ?, ?, ?, ?, 'legacy_snapshot',"
                    " 'system:backfill', ?)",
                    (row["id"], row["assoc_revision"],
                     json.dumps(by_evidence.get(row["id"], [])),
                     row["assoc_revision"], at))
        counts["associations"] += 1
    for row in con.execute("SELECT * FROM campaigns").fetchall():
        pins = [{"evidence_id": r["evidence_id"],
                 "evidence_revision": con.execute("SELECT revision FROM evidence WHERE id=?",
                                                  (r["evidence_id"],)).fetchone()["revision"]}
                for r in con.execute("SELECT evidence_id FROM campaign_evidence"
                                     " WHERE campaign_id=?", (row["id"],)).fetchall()]
        checks = [dict(c) for c in con.execute(
            "SELECT id, check_type, evidence_id, params, manual_verdict, manual_reason,"
            " revision FROM campaign_checks WHERE campaign_id=? ORDER BY id",
            (row["id"],)).fetchall()]
        con.execute("INSERT OR IGNORE INTO campaign_history (workspace_id, campaign_id,"
                    " campaign_revision, name, status, brand, date_from, date_to,"
                    " target_shows, message, offer, notes, manual_conclusion, manual_reason,"
                    " evidence_pins_json, checks_json, revision, history_origin, actor,"
                    " created_at) VALUES ('local', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                    " ?, ?, ?, 'legacy_snapshot', 'system:backfill', ?)",
                    (row["id"], row["revision"], row["name"], row["status"], row["brand"],
                     row["date_from"], row["date_to"], row["target_shows"], row["message"],
                     row["offer"], row["notes"], row["manual_conclusion"],
                     row["manual_reason"], json.dumps(pins), json.dumps(checks),
                     row["revision"], at))
        counts["campaigns"] += 1
    for key in con.execute("SELECT DISTINCT source_key FROM source_versions").fetchall():
        for category, disclosure, eligible in _POLICY_DEFAULTS:
            con.execute("INSERT OR IGNORE INTO source_content_policy (workspace_id, source_key,"
                        " content_category, agent_disclosure, export_eligible, history_origin,"
                        " updated_at) VALUES ('local', ?, ?, ?, ?, 'legacy_snapshot', ?)",
                        (key["source_key"], category, disclosure, eligible, at))
            counts["policy"] += 1
    return counts
