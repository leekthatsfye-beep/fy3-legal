# DATA_FLOW

End-to-end pipelines. Each stage names its inputs, outputs, the tables written, and the failure
mode that matters.

---

## Flow A — Competitor ingestion (recurring, quota-bounded)

```
Beat schedule
   │
   ▼
[plan_ingestion]  reads competitor_channels where is_active
   │              asks QuotaBroker how many units are available today
   │              emits per-channel jobs sized to fit the envelope
   ▼
[ingest_channel(competitor_channel_id)]
   │
   ├─ channels.list (1u) ─────────► cache uploads_playlist_id (skipped if cached)
   ├─ playlistItems.list (1u/page) ► collect youtube_video_id list
   └─ videos.list (1u per 50 ids) ─► statistics + contentDetails
   │
   ▼
UPSERT competitor_videos      (immutable facts only — never statistics)
INSERT competitor_video_snapshots  (append-only observation)
UPDATE ingestion_runs         (videos_seen, snapshots_written, quota_units_spent)
SETTLE quota_ledger
```

**Failure mode that matters:** a partially completed ingestion must not look like a complete one.
`ingestion_runs.status` stays `running` until the final page; a run that dies leaves `running` and
is reaped to `failed` by a sweeper. Downstream scoring **ignores failed runs' snapshots** for
baseline computation, because a half-ingested channel produces a biased baseline.

**Quota exhaustion is not an error.** When the broker cannot reserve, the job re-queues itself for
the next quota day and records `deferred`. Ingestion is designed to make progress across days.

---

## Flow B — Normalization, clustering, outlier detection

```
competitor_video_snapshots (new rows)
   │
   ▼
[embed_titles]      EmbeddingProvider ──► competitor_videos.title_embedding
   │
   ▼
[cluster_topics]    clustering over embeddings ──► topic_clusters (+ membership)
   │                                               centroid, member_count, method_version
   ▼
[compute_cluster_stats]  median_views_per_day, outlier_rate,
   │                     uploads_last_30d, momentum, saturation
   ▼
[score_outliers]    for each video: log(1 + views/age_days)
   │                stratify (channel, age_bucket, cluster) → median + MAD
   │                robust_z; fall back + lower confidence if n < 8
   ▼
competitor_video_scores  (versioned rows, never in-place updates)
```

**Failure mode that matters:** re-running clustering with a new `method_version` must not orphan
old scores. Cluster membership is versioned; scores record the `method_version` they were computed
under. The dashboard always states which version it is displaying.

---

## Flow C — Research (before any script exists)

```
Topic (approved for research)
   │
   ▼
[plan_queries]   LLM proposes queries ──► research_queries
   │
   ▼
[execute_search] SearchProvider ──► candidate URLs
   │
   ▼
[fetch_source]   URL fetched through the SSRF-guarded fetcher
   │             ├─ store raw text via StorageProvider
   │             └─ INSERT sources (url, publisher, published_date,
   │                                retrieved_at, content_hash, authority_tier)
   ▼
[extract_evidence]  LLM extracts spans, each labelled fact|claim|opinion|inference
   │
   ▼
[verify_locators]   MECHANICAL check: does quoted_text occur in stored source text?
   │                sets evidence.locator_verified
   ▼
[assemble_packet]   ResearchPacket: background, timeline, key facts, primary sources,
                    conflicting claims, unknowns, angles, viewer questions,
                    risky/unverified claims
```

**The `verify_locators` step is not optional and is not an LLM call.** It is a string/fuzzy match.
Evidence that fails it is retained with `locator_verified = false` and **cannot be cited**. This is
the mechanism that stops fabricated quotes from entering scripts — asking a model "is this quote
real?" is not a control.

**Failure mode that matters:** a source that 404s or paywalls yields no evidence rather than
evidence with no text. `sources.http_status` records what happened.

---

## Flow D — Opportunity generation and ranking

```
topic_clusters + competitor_video_scores + ResearchPacket + ChannelMemory
   │
   ▼
[generate_opportunities]  LLM proposes angles ──► opportunities
   │                      each MUST carry: why_now, differentiation,
   │                      supporting evidence links
   ▼
[reject_duplicates]  embedding similarity vs. previous channel videos
   │                 and vs. other queued opportunities → rejected before scoring
   ▼
[score_components]   nine independent scores ──► opportunity_scores (+ inputs jsonb)
   │
   ▼
[rank]  active ranking_model weights applied at READ time, not write time
   │
   ▼
Dashboard: ranked list with every component score visible and the evidence behind it
```

**Ranking is applied at read time.** Weights change often; scores should not be recomputed every
time a weight is tuned. Storing a baked ranking would make weight experiments destructive.

---

## Flow E — Script production (11 stages, one cached prefix)

