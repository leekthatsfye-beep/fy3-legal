# DATABASE_SCHEMA

PostgreSQL 16. Migrations via Alembic. All timestamps `timestamptz`, stored UTC.

Conventions: `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `created_at`/`updated_at`
`timestamptz NOT NULL DEFAULT now()`, `version integer NOT NULL DEFAULT 1`.
`project_id` on every tenant-scoped table with `ON DELETE RESTRICT` (never cascade-delete a project
by accident). Enums are native PostgreSQL enum types so an invalid value is a database error.

Required extensions: `pgcrypto` (UUIDs), `vector` (pgvector), `pg_trgm` (title similarity).

### Decision: pgvector rather than a dedicated vector database

**WHY.** Clustering and semantic-similarity search are needed for topic clusters, saturation
detection, and duplicate-idea rejection.

**PROBLEM SOLVED.** Avoids a second datastore, a second backup story, and cross-store consistency
between an embedding and the row it describes.

**ALTERNATIVES.** Pinecone/Weaviate/Qdrant — rejected at this scale: corpus is on the order of
10⁴–10⁵ vectors, which Postgres handles comfortably. A standalone store adds a failure mode where
an embedding exists for a deleted video.

**TRADEOFF.** pgvector's ANN indexes are less tunable than a purpose-built engine, and very large
corpora would eventually need one. The `EmbeddingStore` interface (see PROVIDER_INTERFACES.md)
keeps that migration to one adapter.

---

## 1. Identity and tenancy

```sql
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  password_hash text NOT NULL,            -- argon2id
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  slug citext NOT NULL UNIQUE,
  status project_status NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_members (
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    uuid NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
  role project_role NOT NULL DEFAULT 'owner',
  PRIMARY KEY (project_id, user_id)
);
```

## 2. Our channels

```sql
CREATE TABLE channels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  youtube_channel_id text NOT NULL,
  title text NOT NULL,
  uploads_playlist_id text,                  -- cached; avoids repeat channels.list
  timezone text NOT NULL DEFAULT 'UTC',      -- analytics day boundaries
  status channel_status NOT NULL DEFAULT 'active',
  credential_id uuid REFERENCES oauth_credentials(id),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, youtube_channel_id)
);

CREATE TABLE channel_settings (
  channel_id uuid PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
  voice_provider text, voice_id text,
  voice_settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  caption_style jsonb NOT NULL DEFAULT '{}'::jsonb,
  target_duration_seconds int,
  editorial_guidelines text,                 -- injected as DATA, never as instructions
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Baselines are COMPUTED, never hand-entered. Recomputed on a schedule.
CREATE TABLE channel_baselines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  computed_at timestamptz NOT NULL DEFAULT now(),
  window_days int NOT NULL,
  sample_size int NOT NULL,
  median_views_per_day numeric,
  mad_views_per_day numeric,
  median_ctr numeric,
  median_avg_view_percentage numeric,
  method_version text NOT NULL,
  status baseline_status NOT NULL DEFAULT 'ok',   -- ok | insufficient_data
  UNIQUE (channel_id, computed_at, method_version)
);
```

## 3. Competitor intelligence

```sql
CREATE TABLE competitor_channels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  youtube_channel_id text NOT NULL,
  title text NOT NULL,
  uploads_playlist_id text,
  subscriber_count bigint,
  is_active boolean NOT NULL DEFAULT true,
  last_ingested_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, youtube_channel_id)
);

-- Immutable facts about a competitor video.
CREATE TABLE competitor_videos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  competitor_channel_id uuid NOT NULL REFERENCES competitor_channels(id) ON DELETE CASCADE,
  youtube_video_id text NOT NULL,
  title text NOT NULL,
  description text,
  published_at timestamptz NOT NULL,
  duration_seconds int,
  thumbnail_url text,
  topic_cluster_id uuid REFERENCES topic_clusters(id),
  title_embedding vector(1536),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, youtube_video_id)
);

