# DOMAIN_MODEL

Entities, aggregates, invariants, and the video state machine.

---

## 1. Aggregates

An **aggregate** is a consistency boundary: everything inside it is updated in one transaction, and
things outside it are referenced by ID only. This matters because several parts of this system
(render jobs, publishing, analytics) are eventually consistent with external services, and pretending
otherwise produces corruption.

| Aggregate | Root | Contains | Referenced by ID |
|---|---|---|---|
| Project | `Project` | settings, budgets, members | Channels |
| Channel | `Channel` | channel settings, monetization config, baselines | Competitors, Videos |
| CompetitorChannel | `CompetitorChannel` | competitor videos, snapshots | TopicCluster |
| ResearchRun | `ResearchRun` | sources, retrieved evidence | Topic |
| Opportunity | `Opportunity` | component scores, evidence links | TopicCluster, ResearchRun |
| VideoProject | `VideoProject` | titles, scripts, claims, beats, visual plan, renders, thumbnails | Opportunity, Assets |
| Asset | `Asset` | provenance, license, hash | — (shared, immutable) |
| PublishedVideo | `PublishedVideo` | analytics snapshots | VideoProject |
| Experiment | `Experiment` | variants, results | PublishedVideo |

**VideoProject is the big one** and deliberately so: a script, its claims, its beats, and its
visual plan must be mutually consistent. Editing a script invalidates downstream beats. That
invalidation is only enforceable inside one aggregate.

**Asset is immutable and shared.** Assets are content-addressed by hash. Two video projects
referencing the same stock clip reference the same `Asset` row. An asset is never edited — a
modified asset is a *new* asset with a `derived_from_asset_id`. This is what makes provenance and
license tracking actually hold up.

---

## 2. Core entities

### Project
Top-level ownership boundary. Everything is scoped by `project_id`. Even in single-operator mode
this column exists on every major table, and the repository layer **requires** a project scope on
every query. Retrofitting tenancy later is a migration across 35 tables; carrying one column now
is free.

### Channel
One of *our* YouTube channels. Holds OAuth credential reference (not the token itself — see
SECURITY_MODEL), style configuration, caption style, voice config, monetization config, and the
computed performance baselines.

### CompetitorChannel / CompetitorVideo / CompetitorVideoSnapshot
Reference channels we observe. `CompetitorVideo` holds immutable facts (id, title, published_at,
duration). `CompetitorVideoSnapshot` holds the **time-varying observations** (views, likes,
comments, observed_at).

**This split is load-bearing.** Storing `views` on the video row and updating it in place destroys
the only way to ever measure growth velocity for content we do not own. Snapshots are append-only.

### TopicCluster / Topic
Clusters emerge from embedding the corpus and clustering it — they are not a hardcoded taxonomy.
`TopicCluster` carries the aggregate statistics (`median_views_per_day`, `outlier_rate`,
`upload_density_30d`, `momentum`). `Topic` is a specific subject that may belong to a cluster.

### TitlePattern
An **abstract structure** extracted from titles, never a copied string. Stored as a set of
categorical attributes (length bucket, question/statement, has_number, has_named_entity,
curiosity_gap, contrarian, urgency, promise_type) plus a generalized shape.

**Invariant:** the system must never emit a generated title whose normalized edit distance to any
observed competitor title falls below a configured threshold. Enforced in the Title Lab as a
rejection rule, not a prompt instruction — prompt instructions are not enforcement.

### ResearchRun / Source / Evidence
A `ResearchRun` is one bounded research operation for a topic. It produces `Source` rows (URL,
publisher, publication date, retrieved_at, content hash, stored text) and `Evidence` rows
(a specific extracted span with a locator into the source text).

**Invariant:** evidence cannot exist without a source whose stored text actually contains the
quoted span. This is checked mechanically (substring/fuzzy match), not by asking a model whether it
looks right. This is the single most important integrity rule in the CREATIVE layer.

### Claim / Citation
A `Claim` is an assertion in a script. `verification_status` ∈
`VERIFIED | CONFLICTING | UNVERIFIED | OPINION | INFERENCE`.
`risk` ∈ `LOW | MEDIUM | HIGH`.

**Invariant:** a `VideoProject` cannot transition to `SCRIPT_APPROVED` while any claim has
`risk = HIGH` and `verification_status ∈ {UNVERIFIED, CONFLICTING}`. This is a state-machine guard
backed by a database check at transition time, not a UI warning.

### VisualPlan / StoryBeat / Shot
Beats are the retention unit. Each beat declares `information_gained`, `open_loop`, and
`payoff_of_beat_id`. The Retention QC pass walks the beat sequence and flags any window longer
than a configured duration with no new information and no open loop — mechanically checkable,
unlike "is this boring".

### Timeline / RenderJob / Render
`Timeline` is the versioned, content-hashed render input. `RenderJob` is the execution attempt.
`Render` is the output artifact. One timeline hash may have many render jobs (retries) but should
produce one logical render.

### PublishedVideo / AnalyticsSnapshot
`AnalyticsSnapshot` is append-only, one row per (video, checkpoint, metric set). Never updated.
`checkpoint` ∈ `1h | 6h | 24h | 48h | 7d | 14d | 30d`.

