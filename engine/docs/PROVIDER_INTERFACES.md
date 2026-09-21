# PROVIDER_INTERFACES

Every external dependency sits behind an interface in `providers/`. Application code depends on the
interface; the concrete adapter is selected by configuration and injected.

**Rule:** no module outside `providers/` may import a vendor SDK. Enforced by an import-linter
contract in CI (see TESTING_STRATEGY.md §6), not by convention.

---

## 1. Why interfaces here and not everywhere

Interfaces are worth their cost when there is a *realistic* second implementation or when the real
thing cannot run in tests. Both are true for every provider below: each one costs money per call,
each has rate limits, and each is a plausible swap target.

Interfaces are **not** introduced for Postgres (SQLAlchemy is already the abstraction) or for
internal services. Over-abstracting internals is the failure mode this avoids.

---

## 2. LLMProvider

```python
class LLMProvider(Protocol):
    async def complete(
        self,
        *,
        prompt: RenderedPrompt,        # stable_prefix + volatile_suffix, already rendered
        task: TaskKind,                # routing key: reasoning | extraction | classification | ...
        schema: type[BaseModel] | None = None,   # structured output
        trace: TraceContext,
    ) -> LLMResult: ...
```

`LLMResult` carries `content`, `parsed`, `model`, `input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`, `stop_reason`, `latency_ms`, `request_id`,
`estimated_cost_usd`.

### ClaudeProvider

Default model **`claude-opus-5`** for every task unless a routing override is configured.

Behavioral requirements the adapter must satisfy:

- **Adaptive thinking** (`thinking={"type": "adaptive"}`) for reasoning-heavy tasks. `budget_tokens`
  is rejected by current models and must not appear in the code.
- **Effort** via `output_config.effort` is the cost lever, not model downgrade. Classification and
  extraction default to lower effort before they default to a smaller model.
- **Streaming** for any long generation, using the SDK's final-message helper.
- **Prompt caching** on `stable_prefix`. The adapter must surface `cache_read_tokens` so the
  effectiveness test can assert on it.
- **`stop_reason` is checked before reading content.** A refusal is an outcome, not an exception —
  it is recorded on `generations` and surfaced to the human, never retried blindly into a loop.
- Structured output via `output_config.format`, never assistant prefill (rejected by current models).

**Routing table lives in configuration:**

```toml
[llm.routing]
default          = { model = "claude-opus-5", effort = "high" }
reasoning        = { model = "claude-opus-5", effort = "xhigh" }
script_draft     = { model = "claude-opus-5", effort = "high" }
extraction       = { model = "claude-opus-5", effort = "low" }
classification   = { model = "claude-opus-5", effort = "low" }
```

**Decision: route by effort first, model second.** Lowering effort on the strong model is measured
before introducing a second model, because a cascade fragments the prompt cache (caches are
model-scoped) and the research packet is the largest repeated input in this system. Cheaper models
remain configurable, but switching one is a deliberate, measured change with an eval behind it —
not a default.

---

## 3. EmbeddingProvider

```python
class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str], *, trace: TraceContext) -> list[Vector]: ...
    @property
    def dimensions(self) -> int: ...
    @property
    def model_id(self) -> str: ...
```

`model_id` is persisted alongside every stored vector. **Vectors from different models are not
comparable**; mixing them silently corrupts clustering and the duplicate-rejection gate. A model
change forces a re-embed under a new `method_version`.

---

## 4. VoiceProvider

```python
class VoiceProvider(Protocol):
    async def synthesize(
        self, *, text: str, voice_id: str, settings: VoiceSettings, trace: TraceContext
    ) -> VoiceResult: ...
    async def list_voices(self) -> list[VoiceDescriptor]: ...
```

### ElevenLabsProvider

- **Segment-level only.** The interface takes one segment; there is no whole-script call. This is
  what makes the `(text_hash, voice_id, settings_hash)` cache usable and stops a one-word edit from
  re-billing the entire narration.
- Pronunciation dictionary applied as a pre-processing step inside the adapter.
- Cost recorded in characters; `cost_entries.unit = 'characters'`.
- Audio normalized to a target loudness inside the adapter so downstream mixing is predictable.
- Retries are bounded and idempotent — the cache key means a retry that succeeds twice writes once.

**UNVERIFIED:** exact ElevenLabs model IDs, character pricing, and rate limits are not confirmed in
this environment. They are configuration values, and Phase 7 begins with confirming them against
live docs. See ARCHITECTURE_REVIEW.md R-02.

---

