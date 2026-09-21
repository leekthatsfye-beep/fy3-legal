# ARCHITECTURE_REVIEW

Review of the design in this directory against spec §§1–48, performed before implementation as
required by spec §49. Findings are ordered by how much damage they do if left unaddressed.

Every finding is marked **RESOLVED** (the architecture above already reflects the fix),
**OPEN — BLOCKING** (must be settled before the named phase), or **ACCEPTED** (a known limitation
we are choosing to live with, recorded so it is a decision rather than a surprise).

---

## A. API limitations that change what the product can promise

### R-01 — Competitor retention, CTR and impressions are not obtainable. **ACCEPTED (and load-bearing)**

Spec §§7, 30, 31 read as though the system will learn what *structures* hold attention across the
competitive set. The public YouTube Data API exposes view count, like count, comment count and
metadata. Watch time, average view duration, retention curves, impressions and CTR are first-party
Analytics metrics for channels you own. Dislikes were removed from the API in 2021.

**Consequence.** Every competitor-derived signal is a *click/demand* signal, never a *quality* or
*retention* signal. "This topic got views" is knowable; "this video was good" is not.

**Fix applied.** Column names now state what they are (`views_per_day`, `robust_z`), not
`performance`. SYSTEM_ARCHITECTURE §5 states the limitation explicitly. All retention/CTR learning
is first-party and therefore gated on our own published videos.

**Why this matters more than it looks:** if it were papered over, the Learning Engine would present
retention conclusions derived from view counts, and the operator would make real editorial
decisions on a number that measures something else.

### R-02 — Several API facts are unverified in this environment. **OPEN — BLOCKING (per phase)**

`developers.google.com` is egress-blocked from this build environment. Confirmed via independent
sources: 10,000 units/day default, `search.list` = 100 units, `videos.list` = 1 unit with up to 50
IDs per call.

**Not confirmed:** quota cost of `videos.insert` and `thumbnails.set`; availability of
`impressions` / `impressionClickThroughRate` through the Analytics API; ElevenLabs model IDs,
character pricing and rate limits.

**Fix applied.** All quota costs and provider pricing are **configuration**
(`config/youtube_quota.toml`), never constants in code, so correcting a number is a config edit.
Unverified values are marked in PROVIDER_INTERFACES.md.

**Gate.** Phase 7 begins by confirming ElevenLabs facts; Phase 10 by confirming upload quota;
Phase 11 by confirming Analytics metric availability. **If impressions/CTR prove unavailable via
API, spec §26 (thumbnail/title performance pairing) and parts of §31 must be redesigned around
views-based signals or manual Studio export** — that redesign is cheaper to do now than after
building a dashboard that promises CTR.

### R-03 — Ingestion by search would exhaust quota immediately. **RESOLVED**

At 100 units per `search.list` call, the entire daily quota is ~100 searches. A naive
competitor-ingestion loop would die on its first channel.

**Fix applied.** `channels.list` → uploads playlist → `playlistItems.list` → `videos.list` batched
50 IDs. A 500-video channel costs ~21 units instead of 1000+. `search.list` is reserved for
deliberate, human-initiated, separately-budgeted discovery. Enforced in the adapter (batching is
internal, so a caller cannot accidentally make 50 single-ID calls) and tested (TESTING_STRATEGY
§3.4).

---

## B. Data integrity risks

### R-04 — "Fact checking" was specified in a way only an LLM could satisfy. **RESOLVED**

Spec §17 correctly says an LLM saying "looks correct" is not verification — but the original data
model (`sources` with claims pointing at them) provides no mechanism to do better. Nothing stops a
model from attributing a fabricated quote to a real URL.

**Fix applied.** An `evidence` table sits between sources and claims. Evidence stores the quoted
span plus a character locator, and `locator_verified` is set by a **mechanical substring/fuzzy
match against the stored source text** — not a model call. Evidence that fails cannot be cited, and
the citation insert is rejected. Sources store their retrieved text so this check is possible at
all. Tested in TESTING_STRATEGY §3.3.

### R-05 — Storing competitor `views` as a mutable column destroys velocity forever. **RESOLVED**

The spec's entity list implies statistics live on the video row. Updating them in place means each
re-ingestion overwrites the only evidence of how the video grew — and unlike most data loss, this
is **unrecoverable**, because the API only ever returns the current total.

