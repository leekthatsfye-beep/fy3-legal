# TESTING_STRATEGY

The purpose of the test suite here is narrower than usual: this system's output is **decision
support**, so the tests that matter most are the ones proving the *math* and the *data integrity*
are right. A beautifully tested HTTP layer over wrong statistics is worthless.

---

## 1. Tiers

| Tier | Scope | Dependencies | Target runtime |
|---|---|---|---|
| **unit** | domain math, state machine, scoring, timeline compiler | none | < 10 s |
| **integration** | services + repositories + jobs | real Postgres + Redis, fake providers | < 3 min |
| **contract** | adapter parsing vs. recorded provider responses | recorded fixtures | < 30 s |
| **e2e** | one pipeline slice through the API | full compose stack | < 10 min |
| **frontend** | components + dashboard flows | Vitest + Testing Library, Playwright for flows | < 2 min |

---

## 2. What is never mocked

**Postgres is never mocked or replaced by SQLite.** This schema uses native enums, JSONB, arrays,
partial unique indexes, and pgvector. SQLite silently accepts things Postgres rejects, so a green
suite on SQLite proves nothing about production. Integration tests run against a real Postgres
container with the real migrations applied.

**Migrations are never bypassed in tests.** The test database is built by running Alembic from
zero, every time. This is what catches the migration that works on an empty database and fails on
one with data.

**Providers are faked, not mocked.** `tests/fakes/` contains real objects implementing the same
Protocol — a `FakeLLMProvider` that returns canned structured responses, a `FakeYouTubeProvider`
with an in-memory channel. Because they satisfy the Protocol, a signature change breaks them under
`mypy` immediately. `unittest.mock.patch` on provider internals is not used; it passes happily
against an interface that no longer exists.

---

## 3. The tests that carry the most weight

### 3.1 Scoring math (unit)

Hand-computed fixtures with known answers:

- `robust_z` on a known distribution matches a hand-calculated value.
- A single 10M-view outlier **does not** move the median/MAD-based score of its peers — this is
  the entire reason for choosing robust statistics, so it is asserted directly.
- `MAD = 0` falls back one stratum level instead of dividing by zero.
- `n < 8` falls back and lowers `confidence`; `n < 5` at channel level yields
  `status = 'insufficient_data'` and **no score**.
- Videos younger than 7 days are excluded from baselines but still scored as `provisional`.

### 3.2 State machine (unit)

- Every legal transition is exercised; every illegal transition raises.
- **Property test:** for random sequences of transition attempts, the entity never reaches a state
  not reachable by a legal path from `IDEA`.
- The three human-only gates (`APPROVED_IDEA`, `SCRIPT_APPROVED`, `SCHEDULED`) reject an
  `actor_type='job'` transition under every configuration. There is no config that enables them.
- `SCRIPT_APPROVED` is refused while any HIGH-risk claim is `UNVERIFIED` or `CONFLICTING`.

### 3.3 Data integrity (integration)

- An analytics metric that YouTube does not report is stored as **NULL with the metric named in
  `metrics_unavailable`** — asserted explicitly, because storing 0 is the silent-corruption bug
  this schema exists to prevent.
- `competitor_video_snapshots` is append-only: a second ingestion of the same video adds a row and
  does not mutate the first.
- A failed `ingestion_run` is excluded from baseline computation.
- Evidence whose `quoted_text` does not occur in the stored source text gets
  `locator_verified = false` and **cannot be cited** — the citation insert is rejected.
- A `performance_insight` below minimum sample size is `provisional` and does **not** appear in the
  `ChannelMemory` query that feeds prompts. This is the one-way valve; it gets a dedicated test.

### 3.4 Cost and quota (integration)

- Every provider call writes exactly one `cost_entries` row, **in the same transaction** as its
  artifact. A rolled-back transaction leaves neither.
- Budget reservation is atomic under concurrency: N parallel tasks against a budget for N−1 result
  in exactly one rejection, not N−1 successes and a negative balance.
- `get_videos` with 120 IDs makes **3** YouTube calls, not 120 — this guards a 40× quota
  regression that would otherwise surface only as a mysteriously exhausted daily quota.
- The quota broker refuses to reserve past the daily cap; the task defers rather than failing.