## 5. VideoPlatformProvider

```python
class VideoPlatformProvider(Protocol):
    async def get_channel(self, channel_id: str) -> ChannelInfo: ...
    async def list_uploads(self, uploads_playlist_id: str, page: str | None) -> UploadsPage: ...
    async def get_videos(self, video_ids: Sequence[str]) -> list[VideoInfo]: ...   # max 50
    async def begin_upload(self, req: UploadRequest) -> ResumableSession: ...
    async def resume_upload(self, session: ResumableSession) -> UploadResult: ...
    async def set_thumbnail(self, video_id: str, asset: AssetRef) -> None: ...
    async def get_analytics(self, req: AnalyticsRequest) -> AnalyticsResult: ...
```

### YouTubeProvider

Two distinct capabilities behind one adapter, with very different permissions:

| Capability | API | Scope |
|---|---|---|
| competitor observation | Data API v3 | API key, public data only |
| own-channel analytics | Analytics API | OAuth, owner-only |
| publishing | Data API v3 | OAuth, owner-only |

**`get_videos` enforces the 50-ID batch limit in the adapter**, chunking internally. A caller
cannot accidentally make 50 one-ID calls and burn 50× the quota.

**Every method declares its quota cost** and goes through the `QuotaBroker`. Costs come from
configuration (`config/youtube_quota.toml`), not constants, because some are unverified.

**`get_analytics` returns explicit unavailability.** `AnalyticsResult.unavailable: list[str]` names
metrics YouTube did not report. The adapter must never substitute 0.

**UNVERIFIED:** availability of `impressions` and `impressionClickThroughRate` through the Analytics
API, and the exact quota cost of `videos.insert` / `thumbnails.set`, could not be confirmed here
(Google developer docs are egress-blocked from this environment). Phase 10/11 opens with confirming
these. If impressions/CTR turn out to be unavailable via API, the thumbnail-performance parts of
the Learning Engine degrade to views-based signals only — which changes what §26 and §31 can
promise. This is tracked as R-01 and must be resolved before Phase 11 design is finalized.

---

## 6. SearchProvider / FetchProvider

```python
class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int, trace: TraceContext) -> list[SearchHit]: ...

class FetchProvider(Protocol):
    async def fetch(self, url: str, *, trace: TraceContext) -> FetchedDocument: ...
```

Deliberately two interfaces, because search and retrieval are separable and often different
vendors. `FetchProvider` implementations **must** go through the SSRF guard (SECURITY_MODEL §4):
scheme allowlist, DNS resolution checked against private/link-local ranges, redirect chain
re-validated at every hop, response size cap, timeout.

No provider is named in the architecture. Research quality depends on source authority, which is
evaluated per-provider during Phase 3 with a small benchmark rather than chosen up front.

---

## 7. StorageProvider

```python
class StorageProvider(Protocol):
    async def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredObject: ...
    async def get(self, key: str) -> BinaryIO: ...
    async def url(self, key: str, *, expires_in: int | None = None) -> str: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
```

`LocalStorageProvider` first. `S3StorageProvider`, `DropboxStorageProvider`,
`GoogleDriveStorageProvider` designed for but not built.

Keys are content-addressed: `{project}/{kind}/{hash[:2]}/{hash}`. Content addressing makes `put`
idempotent and deduplication automatic — re-uploading identical bytes is a no-op.

**Note:** the operator already keeps source footage and image assets in Dropbox, so
`DropboxStorageProvider` is the most likely second adapter and the interface is shaped to
accommodate it (no assumption of byte-range reads or of S3-style presigned semantics).

---

## 8. Cross-cutting adapter requirements

Every adapter, without exception:

1. **Accepts a `TraceContext`** (request_id, job_id, project_id) and includes it in every log line.
2. **Writes a `cost_entries` row in the caller's transaction.** The adapter returns cost; the
   service layer persists it atomically with the artifact.
3. **Records the raw provider response** (or its hash plus key metadata) in `generations` /
   `provider_payload` for debugging.
4. **Classifies errors** into `Retryable` / `Permanent` / `RateLimited(retry_after)` /
   `QuotaExhausted` / `AuthExpired`. The job layer reacts to the class, never to a string match.
5. **Never retries a non-idempotent write blindly.** Publishing and upload have explicit resume
   semantics instead.
6. **Has a fake implementation** in `tests/fakes/` that satisfies the same Protocol and is used by
   every integration test. Fakes are not `unittest.mock` — they are real objects with real behavior,
   so a signature change breaks them at type-check time.