-- APPEND-ONLY time series. This is what makes velocity measurable.
CREATE TABLE competitor_video_snapshots (
  id bigserial PRIMARY KEY,
  competitor_video_id uuid NOT NULL REFERENCES competitor_videos(id) ON DELETE CASCADE,
  observed_at timestamptz NOT NULL DEFAULT now(),
  age_days numeric NOT NULL,
  view_count bigint,
  like_count bigint,
  comment_count bigint,
  ingestion_run_id uuid REFERENCES ingestion_runs(id)
);
CREATE INDEX ON competitor_video_snapshots (competitor_video_id, observed_at DESC);

CREATE TABLE competitor_video_scores (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  competitor_video_id uuid NOT NULL REFERENCES competitor_videos(id) ON DELETE CASCADE,
  computed_at timestamptz NOT NULL DEFAULT now(),
  method_version text NOT NULL,
  views_per_day numeric,
  log_vpd numeric,
  robust_z numeric,
  outlier_basis text NOT NULL,       -- 'channel+age+cluster' | 'channel+age' | 'channel'
  stratum_n int NOT NULL,
  confidence numeric NOT NULL,
  status score_status NOT NULL,      -- ok | provisional | insufficient_data
  UNIQUE (competitor_video_id, method_version, computed_at)
);
```

**Note on `competitor_video_scores`:** scores are versioned rows, not mutable columns on the video.
Changing the methodology must never silently rewrite history — both versions coexist and the
dashboard states which one it is showing.

## 4. Topics, clusters, title patterns

```sql
CREATE TABLE topic_clusters (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  label text NOT NULL,                     -- human-editable; generated label is a suggestion
  centroid vector(1536),
  method_version text NOT NULL,
  member_count int NOT NULL DEFAULT 0,
  median_views_per_day numeric,
  outlier_rate numeric,
  uploads_last_30d int,
  momentum numeric,
  saturation saturation_level,             -- low | moderate | high | oversaturated
  computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE topics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  topic_cluster_id uuid REFERENCES topic_clusters(id),
  name text NOT NULL,
  freshness_type freshness_type NOT NULL,  -- evergreen | trending | breaking | event_driven
  event_date timestamptz,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz,
  freshness_score numeric,
  embedding vector(1536),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Abstract structures only. Never a copied competitor title.
CREATE TABLE title_patterns (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  shape text NOT NULL,                     -- e.g. 'SUBJECT + unexpected_significance_claim'
  length_bucket text,
  is_question boolean,
  has_number boolean,
  has_named_entity boolean,
  curiosity_gap boolean,
  contrarian boolean,
  urgency boolean,
  promise_type text,
  observed_count int NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, shape)
);
```

## 5. Research, sources, evidence

```sql
CREATE TABLE research_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  topic_id uuid REFERENCES topics(id),
  video_project_id uuid REFERENCES video_projects(id),
  provider text NOT NULL,
  status run_status NOT NULL DEFAULT 'running',
  query_count int NOT NULL DEFAULT 0,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error text
);

CREATE TABLE research_queries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  query text NOT NULL,
  provider text NOT NULL,
  executed_at timestamptz NOT NULL DEFAULT now(),
  result_count int NOT NULL DEFAULT 0
);

CREATE TABLE sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  url text NOT NULL,
  canonical_url text,
  publisher text,
  title text,
  published_date date,
  retrieved_at timestamptz NOT NULL DEFAULT now(),
  content_hash text NOT NULL,
  stored_text_key text,                    -- StorageProvider key for the retrieved text
  authority_tier smallint,                 -- 1 primary, 2 reputable secondary, 3 other
  http_status int,
  UNIQUE (project_id, content_hash)
);

