# PROJECT_STATE

**Read this first, every session.** It is the single source of truth for what actually exists.

Last updated: 2026-09-21

---

## Current state

| | |
|---|---|
| **Phase** | Pre-Phase-0 |
| **Architecture** | Complete and reviewed |
| **Implementation** | **None. No application code exists.** |
| **Next action** | PHASE_0_PLAN.md step 0.1 |

There is no backend, no frontend, no database, no Docker setup. `engine/` contains documentation
only. Nothing in these documents describes working software — they describe software to be built.

---

## What exists

```
engine/docs/
├── SYSTEM_ARCHITECTURE.md    layers, topology, quota strategy, outlier methodology
├── DATABASE_SCHEMA.md        full schema, ~40 tables
├── DOMAIN_MODEL.md           aggregates, invariants, video state machine
├── DATA_FLOW.md              eight end-to-end pipelines
├── PROVIDER_INTERFACES.md    LLM, embedding, voice, platform, search, storage
├── JOB_ARCHITECTURE.md       queues, idempotency, retries, publishing resume
├── REPOSITORY_STRUCTURE.md   layout, enforced module boundaries
├── SECURITY_MODEL.md         secrets, SSRF, FFmpeg injection, prompt injection
├── TESTING_STRATEGY.md       tiers, the tests that carry weight, CI gates
├── ARCHITECTURE_REVIEW.md    25 findings, resolutions, accepted risks
├── PHASE_0_PLAN.md           14 steps to a running skeleton
└── PHASE_1_PLAN.md           projects, channels, quota visibility
```

---

## Open blocking questions

These are external facts, not design decisions. Each blocks a specific phase.

| # | Question | Blocks | Why it matters |
|---|---|---|---|
| R-02a | Are `impressions` / `impressionClickThroughRate` available via the YouTube Analytics API? | Phase 11 | If not, thumbnail/title CTR learning (spec §26, §31) must be redesigned around views-based signals or manual Studio export |
| R-02b | Exact quota cost of `videos.insert` and `thumbnails.set` | Phase 10 | Upload is the largest single quota consumer; publishing cadence depends on it |
| R-02c | ElevenLabs model IDs, character pricing, rate limits | Phase 7 | Voice is a per-character recurring cost; unit economics need the real number |

`developers.google.com` is egress-blocked from the build environment used to write these documents,
so these could not be confirmed here. **Confirm against live documentation at the start of the
relevant phase.** Do not implement against assumed values.

---

## Decisions already made (do not relitigate without cause)

- Modular monolith, multiple process types. Not microservices.
- Postgres + pgvector. Not a separate vector database.
- **Never `search.list` for routine ingestion** — uploads playlist + batched `videos.list`.
- Robust statistics (median/MAD, log-transformed, stratified) for outlier detection. Not
  views/average.
- Competitor statistics are an **append-only snapshot series**, never mutable columns.
- Analytics metrics are **nullable**; unavailable ≠ zero.
- Claims cite `evidence` whose locator is **mechanically verified**, never LLM-attested.
- Insights below minimum sample size are `provisional` and are **never fed to prompts**.
- Default model `claude-opus-5`; **effort is the cost lever before model choice**.
- Publishing uses **resumable upload sessions**, not retry-with-dedup.
- FFmpeg via argument lists only; overlay text via `textfile=`; no shell anywhere.
- Three human gates (`APPROVED_IDEA`, `SCRIPT_APPROVED`, `SCHEDULED`) are **not bypassable**.

---

## Accepted limitations

- Competitor retention, CTR and impressions are **not obtainable**. Competitor signals measure
  demand, never quality.
- All retention/CTR learning is first-party and blocked until our own videos publish and accumulate
  data.
- The Learning Engine will return `insufficient_data` for a long time. That is correct behavior.
- Prompt injection cannot be eliminated; consequence is reduced by denying privileged tools to
  content-processing stages.

---

## Where the engine lives

Currently `engine/` inside `fy3-legal`, which also serves the FY3 Beats GitHub Pages site
(`index.html`, `privacy.html`, `tos.html`, TikTok verification files) at the repository root.
**Those files must not move** — they are referenced by platform API reviews.

A root `.nojekyll` keeps Pages serving the static files verbatim rather than processing this
Markdown through Jekyll.

**Recommended:** split `engine/` into its own repository (`git subtree split -P engine`) before
Phase 0 implementation begins. Nothing in the layout assumes a repository root.

---

## Session log

| Date | Session | Outcome |
|---|---|---|
| 2026-09-21 | Architecture | 11 required documents + architecture review written. 25 findings; 19 resolved in design, 3 open-blocking (external facts), 3 accepted. No code written — correct per spec §49. |
