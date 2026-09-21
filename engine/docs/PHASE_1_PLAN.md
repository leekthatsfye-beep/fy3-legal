# PHASE_1_PLAN

**Goal:** projects, channels, and a dashboard shell that reflects real database state.

**Prerequisite:** every Phase 0 exit checklist item is checked.

**Definition of done:** an operator can create a project, register their own YouTube channel by ID,
register competitor reference channels, and see them in the dashboard — with quota accounting live
and observable. **No ingestion, no analysis, no LLM calls yet.**

**Why this shape:** Phase 1 exists to make the quota broker real against a live API on the cheapest
possible calls (`channels.list` = 1 unit) before Phase 2 spends quota in volume. Discovering a
broken quota broker during bulk ingestion is far more expensive than discovering it here.

---

## Step sequence

### 1.1 — Domain entities
- `Project`, `Channel`, `ChannelSettings`, `CompetitorChannel` as pure domain objects.
- The `VideoStatus` enum and the **complete transition table**, even though no video exists yet.
- **Verify:** full state-machine unit tests including the property test for unreachable states, and
  the three human-only gates rejecting `actor_type='job'`. Writing this now means later phases
  inherit a proven state machine instead of growing one ad hoc.

### 1.2 — Migration 002
- `channels`, `channel_settings`, `channel_baselines`, `competitor_channels`,
  `oauth_credentials`, `quota_ledger`, `ingestion_runs`.
- **Verify:** upgrade/downgrade both clean.

### 1.3 — Credential encryption
- Envelope encryption for `oauth_credentials.ciphertext` with `key_version`.
- Decryption only inside provider adapters, never logged.
- **Verify:** a test asserting ciphertext differs from plaintext, that a wrong key fails closed, and
  that a decrypted token never appears in captured log output.

### 1.4 — QuotaBroker
- Atomic Redis reservation keyed by **US/Pacific** quota day (not UTC — getting this wrong shifts
  the reset by up to 8 hours and produces mystifying exhaustion).
- `reserve` / `settle` / `release`; `quota_ledger` rows; `reconcile_quota_ledger` beat job.
- **Verify:** concurrent reservations never exceed the cap; leaked reservations are released;
  a quota-day boundary test using a frozen clock across the Pacific midnight.

### 1.5 — YouTubeProvider, read-only subset
- `get_channel` (`channels.list`, 1 unit) and `list_uploads` only. **No** `search.list`.
- Quota cost per method read from `config/youtube_quota.toml`.
- Error classification: 403 quota → `QuotaExhausted`; 429 → `RateLimited`; 404 → `Permanent`.
- **Verify:** contract tests against recorded responses (scrubbed, dated). One manual live smoke
  call against a real channel ID, run deliberately, confirming the quota ledger settles.

### 1.6 — Services and API
- `ProjectService`, `ChannelService`, `CompetitorService`.
- Registering a channel fetches and caches `uploads_playlist_id` — 1 unit once, saving a call on
  every future ingestion.
- Routers: projects, channels, competitors. **Thin** — no logic.
- **Verify:** integration tests through the service layer with `FakeYouTubeProvider`; authz test
  proving cross-project access is refused.

### 1.7 — Dashboard shell
- Layout, navigation, project switcher.
- Channels page: our channels + competitors, with `last_ingested_at`.
- **Quota widget: units used today, units remaining, reset time in Pacific.** Surfaced from day one
  because quota is the binding constraint on everything that follows, and an invisible constraint
  gets violated.
- Jobs page: `system_jobs` with status and failure reasons.
- **Verify:** component tests; one Playwright flow creating a project and adding a competitor.

### 1.8 — Audit logging
- Every create/update writes `audit_logs` with actor, before/after, `request_id`.
- **Verify:** integration test asserting a channel update produces an audit row with correct
  before/after.

---

## Phase 1 exit checklist

- [ ] Operator can create a project and register our channel and competitors
- [ ] `uploads_playlist_id` cached at registration
- [ ] QuotaBroker live, correct across the Pacific day boundary, reconciled
- [ ] Quota consumption visible in the dashboard
- [ ] OAuth credentials encrypted at rest; decryption never logged
- [ ] State machine fully implemented and property-tested
- [ ] Audit log populated on every mutation
- [ ] Cross-project access refused, proven by test
- [ ] `PROJECT_STATE.md` updated

---

## Explicitly deferred

| Deferred | To phase |
|---|---|
| Bulk competitor ingestion | 2 |
| Outlier detection | 2 |
| Clustering, embeddings | 3 |
| Research engine | 3 |
| Opportunity scoring | 4 |
| Any LLM call | 3 |
| Voice, render, publish, analytics | 7–11 |

**Phase 2 is the first phase that spends quota in volume.** It does not start until the quota broker
has been proven correct in production use here.
