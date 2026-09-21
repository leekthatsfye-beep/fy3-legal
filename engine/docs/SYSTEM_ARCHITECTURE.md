# SYSTEM_ARCHITECTURE

FY3 Media Engine — research-driven YouTube content operating system.

Status: **architecture only. No implementation exists.** Phase 0 has not started.

---

## 1. What this system actually is

A pipeline that converts *observed public performance data* into *ranked, evidence-backed content
opportunities*, then converts an approved opportunity into a *sourced script*, then into a
*rendered video*, then measures the result and feeds it back.

The defensible asset is not the renderer. Anyone can render. The defensible asset is the
**accumulated, channel-specific record of what was tried, what it cost, and what it returned** —
stored in a way that supports statistical comparison rather than anecdote.

That framing drives every decision below. The two things that must never be compromised:

1. **Data integrity of the measurement layer.** A learning engine trained on fabricated,
   mis-normalized, or silently-imputed data is worse than no learning engine, because it produces
   confident wrong strategy.
2. **Editorial judgment stays human.** The system automates *effort*, not *decisions*.

---

## 2. Layer model

Four layers, as specified. Dependencies point downward only. No layer imports from a layer above it.

```
┌───────────────────────────────────────────────────────────────┐
│  INTERFACE           Next.js dashboard  ·  FastAPI HTTP API    │
├───────────────────────────────────────────────────────────────┤
│  INTELLIGENCE        ingestion · normalization · clustering    │
│                      outlier detection · research · scoring    │
├───────────────────────────────────────────────────────────────┤
│  CREATIVE            ideation · titles · scripts · claims      │
│                      visual planning · thumbnail concepts      │
├───────────────────────────────────────────────────────────────┤
│  PRODUCTION          voice · assets · timeline · render · QC   │
│                      publishing                                │
├───────────────────────────────────────────────────────────────┤
│  LEARNING            analytics · normalization · insights      │
│                      experiments · unit economics              │
├───────────────────────────────────────────────────────────────┤
│  PLATFORM            providers · jobs · storage · config       │
│                      logging · auth · cost metering            │
└───────────────────────────────────────────────────────────────┘
```

LEARNING sits below CREATIVE in the import graph but *logically* closes the loop: it writes
`performance_insights` rows, and CREATIVE **reads** them through a narrow query interface
(`ChannelMemory`) rather than importing learning code. This keeps the cycle out of the code graph.

### Decision: modular monolith, not microservices

**WHY.** One operator, one channel initially, media-heavy workloads.

**PROBLEM IT SOLVES.** Microservices would add network failure modes, distributed tracing needs,
schema-contract versioning, and N deployment targets — for zero throughput benefit at this scale.

**ALTERNATIVES CONSIDERED.**
- *Microservices per domain.* Rejected: operational cost dwarfs benefit; the bottleneck is
  FFmpeg CPU and third-party API quota, neither solved by splitting the app.
- *Serverless functions.* Rejected: renders run for minutes and need a big local scratch disk;
  cold starts and execution ceilings fight the workload.

**TRADEOFF ACCEPTED.** A single deploy unit means a bad migration can take down the whole app, and
the render worker scales together with the web tier unless we split process types. We mitigate by
running **the same image as multiple process types** (`api`, `worker-default`, `worker-render`,
`beat`), which gives independent scaling without independent codebases.

---

## 3. Process topology

| Process | Purpose | Scaling signal |
|---|---|---|
| `api` | FastAPI, HTTP only, no long work | request latency |
| `worker-default` | research, LLM calls, analytics pulls, scoring | queue depth |
| `worker-render` | FFmpeg + voice assembly, concurrency 1–2 | render queue depth, CPU |
| `beat` | Celery Beat scheduler, **exactly one instance** | n/a |
| `postgres` | system of record | disk, connections |
| `redis` | broker + cache + distributed locks | memory |

**Render is a separate queue and worker.** A 20-minute render must never occupy the worker that
also refreshes analytics, or the learning layer silently starves and snapshot checkpoints are
missed — which corrupts the time series permanently, because you cannot retroactively observe
"views at 24h".

---

## 4. The constraint that shapes everything: YouTube quota

The YouTube Data API v3 default allocation is **10,000 units/day per Google Cloud project**,
resetting at midnight Pacific. Costs that matter to us:

| Call | Cost | Note |
|---|---|---|
| `search.list` | **100 units** | ~100 calls/day exhausts the entire quota |
| `videos.list` | 1 unit | up to **50 video IDs per call** |
| `playlistItems.list` | 1 unit | up to 50 items per page |
| `channels.list` | 1 unit | |
| `videos.insert` (upload) | ~1600 units | verify before Phase 10 |

*(Per-method costs for `videos.insert`, `thumbnails.set`, and `commentThreads.list` are marked
**UNVERIFIED** — `developers.google.com` is blocked from this build environment. See
ARCHITECTURE_REVIEW.md finding R-01. The quota table is **configuration**, not constants in code,
so correcting a number is a config change, not a refactor.)*

### Decision: never use `search.list` for routine ingestion

**WHY.** Ingesting a competitor channel by search would cost 100 units per page and blow the daily
budget on a single channel.

**HOW INSTEAD.** Every YouTube channel has an **uploads playlist** (`channels.list` →
`contentDetails.relatedPlaylists.uploads`, 1 unit). Page it with `playlistItems.list` (1 unit per
50 videos), collect IDs, then hydrate statistics with `videos.list` batched **50 IDs at a time**
(1 unit per 50).

Ingesting a 500-video channel:
`1 (channels) + 10 (playlistItems) + 10 (videos) = 21 units` — versus ~1000+ via search.

**TRADEOFF ACCEPTED.** We lose keyword-based discovery of *new* channels. Discovery of new
competitors becomes a **deliberate, budgeted, human-initiated action** with its own quota envelope,
not a background process. That is the correct behavior anyway: adding a reference channel is an
editorial decision.

### Decision: quota is a reserved resource, not a post-hoc counter

Every YouTube call passes through a `QuotaBroker` that **reserves** units *before* the call and
settles actual cost after. Reservation uses an atomic Redis operation keyed by the Pacific-time
quota day.

**WHY.** A post-hoc counter races under concurrency: ten workers each check "we're at 9,900" and
all proceed. Reservation makes overspend structurally impossible rather than unlikely.

**TRADEOFF.** Reservations can leak if a worker dies mid-call. Mitigated with a TTL on the
reservation and a reconciliation job that compares reservations to settled costs.

---

## 5. The constraint nobody likes: competitor metrics are thin

Publicly available per-video data is limited to **view count, like count, comment count** and
metadata. Dislike counts were removed from the API in December 2021.

**Watch time, average view duration, retention curves, impressions, and CTR are NOT available for
channels you do not own.** They are first-party Analytics metrics only.

This has three hard consequences that the architecture must honor rather than paper over:

1. **Competitor "outlier detection" is a views-based signal only.** It can tell you a topic drew
   clicks. It cannot tell you the video was *good*. The DB column names reflect this
   (`views_per_day`, not `performance`).
2. **All retention/CTR learning is first-party and therefore cold-start blocked.** No insight about
   hooks, pacing, or thumbnail CTR can exist until *our own* videos have published and accumulated
   analytics. The Learning Engine must return `insufficient_data`, not a guess.
3. **A competitor's current view count is a single point-in-time observation.** You cannot
   reconstruct how fast it grew. Therefore competitor videos must be **snapshotted repeatedly over
   time** (`competitor_video_snapshots`) if we ever want real velocity rather than the crude
   `views / age` proxy. This is why ingestion is a recurring job, not a one-time import.

---

## 6. Outlier detection methodology

Required by spec §7 to be statistically defensible and documented. The naive
`video_views / channel_average_views` is rejected for three reasons: view distributions are
heavy-tailed and log-normal-ish (the mean is dragged by one viral video); view accumulation is
front-loaded and non-linear in age; and a channel's baseline drifts as it grows.

**Method (v1):**

1. Observe `views`, `published_at`, `observed_at`. Define `age_days = observed_at - published_at`.
2. Discard videos with `age_days < 7` from *baseline* computation (still scored, flagged
   `provisional`) — early velocity is dominated by subscriber notification, not topic demand.
3. Compute `y = ln(1 + views / age_days)`. The log transform makes the distribution roughly
   symmetric so location/scale statistics mean something.
4. Stratify by `(channel_id, age_bucket, topic_cluster_id)`. Age buckets:
   `7–30 / 31–90 / 91–365 / 365+ days`.
5. Within the stratum compute the **median** and **MAD** (median absolute deviation), not mean and
   standard deviation — a single 10M-view outlier must not inflate the scale and hide every other
   outlier.
6. `robust_z = 0.6745 * (y - median) / MAD`. (0.6745 makes MAD a consistent estimator of σ for
   normal data.)