**Fix applied.** `competitor_videos` holds immutable facts; `competitor_video_snapshots` is an
append-only time series. Ingestion is recurring specifically so this series accumulates. This is
also the only path to replacing the crude `views / age` proxy with measured velocity later.

### R-06 — `views/average_views` is not a defensible outlier method. **RESOLVED**

Spec §7 offers it but explicitly asks for something defensible. It fails because view
distributions are heavy-tailed (the mean is dragged by one viral video, so everything else looks
below average), accumulation is front-loaded and non-linear in age, and channel baselines drift.

**Fix applied.** Log transform, stratification by (channel, age bucket, cluster), **median and MAD**
rather than mean and standard deviation, robust z-score, explicit minimum stratum size with
documented fallback and confidence decay, and `status='insufficient_data'` rather than a guess.
Degenerate `MAD = 0` handled. Methodology documented in SYSTEM_ARCHITECTURE §6 and unit-tested
against hand-computed fixtures.

### R-07 — Zero and "not reported yet" would be conflated in analytics. **RESOLVED**

YouTube does not report every metric immediately, and revenue metrics lag. A schema with
`impressions bigint NOT NULL DEFAULT 0` would silently record "0 impressions" for every early
checkpoint and drag every average toward zero permanently.

**Fix applied.** All analytics metrics are nullable; unavailable metrics are NULL and named in
`metrics_unavailable text[]`. The row's existence records that we looked. Asserted directly in
tests.

### R-08 — The learning engine would draw confident conclusions from a handful of videos. **RESOLVED**

Spec §§31, 47 want learning, and the first milestone is 20–30 videos. Pattern detection over 20
videos split across clusters, durations and title patterns yields cells of n = 2. Every such
"insight" would be noise, and it would feed straight back into idea generation — a system
confidently learning from nothing.

**Fix applied.** `performance_insights` requires `sample_size`, `effect_size`, `confidence`,
`method_version`, and supporting video IDs. Below the configured minimum it is written
`status='provisional'`, and the `ChannelMemory` interface that feeds generation prompts filters to
`status='supported'`. **Provisional insights are visible to the human and invisible to the model.**
This one-way valve has a dedicated test.

### R-09 — Cost logging separate from artifact writes drifts. **RESOLVED**

A crash between "artifact saved" and "cost logged" produces unbilled artifacts or phantom charges.
Over months this destroys the unit economics the business case depends on.

**Fix applied.** Adapters return cost; the service layer persists `cost_entries` in the **same
transaction** as the artifact. Tested by asserting a rolled-back transaction leaves neither.

---

## C. Concurrency and failure modes

### R-10 — Post-hoc budget and quota checks race. **RESOLVED**

"Check remaining, then spend" is a classic TOCTOU: ten workers each see 100 units remaining and all
proceed. Under concurrency the daily cap is not a cap.

**Fix applied.** Both YouTube quota and spending budgets use **pre-flight atomic reservation** with
later settlement, plus a reconciliation job that releases leaked reservations from dead workers.
Tested with N parallel tasks against a budget for N−1.

### R-11 — Retrying a video upload can publish duplicates. **RESOLVED**

Upload is non-idempotent and expensive (~1600 units), and YouTube accepts no client-supplied
idempotency key. A retry after a timeout — where the upload may in fact have succeeded — is exactly
the case a naive retry policy handles worst, and the failure is publicly visible.

**Fix applied.** `publishing_jobs` with `UNIQUE (idempotency_key)` inserted **before any network
call**; **resumable upload sessions** with the session URI persisted before any media bytes are
sent; retry resumes rather than restarts. On session expiry the job **stops and asks a human**,
because when you cannot tell whether bytes landed, guessing is the wrong move. Detailed in
JOB_ARCHITECTURE §5.

### R-12 — A shared worker pool would silently corrupt the analytics time series. **RESOLVED**

A 20-minute render occupying the only worker delays the 24h analytics checkpoint. A missed
checkpoint cannot be collected later — "views at 24h" is gone forever.

**Fix applied.** Dedicated `render` queue at concurrency 1–2, separate `analytics` queue.
Checkpoints are computed from `published_at` and record real `collected_at`, so lateness is visible
in the data rather than silently shifting the series.

### R-13 — Silent state coercion. **RESOLVED**

