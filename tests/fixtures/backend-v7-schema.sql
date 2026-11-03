-- Synthetic candidate v7 schema snapshot (DDL only, no owner/corpus rows).
-- source: pristine v7 implementation; tables first, then indexes.
-- FTS5 shadow tables (evidence_fts_*) are auto-created by the virtual
-- table statement and are not stored here.
CREATE TABLE ad_observations (
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
CREATE TABLE assignments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  member TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE association_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  association_revision INTEGER NOT NULL,
  assocs_json TEXT NOT NULL DEFAULT '[]',
  revision INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, evidence_id, association_revision)
);
CREATE TABLE audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  aggregate_rev INTEGER NOT NULL
);
CREATE TABLE "brand_assocs" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  brand TEXT NOT NULL,
  basis TEXT NOT NULL,
  origin TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
, channel TEXT NOT NULL DEFAULT 'unspecified');
CREATE TABLE "campaign_checks" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  check_type TEXT NOT NULL,
  evidence_id TEXT REFERENCES evidence(id),
  params TEXT NOT NULL DEFAULT '{}',
  manual_verdict TEXT,
  manual_reason TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE "campaign_evidence" (
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  PRIMARY KEY (campaign_id, evidence_id)
);
CREATE TABLE campaign_history (
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
CREATE TABLE campaign_reports (
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
CREATE TABLE campaigns (
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
CREATE TABLE check_evaluations (
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
CREATE TABLE comments (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE coverage_attestations (
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
, source_identity INTEGER);
CREATE TABLE coverage_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  source_key TEXT NOT NULL DEFAULT '',
  episode_id TEXT REFERENCES episodes(id),
  definition_revision INTEGER NOT NULL DEFAULT 1,
  states TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE creative_memberships (
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
CREATE TABLE creatives (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'suggested',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE customer_alert_events (
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
CREATE TABLE customer_alerts (
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
CREATE TABLE customer_episode_shows (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT NOT NULL REFERENCES episodes(id),
  show_identity_id TEXT NOT NULL REFERENCES customer_show_identities(id),
  basis TEXT NOT NULL DEFAULT '',
  source_version_id INTEGER REFERENCES source_versions(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, episode_id)
);
CREATE TABLE customer_mistake_reports (
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
CREATE TABLE customer_saved_searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  name TEXT NOT NULL,
  query_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE customer_show_identities (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  display_title TEXT NOT NULL DEFAULT '',
  authority_key TEXT NOT NULL DEFAULT '',
  authority_basis TEXT NOT NULL DEFAULT '',
  source_version_id INTEGER REFERENCES source_versions(id),
  unresolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE episode_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT NOT NULL REFERENCES episodes(id),
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
CREATE TABLE "episodes" (
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
, alignment TEXT NOT NULL DEFAULT 'unknown', media_edited INTEGER NOT NULL DEFAULT 0, media_artifact_id TEXT REFERENCES media_artifacts(id));
CREATE TABLE "evidence" (
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
, transcript_version_id INTEGER REFERENCES source_versions(id), source_record_key TEXT, assoc_revision INTEGER NOT NULL DEFAULT 0, record_kind TEXT NOT NULL DEFAULT 'candidate' CHECK (record_kind IN ('candidate','observation')), observation_revision_id INTEGER REFERENCES observation_revisions(id));
CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, text);
CREATE TABLE evidence_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
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
CREATE TABLE expected_placements (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  scope TEXT NOT NULL DEFAULT 'show_window',
  show_id TEXT NOT NULL DEFAULT '',
  episode_id TEXT REFERENCES episodes(id),
  date_from TEXT,
  date_to TEXT,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  expected_count INTEGER NOT NULL DEFAULT 1,
  requirements TEXT NOT NULL DEFAULT '{}',
  provenance TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE exports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  format TEXT NOT NULL,
  filter TEXT NOT NULL DEFAULT '{}',
  aggregate_rev INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE job_attempts (
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
CREATE TABLE media_artifacts (
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
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE monitor_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  schedule_id TEXT NOT NULL REFERENCES schedules(id),
  event_key TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE (schedule_id, event_key)
);
CREATE TABLE notification_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  event_id INTEGER REFERENCES monitor_events(id),
  recipient TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE observation_attributions (
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
CREATE TABLE observation_revisions (
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
CREATE TABLE occurrence_observations (
  occurrence_id TEXT NOT NULL REFERENCES occurrences(id),
  observation_id TEXT NOT NULL REFERENCES ad_observations(id),
  basis TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (occurrence_id, observation_id)
);
CREATE TABLE occurrences (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  episode_id TEXT REFERENCES episodes(id),
  correspondence TEXT NOT NULL DEFAULT 'confirmed',
  revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE paid_operations (
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
CREATE TABLE placement_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'local',
  placement_id TEXT NOT NULL REFERENCES expected_placements(id),
  campaign_revision INTEGER NOT NULL,
  outcome TEXT NOT NULL DEFAULT 'unknown',
  reasons TEXT NOT NULL DEFAULT '[]',
  evidence_pins TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE placement_evidence (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  placement_id TEXT NOT NULL REFERENCES expected_placements(id),
  ref TEXT NOT NULL,
  attached_at TEXT NOT NULL,
  attached_by TEXT NOT NULL DEFAULT 'owner',
  PRIMARY KEY (workspace_id, campaign_id, placement_id, ref)
);
CREATE TABLE processing_jobs (
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
, stage_key TEXT);
CREATE TABLE processing_runs (
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
CREATE TABLE review_history (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
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
CREATE TABLE "reviews" (
  evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
  judgment TEXT,
  eff_start_s REAL,
  eff_end_s REAL,
  notes TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
, bounds_verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE schedules (
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
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE shares (
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
CREATE TABLE show_artwork (
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
CREATE TABLE source_content_policy (
  workspace_id TEXT NOT NULL DEFAULT 'local',
  source_key TEXT NOT NULL,
  content_category TEXT NOT NULL,
  agent_disclosure TEXT NOT NULL DEFAULT 'allow',
  export_eligible INTEGER NOT NULL DEFAULT 1,
  history_origin TEXT NOT NULL DEFAULT 'mutation',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (workspace_id, source_key, content_category)
);
CREATE TABLE source_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE TABLE source_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_key TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  payload TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (source_key, version_no)
);
CREATE TABLE transcript_versions (
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
CREATE TABLE watchlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  query TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_assoc_history_id ON association_history (evidence_id);
CREATE INDEX idx_assocs_brand ON brand_assocs (brand);
CREATE INDEX idx_assocs_evidence ON brand_assocs (evidence_id);
CREATE INDEX idx_attempts_job ON job_attempts (job_id);
CREATE INDEX idx_audit_idempotency ON audit (action, target_type,
  json_extract(detail, '$.idempotency_key'), json_extract(detail, '$.digest'));
CREATE INDEX idx_audit_target ON audit (target_type, target_id);
CREATE INDEX idx_campaign_history_id ON campaign_history (campaign_id);
CREATE INDEX idx_campaign_reports_campaign ON campaign_reports (campaign_id);
CREATE INDEX idx_customer_alert_events_alert ON customer_alert_events (alert_id);
CREATE INDEX idx_customer_mistake_ref ON customer_mistake_reports (ref);
CREATE INDEX idx_episode_history_id ON episode_history (episode_id);
CREATE INDEX idx_events_schedule ON monitor_events (schedule_id);
CREATE INDEX idx_evidence_episode ON evidence (episode_id);
CREATE INDEX idx_evidence_history_id ON evidence_history (evidence_id);
CREATE INDEX idx_evidence_source ON evidence (source);
CREATE INDEX idx_jobs_run ON processing_jobs (run_id);
CREATE INDEX idx_obs_episode ON ad_observations (episode_id);
CREATE INDEX idx_occ_episode ON occurrences (episode_id);
CREATE INDEX idx_placement_evidence_placement ON placement_evidence (placement_id);
CREATE INDEX idx_review_history_id ON review_history (evidence_id);
CREATE INDEX idx_show_artwork_status ON show_artwork (status);
