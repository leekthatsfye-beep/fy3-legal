# REPOSITORY_STRUCTURE

---

## 1. Where this code should live

**Recommendation: FY3 Media Engine belongs in its own repository.**

It currently sits under `engine/` in `fy3-legal`, which is a three-page static GitHub Pages site
serving the TikTok/Instagram API verification files, Terms of Service, and Privacy Policy for
FY3 Beats. Those pages must keep resolving at their current URLs — a platform API review failing
because a legal URL moved is a real, avoidable outage.

The two have nothing in common: different languages, different deploy targets, different release
cadence, and very different blast radius.

**Interim arrangement (in effect now):** the engine lives under `engine/`, the static site is
untouched at the repository root, and a root `.nojekyll` file keeps GitHub Pages serving the
existing files verbatim rather than running the engine's Markdown through Jekyll.

**Migration when ready:** `git subtree split -P engine -b engine-only` into a new
`fy3-media-engine` repository. Nothing in the layout below assumes a repository root, so the move
is mechanical.

---

## 2. Layout

```
fy3-legal/
├── index.html  privacy.html  tos.html  tiktok*.txt   # GitHub Pages site — DO NOT MOVE
├── .nojekyll                                          # serve static files verbatim
├── CLAUDE.md                                          # session operating rule (spec §50)
└── engine/
    ├── docs/                       # architecture — the 11 documents + review + state
    ├── docker-compose.yml
    ├── Dockerfile
    ├── Makefile
    ├── .env.example
    │
    ├── backend/
    │   ├── pyproject.toml
    │   ├── alembic.ini
    │   ├── migrations/versions/
    │   ├── config/
    │   │   ├── youtube_quota.toml        # quota costs — config, not constants
    │   │   ├── llm_routing.toml
    │   │   └── scoring.toml              # weights, thresholds, minimum sample sizes
    │   ├── prompts/                      # versioned prompt templates, git-tracked
    │   │   └── <name>/v<N>.md
    │   └── src/fy3/
    │       ├── main.py                   # FastAPI app factory
    │       ├── settings.py               # typed config (pydantic-settings)
    │       ├── logging.py                # structlog setup
    │       ├── db/                       # engine, session, base
    │       │
    │       ├── domain/                   # PURE. no I/O, no SDKs, no ORM.
    │       │   ├── entities/
    │       │   ├── video_state.py        # the state machine + transition guards
    │       │   ├── scoring/              # outlier math, opportunity scores
    │       │   └── errors.py
    │       │
    │       ├── repositories/             # SQLAlchemy; project-scoped queries only
    │       ├── services/                 # business logic; orchestrates domain + repos
    │       │
    │       ├── providers/                # ONLY place vendor SDKs may be imported
    │       │   ├── base.py               # Protocols, TraceContext, error classes
    │       │   ├── llm/         claude.py
    │       │   ├── embedding/
    │       │   ├── voice/       elevenlabs.py
    │       │   ├── platform/    youtube.py  quota_broker.py
    │       │   ├── research/    search.py  fetch.py  ssrf.py
    │       │   └── storage/     local.py
    │       │
    │       ├── jobs/                     # Celery app, tasks, schedules, sweepers
    │       ├── rendering/                # timeline model, compiler, ffmpeg arg builder
    │       ├── api/                      # routers — THIN. no business logic.
    │       │   ├── deps.py
    │       │   └── routers/
    │       └── observability/            # metering, audit, trace context
    │
    ├── frontend/                         # Next.js + TypeScript + Tailwind
    │   └── src/{app,components,lib}/
    │
    └── tests/
        ├── unit/          # domain math, state machine — no containers
        ├── integration/   # real Postgres + Redis, fake providers
        ├── contract/      # recorded provider responses vs. adapter parsing
        ├── fakes/         # real fake providers, not mocks
        └── conftest.py
```

---

## 3. Module boundary rules (enforced, not advisory)

```
api  →  services  →  domain
          ↓            ↑
     repositories ─────┘
          ↓
     providers  →  vendor SDKs
```

Enforced in CI with `import-linter` contracts:

| Contract | Rule |
|---|---|
| `domain-is-pure` | `fy3.domain` may not import `sqlalchemy`, `fastapi`, `celery`, `httpx`, `anthropic`, or any adapter |
| `providers-are-isolated` | vendor SDKs importable only under `fy3.providers` |
| `api-is-thin` | `fy3.api` may not import `fy3.repositories` or `fy3.providers` directly |
| `layered` | no upward imports |

**WHY enforce mechanically.** Every one of these boundaries erodes under deadline pressure, and
erosion is invisible in review. A failing CI contract is the only reliable enforcement.

---

## 4. Why business logic is not in routes

A route handler that contains logic cannot be called from a Celery task, and this system runs the
*same* operations from both HTTP and jobs (score an opportunity, generate a script stage). Putting
logic in services means there is exactly one implementation with one set of tests.

---

## 5. Prompts on disk, registered in the database

Prompt templates are Markdown files at `backend/prompts/<name>/v<N>.md` — git-tracked, diffable,
reviewable. On startup each is hashed and registered in `prompt_versions`; every generation records
`prompt_version_id`.

**WHY both.** Files alone cannot be joined against performance data. Database rows alone are not
reviewable in a pull request. Files are the source of truth; the table is the index that makes
"did prompt v4 outperform v3?" answerable.

**Invariant:** a template declares `stable_prefix` and `volatile_suffix` regions. A test fails if
anything volatile (timestamp, UUID, per-request value) appears in the stable region, because that
silently destroys prompt caching and inflates cost with no visible symptom.

---

## 6. File size

No hard line limit. The real rule: **one module, one reason to change.** A file that mixes
HTTP handling, business rules, and SQL is wrong at 80 lines. A state-machine table is fine at 400.
If a module needs a section comment to navigate, it should have been two modules.