-- A specific supported span. Must be locatable in the source text.
CREATE TABLE evidence (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  research_run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
  source_id uuid NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  quoted_text text NOT NULL,
  char_start int, char_end int,
  kind evidence_kind NOT NULL,             -- fact | claim | opinion | inference
  relevance numeric,
  locator_verified boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

`evidence.locator_verified` is set by a mechanical check that `quoted_text` occurs in the stored
source text. Evidence with `locator_verified = false` cannot support a claim. See
ARCHITECTURE_REVIEW.md finding R-04.

## 6. Video projects, titles, scripts, claims

```sql
CREATE TABLE video_projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  opportunity_id uuid REFERENCES opportunities(id),
  working_title text,
  status video_status NOT NULL DEFAULT 'IDEA',
  angle text,
  target_duration_seconds int,
  version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON video_projects (project_id, status);

CREATE TABLE video_state_transitions (
  id bigserial PRIMARY KEY,
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  from_status video_status,
  to_status video_status NOT NULL,
  actor_type actor_type NOT NULL,          -- user | job | system
  actor_id text,
  reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE titles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  text text NOT NULL,
  title_pattern_id uuid REFERENCES title_patterns(id),
  clarity numeric, curiosity numeric, specificity numeric, promise_alignment numeric,
  max_competitor_similarity numeric,       -- rejection gate
  is_selected boolean NOT NULL DEFAULT false,
  prompt_version_id uuid REFERENCES prompt_versions(id),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON titles (video_project_id) WHERE is_selected;

CREATE TABLE scripts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  current_version_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Append-only. History is never overwritten.
CREATE TABLE script_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_id uuid NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
  version_number int NOT NULL,
  stage script_stage NOT NULL,             -- outline | claim_map | hook | draft | assembled | ...
  body text NOT NULL,
  word_count int,
  prompt_version_id uuid REFERENCES prompt_versions(id),
  created_by actor_type NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (script_id, version_number)
);

CREATE TABLE claims (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_version_id uuid NOT NULL REFERENCES script_versions(id) ON DELETE CASCADE,
  text text NOT NULL,
  kind evidence_kind NOT NULL,
  risk claim_risk NOT NULL DEFAULT 'LOW',
  verification_status verification_status NOT NULL DEFAULT 'UNVERIFIED',
  verified_at timestamptz,
  verified_by actor_type,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE citations (
  claim_id uuid NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  evidence_id uuid NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
  PRIMARY KEY (claim_id, evidence_id)
);

CREATE TABLE qc_results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  script_version_id uuid REFERENCES script_versions(id),
  check_name text NOT NULL,                -- fact | structure | retention | repetition | ...
  passed boolean NOT NULL,
  blocking boolean NOT NULL,
  findings jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

## 7. Opportunities and scoring

```sql
CREATE TABLE opportunities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  topic_id uuid REFERENCES topics(id),
  topic_cluster_id uuid REFERENCES topic_clusters(id),
  proposed_angle text NOT NULL,
  audience text,
  viewer_question text,
  hook_concept text,
  content_promise text,
  why_now text,
  differentiation text,
  estimated_shelf_life_days int,
  status opportunity_status NOT NULL DEFAULT 'proposed',
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Component scores stored SEPARATELY. No single magic number in this table.
CREATE TABLE opportunity_scores (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  opportunity_id uuid NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  method_version text NOT NULL,
  demand numeric, momentum numeric, competition numeric, originality numeric,
  channel_fit numeric, evidence numeric, monetization numeric,
  production_difficulty numeric, freshness numeric,
  inputs jsonb NOT NULL DEFAULT '{}'::jsonb,   -- exact values the formula consumed
  computed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (opportunity_id, method_version, computed_at)
);

-- The weighted ranking is a separate, configurable, re-runnable artifact.
CREATE TABLE ranking_models (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  name text NOT NULL,
  weights jsonb NOT NULL,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE opportunity_evidence (
  opportunity_id uuid NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  evidence_id uuid NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
  PRIMARY KEY (opportunity_id, evidence_id)
);
```

`opportunity_scores.inputs` stores the exact numbers the formula consumed. Without it, a score is
unreproducible the moment the underlying data changes — and an unreproducible score cannot be
debugged or defended.

## 8. Production: beats, visuals, assets, voice, render

```sql
CREATE TABLE story_beats (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  script_version_id uuid NOT NULL REFERENCES script_versions(id) ON DELETE CASCADE,
  ordinal int NOT NULL,
  start_estimate_seconds numeric,
  duration_estimate_seconds numeric,
  purpose text,
  spoken_text text NOT NULL,
  visual_intent text,
  information_gained text,
  open_loop text,
  payoff_of_beat_id uuid REFERENCES story_beats(id),
  energy_level smallint,
  UNIQUE (script_version_id, ordinal)
);

CREATE TABLE visual_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  script_version_id uuid NOT NULL REFERENCES script_versions(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE shots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  visual_plan_id uuid NOT NULL REFERENCES visual_plans(id) ON DELETE CASCADE,
  story_beat_id uuid REFERENCES story_beats(id),
  ordinal int NOT NULL,
  duration_seconds numeric NOT NULL,
  visual_description text NOT NULL,
  asset_type asset_type NOT NULL,
  asset_id uuid REFERENCES assets(id),
  overlay_text text,
  animation text, transition text,
  priority smallint NOT NULL DEFAULT 3,
  UNIQUE (visual_plan_id, ordinal)
);

-- Immutable, content-addressed, provenance-tracked.
CREATE TABLE assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  file_hash text NOT NULL,
  storage_key text NOT NULL,
  mime_type text NOT NULL,
  bytes bigint,
  width int, height int, duration_seconds numeric,
  origin asset_origin NOT NULL,            -- owned | licensed_stock | generated | screenshot
  source_url text,
  license_id uuid REFERENCES licenses(id),
  creation_method text,
  derived_from_asset_id uuid REFERENCES assets(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, file_hash)
);

CREATE TABLE licenses (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  name text NOT NULL,
  allows_commercial boolean NOT NULL,
  allows_modification boolean NOT NULL,
  requires_attribution boolean NOT NULL,
  attribution_text text,
  expires_at timestamptz,
  document_key text
);

-- Segment-level so one edited paragraph does not regenerate the narration.
CREATE TABLE voiceover_segments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  story_beat_id uuid REFERENCES story_beats(id),
  text_hash text NOT NULL,
  voice_id text NOT NULL,
  settings_hash text NOT NULL,
  storage_key text,
  duration_seconds numeric,
  status segment_status NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (video_project_id, text_hash, voice_id, settings_hash)
);
```

The `UNIQUE (text_hash, voice_id, settings_hash)` is the cache key. Identical text with identical
voice settings is generated once and reused — this is the single largest voice-cost saving and it
is enforced by the schema rather than by remembering to check.

```sql
CREATE TABLE timelines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  timeline_hash text NOT NULL,
  spec jsonb NOT NULL,                     -- validated against a Pydantic schema before insert
  schema_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (video_project_id, timeline_hash)
);

