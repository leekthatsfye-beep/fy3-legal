# SECURITY_MODEL

This system holds OAuth tokens that can publish to a revenue-generating YouTube channel, spends
money through third-party APIs on autopilot, fetches attacker-influenced web content, and passes
model-generated text into a media pipeline. Each of those is a distinct threat surface.

---

## 1. Threat model

| Asset | Threat | Consequence |
|---|---|---|
| YouTube OAuth refresh token | exfiltration | attacker publishes to / deletes from the channel |
| Anthropic / ElevenLabs keys | exfiltration | unbounded billing |
| Research fetcher | SSRF | internal network access from the app host |
| Model output → FFmpeg | command / filtergraph injection | arbitrary process execution |
| Fetched web content | prompt injection | model follows attacker instructions |
| Render scratch disk | path traversal | arbitrary file write |
| Publishing endpoint | CSRF / broken authz | unauthorized upload |

---

## 2. Secrets

**Never in frontend code, never in the repository, never in a client-reachable API response.**

- Provider API keys: environment variables only, loaded through typed settings. `.env.example`
  lists names with empty values; `.env` is git-ignored.
- OAuth refresh tokens: **envelope-encrypted at rest** in `oauth_credentials.ciphertext` using a
  key from the environment, with `key_version` stored per row so rotation is possible without a
  flag day.
- A token is decrypted only inside the provider adapter, at the moment of use, and never logged.
- Structured logging runs a redaction processor that drops any field whose name matches a secret
  pattern and truncates any value matching a key-like shape. **Redaction is a logger processor, not
  a discipline** — relying on every call site to remember is how keys end up in logs.
- Secret scanning runs in CI on every push.

**The frontend never holds a provider credential.** All provider calls originate server-side. The
dashboard authenticates to our own API and nothing else.

---

## 3. Authentication and authorization

- Session-based auth for the dashboard; passwords hashed with **argon2id**.
- Every repository method takes a `project_id` and filters on it. There is no unscoped query helper.
  This is the tenancy guard, and it lives in the repository layer because a service that forgets to
  filter is an ordinary mistake, while a repository that cannot express an unscoped query is a
  structural defense.
- Expensive endpoints (anything that triggers LLM, voice, render, or publish) are rate-limited per
  user and additionally gated by the budget reservation system — an authenticated user still cannot
  spend past the configured budget.
- Publishing requires a re-confirmation step; it is never a side effect of another action.

---

## 4. SSRF defense in the research fetcher

The research engine fetches URLs chosen by an LLM from search results. That is attacker-influenced
input pointed at an HTTP client running inside our network.

Every fetch goes through one guarded client:

1. **Scheme allowlist** — `http`, `https` only. No `file:`, `gopher:`, `ftp:`, `data:`.
2. **Resolve DNS first, then check the resolved IPs** against denied ranges: loopback, private
   (RFC1918), link-local (169.254.0.0/16 — this is the cloud metadata range), unique-local, and
   reserved. Checking the hostname string is not sufficient: DNS can resolve a public-looking name
   to 127.0.0.1.
3. **Re-validate on every redirect hop.** A public URL that 302s to `169.254.169.254` defeats a
   check performed only on the initial URL. Redirects are followed manually with the same guard
   applied each hop, capped at 5.
4. **Connect to the validated IP** with the `Host` header preserved, closing the DNS-rebinding
   window between the check and the connection.
5. **Caps** — response size, total time, and content-type allowlist. A 4 GB response must not be
   able to fill the disk.
6. No credentials, cookies, or internal headers are ever attached to an outbound research fetch.

---

## 5. FFmpeg: the sharpest edge

Spec §40 says never execute LLM-generated shell commands. In a video pipeline that rule is easy to
violate without noticing, because overlay text, file paths, and filter parameters all flow from
model output into FFmpeg.

**Controls:**

1. **No shell, ever.** FFmpeg is invoked with an **argument list** (`subprocess.exec` semantics,
   `shell=False`). No `os.system`, no `shell=True`, no f-string command construction. Enforced by a
   lint rule.
2. **The LLM never produces FFmpeg arguments.** It produces a `VisualPlan`. A deterministic
   compiler turns that into a validated `Timeline`; a deterministic builder turns the `Timeline`
   into arguments. Every value is typed and range-checked by Pydantic before it reaches the builder.
3. **Overlay text goes through a file, never inline.** `drawtext` has its own escaping rules, and
   its `filtergraph` syntax treats `:`, `'`, `\`, and `,` as structure. Arbitrary caption text
   inline is a filtergraph injection. Text is written to a temp file and referenced with
   `textfile=`, and the file path is one we generated.
4. **Asset paths are never model-supplied.** Shots reference `asset_id`; the builder resolves the
   real path from the database and the storage layer. A model cannot name a path.
5. **Scratch directories** are created per render job under a fixed root, with generated names, and
   are removed on completion and on failure. Any resolved path is asserted to remain under the
   scratch root (traversal check after `realpath`).
6. **Resource limits** — CPU time and wall-clock limits on the FFmpeg process; a hung encode is
   killed rather than holding the render worker forever.

---

## 6. Prompt injection

Fetched web pages, competitor video titles and descriptions, and viewer comments are **untrusted
data**. They are also exactly what the research and analysis prompts consume.

Controls, in order of reliability:

1. **Structural separation.** Untrusted content is passed in clearly delimited data regions with an
   explicit instruction that content inside is data to analyze, never instructions to follow.
2. **Constrained outputs.** Analysis stages use structured output schemas. A model that has been
   talked into something still has to return a valid `TopicClassification`, which sharply limits
   what a successful injection can achieve.
3. **No privileged tools in content-processing stages.** The stage that reads a fetched page has no
   ability to fetch, publish, spend, or write to the database. It returns a value; the caller
   decides. This is the control that actually matters — the others reduce likelihood, this one
   reduces consequence.
4. **Human approval gates** sit between any model output and any irreversible action. An injection
   that survives everything above still cannot publish.
5. `channel_settings.editorial_guidelines` is operator-authored and therefore trusted-ish, but is
   still injected as data rather than concatenated into the system prompt.

---

## 7. Input validation

- All API inputs are Pydantic models. No raw dict handling in routers.
- Uploaded files: extension **and** content-sniffed MIME must both be in the allowlist; size capped;
  stored under a generated content-addressed key, never the client-supplied filename.
- All SQL through SQLAlchemy with bound parameters. Raw SQL requires a review comment explaining
  why, and still uses bound parameters.
- URLs, YouTube IDs, and storage keys are validated against strict patterns before use.

---

## 8. Auditability

`audit_logs` records every state transition, every human approval, every publish, and every
configuration change, with actor, before/after, and `request_id`. Approval of a script for
publication is a decision with consequences; it needs a record independent of application logs.

---

## 9. Dependency and supply chain

- Dependencies pinned with a lockfile; `pip-audit` / `npm audit` in CI.
- Base images pinned by digest, not by floating tag.
- CI has no access to production secrets; tests run entirely against fakes and local containers.

---

## 10. What is explicitly out of scope for now

Single-operator deployment: no SSO, no per-user RBAC beyond `project_role`, no SOC2 controls, no
CMEK. `project_id` scoping and the audit log are carried from day one because retrofitting them is
expensive; the rest is deferred and noted here so the deferral is a decision rather than an
oversight.