A job that finds an entity in an unexpected state and "fixes" it is how pipelines corrupt
themselves invisibly.

**Fix applied.** Transitions are table-driven and validated; `status` is never set directly; a job
encountering an unexpected state **fails loudly**. Optimistic concurrency on `version` prevents two
workers advancing the same project. Property-tested for unreachable states.

---

## D. Security

### R-14 — The FFmpeg path violates "never execute LLM-generated commands" by default. **RESOLVED**

Spec §40 states the rule, and spec §§23–24 describe a pipeline where overlay text, captions and
paths all originate from model output and land in FFmpeg. `drawtext` filtergraph syntax treats
`:`, `'`, `\` and `,` as structure, so caption text inline is an injection vector even without a
shell.

**Fix applied.** No shell anywhere (argument lists only, `shell=False`, lint-enforced); the LLM
produces a `VisualPlan`, never arguments; overlay text is written to a **file** and referenced with
`textfile=`; asset paths are resolved from the database by `asset_id`, never model-supplied; scratch
paths are traversal-checked after `realpath`. Hostile-input tests in TESTING_STRATEGY §3.6.

### R-15 — The research fetcher is an SSRF hole by construction. **RESOLVED**

An LLM picks URLs from search results and an internal HTTP client fetches them. Hostname-only
validation is insufficient (DNS can resolve a public name to a private IP, and redirects can hop to
the cloud metadata endpoint at 169.254.169.254).

**Fix applied.** Scheme allowlist, **resolve-then-check-the-IP**, re-validation at every redirect
hop, connect-to-validated-IP with preserved `Host` to close the rebinding window, size/time caps,
no credentials on outbound fetches. Table-driven tests.

### R-16 — Prompt injection from fetched content. **ACCEPTED, with consequence reduced**

Fetched pages and competitor metadata are untrusted and are exactly what research prompts consume.
No known technique makes a model reliably immune.

**Approach.** Reduce likelihood (structural data/instruction separation, constrained structured
outputs), then reduce *consequence*, which is the part that actually holds: **content-processing
stages have no privileged tools** — no fetch, no publish, no spend, no database write. They return
a value and the caller decides. Human approval gates sit between any model output and any
irreversible action.

---

## E. Cost risks

### R-17 — The 11-stage script pipeline re-sends the research packet 11 times. **RESOLVED**

Spec §16's multi-stage pipeline is right for quality, and naively it multiplies input cost by ~11,
since the packet is the largest input and is needed at every stage. This, not model choice, is the
dominant cost driver in the system.

**Fix applied.** Prompt templates declare an explicit `stable_prefix` (the packet) and
`volatile_suffix` (stage instruction), with a cache breakpoint between them. A lint test fails if
anything volatile appears in the stable region — otherwise caching breaks silently and the only
symptom is a larger bill. A live smoke test asserts `cache_read_tokens > 0` on stages 2+.

### R-18 — "Use a cheaper model for cheap tasks" is the wrong first lever. **RESOLVED**

Spec §42 asks for task-specific model routing. Taken literally as model-downgrading, it fragments
the prompt cache, because caches are model-scoped — and cache reuse on the research packet is worth
more than the per-token delta on extraction calls.

**Fix applied.** Routing config exists as specified, but the **default lever is effort, not model**:
extraction and classification run the same model at low effort. Introducing a second model stays
possible and is a deliberate, measured change with an eval behind it. Judged on **cost per completed
task**, not cost per request — a cheaper call that needs three retries is not cheaper.

### R-19 — Whole-narration regeneration on a one-word edit. **RESOLVED**

Spec §22 calls for segment-based voice; without a schema-level cache key it degrades in practice to
regenerating everything.

**Fix applied.** `UNIQUE (video_project_id, text_hash, voice_id, settings_hash)` on
`voiceover_segments` — the constraint *is* the cache, so reuse cannot be forgotten. The
`VoiceProvider` interface has no whole-script method, making the expensive call unavailable rather
than merely discouraged.

---

## F. Platform and copyright risk

### R-20 — Mass-produced content is a monetization risk, not just a taste issue. **ACCEPTED, mitigated**

YouTube's monetization policies target mass-produced and repetitious content. A system whose
throughput exceeds its editorial judgment trends directly toward demonetization — and the
spec's own §43 says as much.

**Mitigations already in the architecture:** three non-bypassable human gates; originality QC with
mechanical near-duplicate rejection against both competitor titles and our own back catalogue;
saturation detection; explicit refusal to build a workflow around republishing others' footage
(spec §20). **The system is deliberately not capable of publishing without a human.**

**Residual risk is real and is the operator's to manage:** volume is a business decision the tool
should not make silently.

### R-21 — Title patterns could drift into copying. **RESOLVED**

"Learn what title structures work" becomes "reproduce competitor titles" without a hard stop.

**Fix applied.** `title_patterns` stores abstract categorical attributes and a generalized shape,
never a template string. `titles.max_competitor_similarity` is computed and used as a **rejection
gate in code** — a prompt instruction not to copy is not enforcement. `pg_trgm` plus embedding
similarity back the check.

### R-22 — Asset licensing would be tracked but not enforced. **RESOLVED**

A `license` column that nothing checks is decoration, and an unlicensed asset discovered after
publication is a copyright strike rather than a bug report.

**Fix applied.** `assets` are immutable and content-addressed with `derived_from_asset_id` for
edits; `licenses` is a real table; the transition into `PRODUCTION` is **blocked** if any shot's
asset lacks a license row. Deliberate friction at the right moment.

---

## G. Unnecessary complexity to remove or defer

### R-23 — The spec asks for ~35 tables and 15 phases up front. **RESOLVED by sequencing**

Building `experiments`, `finance`, multi-channel tenancy, and the executive dashboard before a
single video exists produces schema designed against imagined usage.

**Fix applied.** Phases 0–4 build the intelligence loop only, ending at a human-approved ranked
opportunity — spec §46's own MVP definition. `experiments`, `revenue_entries`, and the executive
dashboard are designed here but not built until Phases 12–13. `project_id` is carried on every table
from day one because *that* retrofit is expensive; everything else waits.

### R-24 — `users` / `project_members` are premature for one operator. **ACCEPTED, kept minimal**

Kept because the dashboard needs authentication regardless, and `project_id` scoping must exist from
the start. What is *not* built: SSO, RBAC beyond a single role column, invitations, org management.

### R-25 — Naming collision: `projects` vs `video_projects`. **ACCEPTED**

Two different things both called "project" is a readability hazard. The spec names both, so the
names are kept for traceability, with the domain glossary stating plainly: **`Project` = tenant
workspace; `VideoProject` = one video in production.** Revisit before the schema is public API.

---

## H. Missing components the spec did not mention

| Gap | Added |
|---|---|
| Quota accounting | `quota_ledger`, `QuotaBroker` with reservations |
| Evidence locator verification | `evidence.locator_verified` |
| Competitor velocity | `competitor_video_snapshots` |
| Baseline provenance | `channel_baselines` with `sample_size`, `method_version`, `status` |
| Score reproducibility | `opportunity_scores.inputs` (exact values consumed) |
| Prompt→performance linkage | `prompt_versions` + `generations.prompt_version_id` |
| Credential storage | `oauth_credentials` with envelope encryption + `key_version` |
| Render reproducibility | `timelines` with content hash and schema version |
| State auditability | `video_state_transitions` |
| Ingestion completeness | `ingestion_runs` (partial runs excluded from baselines) |
| Timezone correctness | `channels.timezone`; YouTube quota day is **US/Pacific** |
| Migration reversibility | CI runs `downgrade -1` on the newest revision |

**Also flagged as not yet designed, deliberately:** backup and restore procedure, data retention
policy, and a publishing kill switch. These are Phase 0 operational items rather than architecture,
listed in PHASE_0_PLAN.md so they are not forgotten.

---

## I. Verdict

The architecture is coherent enough to begin Phase 0.

**Three things must be settled before their phases begin**, and all three are external facts rather
than design questions:

1. **R-02 / Analytics metric availability** — before Phase 11 design is finalized. This one can
   change what the product promises.
2. **R-02 / upload quota cost** — before Phase 10.
3. **R-02 / ElevenLabs pricing and limits** — before Phase 7.

Everything else is either resolved in the design above or consciously accepted and written down.

**The single biggest risk is not technical.** It is R-20: building throughput faster than editorial
judgment. The architecture defends against it structurally — three non-bypassable human gates —
but no architecture can defend against an operator who decides to publish more than they can stand
behind.