CREATE TABLE render_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  timeline_id uuid NOT NULL REFERENCES timelines(id) ON DELETE RESTRICT,
  status job_status NOT NULL DEFAULT 'queued',
  attempt int NOT NULL DEFAULT 1,
  celery_task_id text,
  progress numeric NOT NULL DEFAULT 0,
  started_at timestamptz, finished_at timestamptz,
  failure_reason text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE renders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  render_job_id uuid NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
  storage_key text NOT NULL,
  file_hash text NOT NULL,
  duration_seconds numeric,
  bytes bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE thumbnail_concepts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE CASCADE,
  primary_subject text, background text, composition text, emotion text,
  overlay_text text, contrast_strategy text, curiosity_mechanism text,
  title_connection text,
  distinctiveness numeric,                 -- guards against 5 near-identical concepts
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE thumbnails (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  thumbnail_concept_id uuid NOT NULL REFERENCES thumbnail_concepts(id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES assets(id),
  is_selected boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

## 9. Publishing

```sql
CREATE TABLE publishing_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  idempotency_key text NOT NULL,
  render_id uuid NOT NULL REFERENCES renders(id),
  thumbnail_id uuid REFERENCES thumbnails(id),
  resumable_session_uri text,              -- resume, never restart, an interrupted upload
  status publish_status NOT NULL DEFAULT 'pending',
  youtube_video_id text,
  scheduled_for timestamptz,
  privacy privacy_status NOT NULL DEFAULT 'private',
  attempt int NOT NULL DEFAULT 1,
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (idempotency_key)
);

CREATE TABLE published_videos (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  video_project_id uuid NOT NULL REFERENCES video_projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  youtube_video_id text NOT NULL,
  published_at timestamptz NOT NULL,
  title_id uuid REFERENCES titles(id),
  thumbnail_id uuid REFERENCES thumbnails(id),
  topic_cluster_id uuid REFERENCES topic_clusters(id),
  duration_seconds int,
  UNIQUE (channel_id, youtube_video_id)
);
```

`UNIQUE (idempotency_key)` plus `resumable_session_uri` is the duplicate-upload defense. See
JOB_ARCHITECTURE.md §5.

## 10. Analytics and learning

```sql
-- APPEND-ONLY. NULL means "not reported", never 0.
CREATE TABLE analytics_snapshots (
  id bigserial PRIMARY KEY,
  published_video_id uuid NOT NULL REFERENCES published_videos(id) ON DELETE CASCADE,
  checkpoint analytics_checkpoint NOT NULL,   -- 1h 6h 24h 48h 7d 14d 30d
  collected_at timestamptz NOT NULL DEFAULT now(),
  impressions bigint,
  ctr numeric,
  views bigint,
  estimated_minutes_watched numeric,
  average_view_duration_seconds numeric,
  average_view_percentage numeric,
  subscribers_gained int,
  estimated_revenue numeric,
  rpm numeric,
  traffic_sources jsonb,
  metrics_unavailable text[] NOT NULL DEFAULT '{}',
  UNIQUE (published_video_id, checkpoint)
);

CREATE TABLE relative_performance (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  published_video_id uuid NOT NULL REFERENCES published_videos(id) ON DELETE CASCADE,
  channel_baseline_id uuid NOT NULL REFERENCES channel_baselines(id),
  checkpoint analytics_checkpoint NOT NULL,
  relative_views numeric, relative_watch_time numeric,
  relative_ctr numeric, relative_retention numeric,
  method_version text NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (published_video_id, checkpoint, method_version)
);

CREATE TABLE performance_insights (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  statement text NOT NULL,
  metric text NOT NULL,
  dimension text NOT NULL,                 -- cluster | title_pattern | duration_bucket | ...
  dimension_value text NOT NULL,
  sample_size int NOT NULL,
  effect_size numeric,
  confidence numeric,
  period_start timestamptz NOT NULL,
  period_end timestamptz NOT NULL,
  method_version text NOT NULL,
  status insight_status NOT NULL,          -- provisional | supported | retired
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE insight_supporting_videos (
  insight_id uuid NOT NULL REFERENCES performance_insights(id) ON DELETE CASCADE,
  published_video_id uuid NOT NULL REFERENCES published_videos(id) ON DELETE CASCADE,
  PRIMARY KEY (insight_id, published_video_id)
);

CREATE TABLE experiments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
  hypothesis text NOT NULL,
  primary_metric text NOT NULL,
  secondary_metrics text[] NOT NULL DEFAULT '{}',
  minimum_sample int NOT NULL,
  status experiment_status NOT NULL DEFAULT 'draft',
  started_at timestamptz, ended_at timestamptz,
  result text, confidence numeric
);

CREATE TABLE experiment_variants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  experiment_id uuid NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  label text NOT NULL,
  is_control boolean NOT NULL DEFAULT false,
  definition jsonb NOT NULL
);

CREATE TABLE experiment_assignments (
  experiment_id uuid NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
  published_video_id uuid NOT NULL REFERENCES published_videos(id) ON DELETE CASCADE,
  variant_id uuid NOT NULL REFERENCES experiment_variants(id),
  PRIMARY KEY (experiment_id, published_video_id)
);
```

**`performance_insights` has a hard constraint enforced in the service layer:** an insight below
the configured minimum sample size is written with `status = 'provisional'` and the Channel Memory
query used by generation prompts filters to `status = 'supported'`. A provisional insight is
visible to the human and invisible to the model.

## 11. Platform tables

```sql
CREATE TABLE oauth_credentials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  provider text NOT NULL,
  subject_id text NOT NULL,
  ciphertext bytea NOT NULL,               -- envelope-encrypted; see SECURITY_MODEL
  key_version int NOT NULL,
  scopes text[] NOT NULL,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, provider, subject_id)
);

CREATE TABLE prompt_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  version int NOT NULL,
  content_hash text NOT NULL,
  model text NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  stable_prefix text NOT NULL,
  volatile_suffix text NOT NULL,
  is_active boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);

CREATE TABLE generations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  prompt_version_id uuid NOT NULL REFERENCES prompt_versions(id),
  provider text NOT NULL, model text NOT NULL,
  request_id text, job_id uuid,
  input_tokens int, output_tokens int,
  cache_read_tokens int, cache_write_tokens int,
  latency_ms int,
  stop_reason text,
  entity_type text, entity_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE cost_entries (
  id bigserial PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid REFERENCES channels(id),
  video_project_id uuid REFERENCES video_projects(id),
  provider text NOT NULL,
  operation text NOT NULL,
  quantity numeric NOT NULL,
  unit text NOT NULL,                      -- tokens | characters | seconds | requests | units
  amount_usd numeric(12,6) NOT NULL,
  is_estimate boolean NOT NULL DEFAULT false,
  generation_id uuid REFERENCES generations(id),
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON cost_entries (project_id, occurred_at);

CREATE TABLE revenue_entries (
  id bigserial PRIMARY KEY,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  channel_id uuid NOT NULL REFERENCES channels(id),
  published_video_id uuid REFERENCES published_videos(id),
  revenue_type revenue_type NOT NULL,      -- adsense | affiliate | sponsorship | digital_product | lead_gen | other
  amount_usd numeric(12,2) NOT NULL,
  period_start date NOT NULL,
  period_end date NOT NULL,
  source_note text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quota_ledger (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  quota_day date NOT NULL,                 -- provider-local day (YouTube: US/Pacific)
  operation text NOT NULL,
  units int NOT NULL,
  reserved boolean NOT NULL,
  settled boolean NOT NULL DEFAULT false,
  job_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON quota_ledger (provider, quota_day);

CREATE TABLE ingestion_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
  competitor_channel_id uuid REFERENCES competitor_channels(id),
  status run_status NOT NULL DEFAULT 'running',
  videos_seen int NOT NULL DEFAULT 0,
  snapshots_written int NOT NULL DEFAULT 0,
  quota_units_spent int NOT NULL DEFAULT 0,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error text
);

CREATE TABLE system_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  celery_task_id text,
  queue text NOT NULL,
  status job_status NOT NULL DEFAULT 'queued',
  attempt int NOT NULL DEFAULT 1,
  max_attempts int NOT NULL DEFAULT 3,
  idempotency_key text,
  args_hash text,
  progress numeric NOT NULL DEFAULT 0,
  failure_reason text,
  queued_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz, finished_at timestamptz,
  UNIQUE (idempotency_key)
);

CREATE TABLE audit_logs (
  id bigserial PRIMARY KEY,
  project_id uuid REFERENCES projects(id),
  actor_type actor_type NOT NULL,
  actor_id text,
  action text NOT NULL,
  entity_type text NOT NULL,
  entity_id uuid,
  before jsonb, after jsonb,
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

---

## 12. Indexing notes

- `competitor_video_snapshots (competitor_video_id, observed_at DESC)` — the hot read path.
- `competitor_videos USING ivfflat (title_embedding vector_cosine_ops)` — similarity / saturation.
- `competitor_videos USING gin (title gin_trgm_ops)` — lexical near-duplicate title guard.
- `video_projects (project_id, status)` — dashboard board view.
- `analytics_snapshots (published_video_id, checkpoint)` — unique, doubles as lookup.
- `cost_entries (project_id, occurred_at)` — budget windows.

## 13. Deliberate omissions from the spec's table list

`opportunity_scores` replaces a separate magic-score column; `evidence` was added (the spec's
`sources` alone cannot support the claim system); `competitor_video_snapshots`,
`quota_ledger`, `ingestion_runs`, `generations`, `prompt_versions`, `oauth_credentials`,
`licenses`, `timelines`, `video_state_transitions`, and `relative_performance` were added because
the requirements in spec §§7, 17, 21, 23, 30, 37, 39, 41 are not satisfiable without them.
Rationale for each is in ARCHITECTURE_REVIEW.md.
