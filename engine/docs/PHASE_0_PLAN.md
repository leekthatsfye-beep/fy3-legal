# PHASE_0_PLAN

**Goal:** a skeleton that runs, tests, migrates, logs, and meters — with *zero* business logic.

**Definition of done:** `make up` starts the stack; `make test` passes; a trivial end-to-end slice
(HTTP → service → repository → Postgres, and HTTP → enqueue → worker → job row) works; a metered
fake provider writes a `cost_entries` row atomically with its artifact.

**Non-goals:** no YouTube, no Claude, no ElevenLabs, no FFmpeg, no scoring, no dashboard beyond a
health page. Phase 0 exists to make Phase 1+ cheap and to prove the boundaries hold.

---

## Step sequence

Each step ends with a commit that leaves the suite green. Do not start the next step with a red
suite.

### 0.1 — Repository and tooling
- `engine/backend/pyproject.toml` — Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic,
  Celery, redis, structlog, httpx, pytest, ruff, mypy, import-linter.
- `Makefile`: `up`, `down`, `test`, `lint`, `typecheck`, `migrate`, `revision`, `fmt`.
- `.env.example` with every variable named and empty. `.env` git-ignored.
- **Verify:** `make lint` and `make typecheck` pass on an empty package.

### 0.2 — Typed configuration
- `settings.py` using `pydantic-settings`. Fail **at startup** on a missing required variable —
  never at first use, halfway through a job.
- `config/` TOML loaders for `youtube_quota`, `llm_routing`, `scoring`, each parsed into a typed
  model.
- **Verify:** unit test asserting startup raises on a missing required variable, and that the
  quota table loads with the confirmed values (search.list=100, videos.list=1).

### 0.3 — Docker Compose
- Services: `postgres:16` (with `pgcrypto`, `vector`, `pg_trgm`), `redis:7`, `api`, `worker-default`,
  `worker-render`, `beat`. One `Dockerfile`, multiple commands.
- Images pinned **by digest**.
- **Verify:** `make up` reaches healthy; `psql` confirms all three extensions.

### 0.4 — Structured logging
- `structlog`, JSON output, with a **redaction processor** dropping secret-shaped fields (§SECURITY
  MODEL §2). Redaction is a processor, not a call-site discipline.
- `TraceContext` (request_id, job_id, project_id); FastAPI middleware generates/propagates
  `request_id`.
- **Verify:** a test asserting a log call containing an API-key-shaped value emits it redacted.

### 0.5 — Database foundation + first migration
- SQLAlchemy 2 declarative base, session factory, `Base` mixins (`id`, timestamps, `version`).
- Alembic configured; **first migration** creates extensions, enums, `users`, `projects`,
  `project_members`, `audit_logs`, `system_jobs`, `cost_entries`.
- **Verify:** `alembic upgrade head` from empty **and** `alembic downgrade -1` both succeed in CI.
  Reversibility is checked from the very first migration, because that habit does not appear later.

### 0.6 — Repository layer with the tenancy guard
- `BaseRepository` whose every query method **requires** `project_id`. No unscoped helper exists.
- **Verify:** a test proving there is no code path to an unscoped query; a type-level test that
  omitting `project_id` fails `mypy`.

### 0.7 — Provider base and the metered adapter pattern
- `providers/base.py`: `TraceContext`, error classes (`Retryable`, `Permanent`,
  `RateLimited`, `QuotaExhausted`, `AuthExpired`), `ProviderResult` carrying cost.
- A `FakeMeteredProvider` plus the service-layer helper that persists the artifact and its
  `cost_entries` row **in one transaction**.
- **Verify:** integration test — rolled-back transaction leaves neither artifact nor cost row.
  This is the pattern every real provider will copy, so it is proven before any real provider
  exists.

### 0.8 — Budget reservation
- `BudgetBroker` with atomic Redis reservation, TTL, settle, and release.
- Scopes: daily / project / provider / video.
- **Verify:** N concurrent tasks against a budget for N−1 → exactly one rejection. Run it with real
  concurrency, not sequentially.

### 0.9 — Celery skeleton
- Celery app, queues per JOB_ARCHITECTURE §2, `system_jobs` row created **before** enqueue,
  enqueue **after commit**.
- Retry policy driven by error class; jitter mandatory.
- `reap_stale_jobs` sweeper; beat with a single-instance Redis lock.
- **Verify:** a task that raises `Retryable` retries with backoff and lands `failed` with a
  `failure_reason` after max attempts; a task enqueued in a rolled-back transaction never runs.

### 0.10 — FastAPI skeleton
- App factory, health endpoint, error handlers mapping domain errors to HTTP codes,
  `GET /jobs/{id}`.
- Auth scaffolding: argon2id hashing, session cookie. One seeded user via a CLI command, not a
  fixture in code.
- **Verify:** e2e — enqueue a trivial job over HTTP, poll it to completion.

### 0.11 — Import-linter contracts
- The four contracts in REPOSITORY_STRUCTURE §3, wired into CI.
- **Verify:** deliberately add a forbidden import, confirm CI fails, remove it.

### 0.12 — Frontend shell
- Next.js + TypeScript + Tailwind; one page showing API health and job status. Nothing else.
- **Verify:** `npm run build` and `npm test` pass.

### 0.13 — CI pipeline
- All seven gates from TESTING_STRATEGY §6.
- **Verify:** a deliberately failing commit is blocked by each gate in turn.

### 0.14 — Operational items flagged in review §H
- `make backup` / `make restore` against the Postgres container, documented in `engine/README.md`.
- Data retention note: what is kept, for how long, what is purgeable.
- **Publishing kill switch:** a config flag checked by the publishing service that hard-fails all
  publish attempts. Built in Phase 0 even though publishing arrives in Phase 10 — a kill switch
  added after the thing it disables has a habit of not existing when needed.

---

## Phase 0 exit checklist

- [ ] `make up` → healthy stack, three extensions present
- [ ] `make test` green: unit + integration against real Postgres/Redis
- [ ] `alembic upgrade head` from empty **and** `downgrade -1` pass in CI
- [ ] Layer contracts enforced and proven to fail on violation
- [ ] Secrets redacted in logs, verified by test
- [ ] Budget reservation atomic under real concurrency
- [ ] Cost row written atomically with its artifact
- [ ] Job enqueued only after commit; stale jobs reaped
- [ ] `.env.example` complete; startup fails loudly on missing config
- [ ] Backup/restore documented and exercised once
- [ ] Publishing kill switch exists and is tested
- [ ] `PROJECT_STATE.md` updated

**Do not proceed to Phase 1 with any box unchecked.** Every one of these is a foundation that gets
disproportionately expensive to add later.
