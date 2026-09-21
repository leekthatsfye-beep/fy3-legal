# JOB_ARCHITECTURE

Celery 5 over Redis. Postgres holds job *state*; Redis is transport and locks only.

---

## 1. Why Postgres holds job state

Celery's result backend is not a system of record: results expire, and Celery's own task state is
not queryable alongside domain data. Every job therefore has a `system_jobs` row created **before**
the task is enqueued.

**WHY.** So that "what is this video waiting on?" is one SQL query, and so a job that never reached
a worker is visible as `queued` forever rather than invisible.

**ALTERNATIVE.** Rely on Celery's backend + Flower. Rejected: Flower shows tasks, not domain
context, and an expired result is indistinguishable from a job that never ran.

**TRADEOFF.** Two writes per job (row + enqueue) and a possible inconsistency window between them.
Mitigated by enqueueing **after commit** (`transaction.on_commit` equivalent) so a rolled-back
transaction never leaves a phantom task, accepting instead the rarer failure of a committed row
whose enqueue was lost — which the sweeper detects as `queued` past its SLA and re-enqueues.

---

## 2. Queues

| Queue | Workload | Concurrency | Time limit |
|---|---|---|---|
| `default` | scoring, clustering, bookkeeping | 4–8 | 5 min |
| `ingest` | YouTube reads, quota-bounded | 2 | 10 min |
| `llm` | research, scripting, QC | 4 | 30 min |
| `voice` | ElevenLabs synthesis | 2 | 15 min |
| `render` | FFmpeg | **1–2** | 4 h |
| `analytics` | snapshot collection | 2 | 10 min |
| `beat` | scheduler only | 1 | — |

**Render is isolated at concurrency 1–2.** Two concurrent FFmpeg jobs on one box contend for CPU
and scratch disk and make both slower while risking OOM. More importantly, a saturated shared
worker pool delays analytics checkpoints, and a missed "views at 24h" observation is
**unrecoverable** — you cannot go back and measure it later.

**`ingest` is concurrency 2 on purpose.** Quota is the constraint, not parallelism; more workers
just contend for reservations.

---

## 3. Idempotency

Every task is one of:

- **Naturally idempotent** — upserts keyed on a natural key (ingestion snapshots are append-only
  but keyed by `(video, ingestion_run)`; scoring writes a versioned row).
- **Guarded by an idempotency key** — `system_jobs.idempotency_key UNIQUE`. A duplicate enqueue
  loses the insert race and exits.
- **Resumable** — upload. See §5.

**Generative tasks (LLM, voice) are keyed on content hashes**, not on "has this run before":
- voice: `(text_hash, voice_id, settings_hash)` — the DB unique constraint *is* the cache.
- LLM script stages: keyed on `(script_id, stage, input_hash, prompt_version_id)`. A retry after a
  crash re-uses the completed stage instead of re-billing it.

---

## 4. Retry policy

Retries are driven by the **error class** the adapter returns, never by exception-message matching.

| Class | Behavior |
|---|---|
| `Retryable` | exponential backoff + jitter, max 3 |
| `RateLimited(retry_after)` | respect `retry_after` exactly; does not consume an attempt |
| `QuotaExhausted` | **do not retry**; re-schedule for next quota day, status `deferred` |
| `AuthExpired` | refresh once; on failure, fail loudly and alert — never loop |
| `Permanent` | fail immediately, record `failure_reason`, no retry |

**Jitter is mandatory.** Without it, N tasks failing on the same upstream outage retry in lockstep
and re-create the outage.

**Errors are never swallowed.** A task that catches an exception must either re-raise, or record a
`failure_reason` and transition the domain entity to `FAILED`. A bare `except: pass` fails code
review and is caught by a lint rule.

---

## 5. Publishing: resume, do not retry

Uploading a rendered video is the one operation where a naive retry is genuinely destructive — a
duplicate public upload is visible to the audience and costly in quota.

**Design:**

1. `publishing_jobs` row inserted with `UNIQUE (idempotency_key)` **before any network call**.
   `idempotency_key = hash(video_project_id, render_id, channel_id)`.
