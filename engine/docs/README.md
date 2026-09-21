# FY3 Media Engine — Documentation

Research-driven YouTube content operating system.

**Status: architecture only. No implementation exists.** See `PROJECT_STATE.md`.

## Reading order

**Every session starts with `PROJECT_STATE.md`.** It says what actually exists.

1. `PROJECT_STATE.md` — current build state, open questions, decisions already made
2. `SYSTEM_ARCHITECTURE.md` — the shape of the system and why
3. `ARCHITECTURE_REVIEW.md` — what is wrong with the obvious design, and what we did instead
4. `DOMAIN_MODEL.md` — entities, invariants, state machine
5. `DATABASE_SCHEMA.md` — tables
6. `DATA_FLOW.md` — how data moves
7. `PROVIDER_INTERFACES.md` / `JOB_ARCHITECTURE.md` / `REPOSITORY_STRUCTURE.md`
8. `SECURITY_MODEL.md` / `TESTING_STRATEGY.md`
9. `PHASE_0_PLAN.md` — the next thing to build

## The short version

The system discovers content opportunities from public competitor data, researches them against
real sources, scores them on nine independent dimensions, produces evidence-backed scripts, and —
after a human approves — produces and publishes video, then measures results and feeds them back.

The advantage is not "AI makes videos fast." It is the accumulated, channel-specific record of
what was tried, what it cost, and what it returned.

Three constraints shape everything:

1. **YouTube quota is 10,000 units/day** and `search.list` costs 100 of them. Routine ingestion
   never searches.
2. **Competitor retention and CTR are not obtainable.** Competitor data measures demand, not
   quality. Everything about retention is first-party and cold-start blocked.
3. **Editorial judgment stays human.** Three gates cannot be bypassed by any configuration.