```
ResearchPacket (stable)  ─────────────────────┐
                                              │  cache_control breakpoint
Stage-specific instruction (volatile) ────────┘
   │
   ├─ 1 packet → 2 angle → 3 outline → 4 claim map → 5 hook options
   ├─ 6 section drafts → 7 assembly → 8 fact verification
   └─ 9 retention edit → 10 language cleanup → 11 HUMAN REVIEW
   │
   ▼  each stage
INSERT script_versions (append-only, records prompt_version_id)
INSERT generations     (tokens, cache_read_tokens, latency, stop_reason)
INSERT cost_entries    (same transaction as the artifact)
   │
   ▼
[extract_claims] ──► claims + citations (citations only to locator_verified evidence)
   │
   ▼
[qc_passes] fact | structure | retention | repetition | language | promise | originality | slop
   │         ──► qc_results (passed, blocking, findings)
   ▼
SCRIPT_REVIEW → human → SCRIPT_APPROVED
                         guard: no HIGH-risk UNVERIFIED/CONFLICTING claim
```

The stable/volatile split is what makes eleven stages affordable: the research packet is written to
cache once and read ten times. A test asserts `cache_read_tokens > 0` on stages 2–11; if a prompt
edit breaks prefix stability, the test fails rather than the bill rising silently.

---

## Flow F — Production and render

```
SCRIPT_APPROVED
   │
   ├─[plan_beats]   script → story_beats (information_gained, open_loop, payoff)
   ├─[retention_qc] flags windows with no new information and no open loop
   ├─[plan_visuals] beats → shots (asset_type, license requirement)
   ├─[resolve_assets] match or acquire assets; every asset needs a license row
   └─[generate_voice] per-segment, cache key = (text_hash, voice_id, settings_hash)
   │
   ▼
[compile_timeline]  deterministic: shots + voice segments + captions + music
   │                → validated Pydantic Timeline → content hash → timelines row
   ▼
[render]  FFmpeg invoked with an ARGUMENT LIST built from the timeline
   │       no shell, no string interpolation of any LLM/user text
   ▼
renders (storage_key, file_hash)
   │
   ▼
EDIT_REVIEW → QC → READY
```

**Failure mode that matters:** an asset with no license row blocks the transition to `PRODUCTION`.
This is deliberate friction — an unlicensed asset discovered after publication is a strike, not a
bug report.

---

## Flow G — Publishing (idempotent)

```
READY → human schedules → SCHEDULED
   │
   ▼
[publish]  idempotency_key = hash(video_project_id, render_id, channel_id)
   │       INSERT publishing_jobs (UNIQUE idempotency_key) BEFORE any API call
   │
   ├─ if row exists with youtube_video_id ──────────► DONE, no upload
   ├─ if row exists with resumable_session_uri ─────► RESUME that upload
   └─ else ─► begin resumable upload, persist session URI IMMEDIATELY
   │
   ▼
on success: persist youtube_video_id in the SAME transaction that marks success
   │
   ▼
INSERT published_videos → schedule analytics checkpoints → MEASURING
```

See JOB_ARCHITECTURE.md §5 for why resumable upload — not retry — is the correct primitive here.

---

## Flow H — Analytics and learning

```
Beat scheduler, per published video, at 1h 6h 24h 48h 7d 14d 30d
   │
   ▼
[collect_analytics]  YouTube Analytics API (owned channel only)
   │                 metric not yet reported → NULL + name in metrics_unavailable
   ▼
analytics_snapshots (append-only, UNIQUE per video+checkpoint)
   │
   ▼
[recompute_baselines]  channel_baselines (median + MAD, window, sample_size)
   │                   sample_size < minimum → status='insufficient_data'
   ▼
[normalize]  relative_views / watch_time / ctr / retention vs. baseline
   │         ──► relative_performance
   ▼
[detect_patterns]  group by cluster / title_pattern / duration_bucket / hook style
   │               require minimum sample; compute effect size + confidence
   ▼
performance_insights   status = provisional | supported | retired
   │
   ▼
ChannelMemory (read interface)  ── filters to status='supported' ──► CREATIVE layer prompts
```

**The one-way valve:** provisional insights reach the human dashboard but never reach a prompt.
This is what prevents the system from confidently learning from three videos.

---

## Where every write happens

| Stage | Writes |
|---|---|
| ingestion | `competitor_videos`, `competitor_video_snapshots`, `ingestion_runs`, `quota_ledger` |
| clustering | `topic_clusters`, `competitor_videos.title_embedding` |
| scoring | `competitor_video_scores` |
| research | `research_runs`, `research_queries`, `sources`, `evidence` |
| ideation | `opportunities`, `opportunity_scores`, `opportunity_evidence` |
| scripting | `scripts`, `script_versions`, `claims`, `citations`, `qc_results` |
| production | `story_beats`, `visual_plans`, `shots`, `assets`, `voiceover_segments`, `timelines` |
| render | `render_jobs`, `renders` |
| publish | `publishing_jobs`, `published_videos` |
| analytics | `analytics_snapshots`, `channel_baselines`, `relative_performance` |
| learning | `performance_insights`, `insight_supporting_videos` |
| every LLM/voice/API call | `generations`, `cost_entries` |
| every state change | `video_state_transitions`, `audit_logs` |