### 3.5 Prompt caching (integration, against fakes + one live smoke)

- Rendering a prompt template produces a byte-identical `stable_prefix` across stages.
- A lint test fails if a volatile token (timestamp, UUID, per-request ID) appears in a template's
  stable region.
- A **live smoke test**, run manually and in nightly CI only, asserts `cache_read_tokens > 0` on
  script stages 2+. Cost regressions here are invisible without this test.

### 3.6 Render safety (unit)

- The FFmpeg argument builder is called with hostile overlay text — `'`, `:`, `\`, `,`, `%`,
  newlines, a `drawtext` filter fragment — and the assertion is that the text reaches a **file**
  and never the argument vector, and that the argument list is unchanged in structure.
- No code path constructs an FFmpeg invocation as a string. Asserted by an AST lint rule over
  `fy3/rendering/` rejecting `shell=True` and string-built commands.
- A path resolving outside the scratch root raises before any process starts.

### 3.7 Idempotency (integration)

- Publishing the same `(video_project, render, channel)` twice produces **one** `publishing_jobs`
  row and **zero** second uploads.
- A simulated crash after `resumable_session_uri` is persisted resumes rather than restarting.
- Re-running a completed script stage with the same `input_hash` and `prompt_version_id` returns
  the cached artifact and writes no new `cost_entries` row.

### 3.8 SSRF (unit)

Table-driven against the guard: `localhost`, `127.0.0.1`, `169.254.169.254`, `10.x`, `192.168.x`,
`[::1]`, a public hostname that resolves to a private IP, and a public URL that redirects to a
private one. All rejected. Public URLs allowed.

---

## 4. What is NOT tested with assertions on model output

LLM output is non-deterministic; asserting on its text produces flaky tests that get deleted.

Instead:
- **Schema conformance** is asserted (structured outputs make this meaningful).
- **Rejection rules** are asserted against fixtures — a generated title too similar to a competitor
  title must be rejected; a claim with no verified evidence must not be citable. These are
  deterministic code paths fed by canned model output.
- **Quality** is measured by a separate **eval suite**, not the unit test suite. Evals are scored
  runs against a fixed input set with recorded results, run deliberately, and cost money. They
  gate prompt changes; they do not gate every commit.

Keeping evals out of CI is deliberate: a test suite that costs money and fails randomly stops being
run.

---

## 5. Fixtures

- Factories (`factory_boy`) for domain entities; no shared mutable fixture state.
- Recorded provider responses under `tests/contract/fixtures/`, **scrubbed of keys and tokens**,
  with the recording date in the filename so staleness is visible.
- A seeded corpus of ~200 synthetic competitor videos with known statistical properties, used to
  test clustering and outlier detection end to end. Synthetic, because it must have *known* answers
   — this is the one place fabricated data is correct, and it lives only in `tests/`.
- Every test runs in a transaction rolled back at teardown; no cross-test leakage.

---

## 6. CI gates

Merge is blocked unless all pass:

1. `ruff` + `ruff format --check`
2. `mypy --strict` on `fy3.domain` and `fy3.providers`; standard elsewhere
3. `import-linter` layer contracts (REPOSITORY_STRUCTURE.md §3)
4. unit + integration + contract suites
5. `alembic upgrade head` from empty, then `alembic downgrade -1` on the newest revision —
   catches irreversible migrations before they are irreversible in production
6. `pip-audit`, `npm audit`, secret scan
7. Coverage: **≥ 90 % on `fy3/domain/`**, ≥ 70 % overall. The domain bar is high because that is
   where the math lives; a blanket high number elsewhere would just incentivize testing getters.

---

## 7. Definition of done for a module

From spec §44, as a checklist applied per module before it is called complete:

1. runs · 2. tests pass · 3. types valid · 4. migration applies **and reverses** · 5. errors handled
and classified · 6. structured logging present · 7. inputs validated · 8. configuration documented
in `.env.example` · 9. no fake data in production paths · 10. no TODO hiding required behavior
· 11. no duplicated business logic · 12. API contract documented · 13. retry/failure behavior
defined · 14. security implications considered · 15. replaceable without breaking unrelated modules.

Anything unfinished is marked unfinished in `PROJECT_STATE.md`. Functionality is never described as
existing when it does not.