**Invariant:** snapshots record `collected_at` and `metrics_available`. If YouTube has not yet
computed a metric, the row stores `NULL` with the metric named in `metrics_unavailable` — it never
stores 0. Zero and "not yet reported" are different facts and conflating them poisons every
downstream average.

### PerformanceInsight
A learned statement. **Cannot be created without** `sample_size`, `confidence`, `time_period`,
`metric`, `effect_size`, and the list of `supporting_video_ids`. An insight below the configured
minimum sample size is stored with `status = 'provisional'` and is **excluded from generation
prompts** — it is visible to the human on the dashboard but never fed back into idea generation.

---

## 3. Video state machine

```
                    ┌──────────────────────────────────────┐
                    ▼                                      │
  IDEA ──► RESEARCHING ──► RESEARCHED ──► VALIDATING ──► APPROVED_IDEA
                                                              │
                                                              ▼
                                                          SCRIPTING
                                                              │
                                                              ▼
                              NEEDS_REVISION ◄──────── SCRIPT_REVIEW
                                    │                         │
                                    └──────────┐              ▼
                                               │      SCRIPT_APPROVED
                                               │              │
                                               │              ▼
                                               └────────► PRODUCTION
                                                              │
                                                              ▼
                                                        EDIT_REVIEW
                                                              │
                                                              ▼
                                                             QC
                                                              │
                                                              ▼
                                                            READY
                                                              │
                                                              ▼
                                                         SCHEDULED
                                                              │
                                                              ▼
                                                         PUBLISHED
                                                              │
                                                              ▼
                                                         MEASURING
```

Terminal / lateral states reachable from most states: `REJECTED`, `FAILED`, `PAUSED`.

### Transition rules

Transitions are declared in one table in code (`domain/video_state.py`) and validated on every
write. There is no path that sets `status` directly.

| From | To | Guard |
|---|---|---|
| `IDEA` | `RESEARCHING` | budget reservation succeeds |
| `RESEARCHING` | `RESEARCHED` | ≥1 `ResearchPacket` exists with ≥ N sources |
| `RESEARCHED` | `VALIDATING` | — |
| `VALIDATING` | `APPROVED_IDEA` | **human action**; not duplicate of prior channel video; saturation acknowledged |
| `VALIDATING` | `REJECTED` | human action, reason required |
| `APPROVED_IDEA` | `SCRIPTING` | — |
| `SCRIPTING` | `SCRIPT_REVIEW` | all script stages complete; QC passes have run |
| `SCRIPT_REVIEW` | `SCRIPT_APPROVED` | **human action** AND no HIGH-risk unverified claim |
| `SCRIPT_REVIEW` | `NEEDS_REVISION` | human action, notes required |
| `NEEDS_REVISION` | `SCRIPTING` | — |
| `SCRIPT_APPROVED` | `PRODUCTION` | voice + visual plan resolvable; all assets licensed |
| `PRODUCTION` | `EDIT_REVIEW` | a `Render` exists for the current timeline hash |
| `EDIT_REVIEW` | `QC` | human action |
| `QC` | `READY` | all blocking QC checks pass |
| `QC` | `NEEDS_REVISION` | any blocking QC check fails |
| `READY` | `SCHEDULED` | **human action**; publish target set |
| `SCHEDULED` | `PUBLISHED` | publishing job confirmed a YouTube video ID |
| `PUBLISHED` | `MEASURING` | first analytics snapshot scheduled |

**Three transitions require a human and cannot be automated in any configuration:**
`APPROVED_IDEA`, `SCRIPT_APPROVED`, `SCHEDULED`. These are the editorial gates. There is no
config flag to bypass them. If they were bypassable they would eventually be bypassed.

### Invariants enforced at transition

1. Every transition writes a `video_state_transitions` row: from, to, actor (user or job), reason,
   timestamp. The current `status` column is a denormalized cache of the latest transition.
2. A job may never skip a state. A job that finds the project in an unexpected state **fails
   loudly** and does not proceed. Silent state coercion is how pipelines corrupt themselves.
3. `version` is incremented on every mutation; writes use optimistic concurrency
   (`WHERE version = :expected`). Two workers cannot both advance the same project.

---

## 4. Cross-cutting fields

Every major entity carries:

| Field | Purpose |
|---|---|
| `id` UUID | stable identity, safe to expose |
| `project_id` UUID | tenancy scope, enforced at repository layer |
| `created_at` / `updated_at` | timestamptz, UTC |
| `status` | entity-specific enum |
| `version` int | optimistic concurrency |
| `metadata` JSONB | genuinely variable data **only** |

### On JSONB discipline

JSONB is used for exactly three things: provider raw responses (`provider_payload`), genuinely
open-ended config (`channel_settings.style`), and component score breakdowns whose keys evolve.

It is **not** used for anything we will filter, sort, join, or aggregate on. If a field is queried,
it is a column. The rule of thumb applied throughout `DATABASE_SCHEMA.md`: *if a human will ever
ask "show me all X where this field is Y", it is a column.*

---

## 5. What the domain layer must not know

The domain layer must not import: `httpx`, `celery`, `fastapi`, `sqlalchemy`, or any provider SDK.
It contains entities, value objects, state rules, and scoring math — things that are testable with
no I/O and no containers. This is what makes the scoring methodology unit-testable against
hand-computed fixtures, which is the only way to know it is correct.