2. Start a **resumable upload session**; persist `resumable_session_uri` immediately, in its own
   committed transaction, before sending any media bytes.
3. On retry, the task branches:
   - `youtube_video_id` present → done, return.
   - `resumable_session_uri` present → **query the session's committed byte offset and resume**.
   - neither → start fresh.
4. On success, persist `youtube_video_id` in the same transaction that marks the job succeeded.

**WHY resumable rather than retry-with-dedup.** There is no way to set a client-supplied unique ID
on a YouTube upload, so post-hoc deduplication would mean listing recent uploads and guessing by
title and duration — ambiguous, quota-costly, and wrong at exactly the moment it matters. Resumable
upload makes the question unnecessary.

**TRADEOFF.** Session URIs expire. On expiry the job requires human confirmation before starting a
fresh upload, because the safe action when you cannot tell whether bytes landed is to ask.

**A publishing job never auto-retries more than once without human acknowledgement.**

---

## 6. Scheduled jobs (Celery Beat)

| Schedule | Job | Notes |
|---|---|---|
| hourly | `plan_ingestion` | sizes work to remaining quota |
| hourly | `collect_due_analytics` | drives the checkpoint grid |
| daily | `recompute_baselines` | after the analytics day settles |
| daily | `recompute_cluster_stats` | |
| daily | `detect_patterns` | writes insights, most stay provisional |
| daily | `reap_stale_jobs` | `running` past SLA → `failed` |
| daily | `reconcile_quota_ledger` | unsettled reservations → released |
| weekly | `refresh_competitor_backfill` | deeper historical pass |

**Exactly one beat instance.** Two schedulers double every job. Enforced by a Redis lock held by
the beat process, so a misconfigured second instance exits rather than duplicating work.

### The analytics checkpoint grid

Checkpoints are computed from `published_at`, not from "N hours after the last run". A job that
runs late collects the checkpoint it was *due* for and records the real `collected_at`, so lateness
is visible in the data rather than silently shifting the time series.

A checkpoint that cannot be collected (API error, metric not yet available) is **retried within a
bounded window and then written with NULLs and `metrics_unavailable` populated** — the row's
existence records that we looked.

---

## 7. Long jobs and progress

No HTTP request waits on a job. The API enqueues and returns a job ID; the dashboard polls
`GET /jobs/{id}` or subscribes to updates.

Render reports progress by parsing FFmpeg's `-progress` output into `render_jobs.progress`. This is
the one place FFmpeg output is parsed, and it is parsed as key/value pairs from a pipe, not scraped
from stderr text.

---

## 8. Observability contract

Every task, on every execution, logs a structured start and end event carrying: `job_id`,
`celery_task_id`, `task_name`, `queue`, `attempt`, `project_id`, `request_id`, `duration_ms`,
`outcome`, and on failure `error_class` + `failure_reason`.

After any failure it must be answerable from the database alone — without log search — what failed,
when, why, which input, which provider, and whether it retried. That requirement is why
`system_jobs`, `generations`, `cost_entries`, and `audit_logs` all carry `job_id`.

---

## 9. Failure modes explicitly designed for

| Failure | Behavior |
|---|---|
| Worker killed mid-render | `render_jobs` reaped to `failed`; scratch dir cleaned; timeline unchanged so retry is deterministic |
| Redis flush | jobs lost from queue; `system_jobs` rows remain `queued` and are re-enqueued by the sweeper |
| Postgres failover | tasks fail `Retryable`; no partial domain writes because every task commits once at the end |
| YouTube quota exhausted mid-ingest | run marked `deferred`, resumes next quota day from its page cursor |
| LLM refusal (`stop_reason == "refusal"`) | recorded on `generations`, surfaced to human, **not** retried in a loop |
| Provider returns malformed JSON | `Permanent`; artifact not written; raw payload retained for debugging |
| Two workers grab the same video project | optimistic concurrency on `version` — one wins, the other fails cleanly |