7. **Minimum stratum size `n >= 8`.** Below that, fall back to the wider stratum
   `(channel_id, age_bucket)`, then `(channel_id)`, recording which level was used in
   `outlier_basis` and lowering `confidence` at each fallback. Below `n = 5` at the channel level,
   emit no score — `status = 'insufficient_data'`.
8. `MAD = 0` (common on small, uniform strata) is a degenerate case: fall back one level rather
   than dividing by zero.

Every `opportunity_scores` / outlier row stores `method_version`, `basis`, `n`, and `confidence`.
When the method changes, old scores are not silently reinterpreted — they are **recomputed under a
new `method_version`** and both are retained.

**TRADEOFF ACCEPTED.** Robust statistics are less efficient than parametric ones on well-behaved
data; we accept lower sensitivity in exchange for not being fooled by the one viral video that
dominates every channel's history.

---

## 7. Cost control architecture

Every provider call is wrapped by a **metered adapter** that writes a `cost_entries` row in the
*same database transaction* as the artifact it produced.

**WHY the same transaction.** If cost logging is a separate write, any crash between them produces
either an unbilled artifact or a phantom charge. Over months this silently destroys the unit
economics the whole business case rests on.

Budgets are enforced as **pre-flight reservations** at four scopes (daily / channel / provider /
video), same reasoning as the YouTube quota broker.

### Model routing and caching

Default model: **`claude-opus-5`**. Routing is *configuration*, not hardcoded, with per-task
overrides so a cheaper model is a deliberate measured choice rather than a default.

The dominant cost lever in this system is **not** model choice — it is that the multi-stage script
pipeline (spec §16, eleven stages) re-sends the same large research packet on every stage. That
packet is stable within a video project, so it is placed at the front of the prompt behind a
`cache_control` breakpoint; volatile per-stage instructions go after it. Cache effectiveness is
asserted in tests by checking `usage.cache_read_input_tokens > 0` across stages 2–11.

**TRADEOFF.** Caching pins prompt *ordering*, which constrains how prompts may be edited. Prompt
templates therefore declare an explicit `stable_prefix` / `volatile_suffix` split and a test fails
if a volatile value (timestamp, UUID) appears in the stable region.

---

## 8. Reproducibility of renders

Renders must be reproducible (spec §23). FFmpeg command strings scattered through application code
make that impossible and are also the system's sharpest security edge (§SECURITY_MODEL).

**Design.** A render is a pure function of a **Timeline** — a validated, versioned, content-hashed
JSON document referencing assets by content hash. The renderer's only inputs are the timeline and
the asset store. Same timeline hash + same asset hashes ⇒ same output bytes (modulo encoder
non-determinism, which we pin via explicit codec settings).

The LLM **never** produces FFmpeg arguments. It produces a `VisualPlan`, which a deterministic
compiler turns into a Timeline, which a deterministic builder turns into an argument *list*
(never a shell string).

---

## 9. What is deliberately NOT built early

Per spec §45 and the review in ARCHITECTURE_REVIEW.md, the following are designed but **not
implemented** before their phase: experiments, multi-channel tenancy, revenue/cost dashboards,
thumbnail generation, voice, rendering, publishing.

Phase 0–4 build the **intelligence loop only**, ending at a ranked opportunity a human approves.
That is the MVP per spec §46. If the intelligence loop does not produce opportunities a human
actually wants to make videos about, no amount of rendering automation matters.

---

## 10. Related documents

| Document | Covers |
|---|---|
| `DOMAIN_MODEL.md` | entities, aggregates, the video state machine, invariants |
| `DATABASE_SCHEMA.md` | tables, keys, indexes, constraints |
| `DATA_FLOW.md` | end-to-end pipelines and where data is written |
| `PROVIDER_INTERFACES.md` | adapter contracts for LLM, voice, platform, storage, research |
| `JOB_ARCHITECTURE.md` | queues, retries, idempotency, scheduling |
| `REPOSITORY_STRUCTURE.md` | directory layout and module boundaries |
| `SECURITY_MODEL.md` | secrets, injection surfaces, SSRF, FFmpeg safety |
| `TESTING_STRATEGY.md` | test tiers, fixtures, what is never mocked |
| `ARCHITECTURE_REVIEW.md` | findings against this design and what changed |
| `PHASE_0_PLAN.md` | exact first implementation sequence |
| `PHASE_1_PLAN.md` | projects/channels/dashboard shell |
| `PROJECT_STATE.md` | current build state — read this first each session |
