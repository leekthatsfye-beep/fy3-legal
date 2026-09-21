# CLAUDE.md

This repository contains two unrelated things. Know which one you are touching.

## 1. FY3 Beats legal site (repository root) — HANDLE WITH CARE

`index.html`, `privacy.html`, `tos.html`, and `tiktok*.txt` are served by GitHub Pages and are
**referenced by TikTok and Instagram API reviews**. Do not move, rename, or delete them. A broken
URL here means a failed platform review, not a broken link.

`.nojekyll` keeps Pages serving these files verbatim instead of running the repository's Markdown
through Jekyll. Leave it in place.

## 2. FY3 Media Engine (`engine/`) — the build

Research-driven YouTube content operating system. **Architecture only; no code exists yet.**

---

## Operating rule for every session

Follow this order. It is the project's standing instruction, not a suggestion.

1. **Inspect the current repository state** — do not assume it matches these docs.
2. **Read `engine/docs/PROJECT_STATE.md`** — the authoritative record of what exists.
3. **Read the current phase plan** (`PHASE_0_PLAN.md`, then later phases).
4. **Read the existing implementation before changing it.**
5. **Identify the smallest coherent next task.**
6. Implement it.
7. Test it.
8. Verify it — actually run the tests; do not infer that they pass.
9. Update `engine/docs/PROJECT_STATE.md` and any affected architecture document.
10. Continue.

## Standing prohibitions

- **Never claim something works without running it.**
- **Never rewrite a working component** because another implementation looks cleaner.
- **Never create a parallel duplicate system** alongside an existing one.
- **Never fabricate** analytics, research, citations, or API responses. If an API cannot do
  something, say so and redesign around reality.
- **Never use mock or placeholder data in a production path.** Synthetic data belongs in `tests/`
  and nowhere else.
- **Never silently swallow an error.** Re-raise, or record a classified failure reason.
- **Never bypass the three human approval gates** (`APPROVED_IDEA`, `SCRIPT_APPROVED`,
  `SCHEDULED`). No configuration flag may enable this.
- **Never hardcode a value that belongs in configuration** — quota costs and provider pricing in
  particular, since several are still unverified.
- If something is unfinished, **mark it unfinished** in `PROJECT_STATE.md`.

## Before calling any module complete

The 15-point checklist in `engine/docs/TESTING_STRATEGY.md` §7. All fifteen, every time.

## Phase discipline

Do not build ahead of the current phase. Phases 0–4 build the intelligence loop only, ending at a
human-approved ranked opportunity. Rendering, voice, and publishing come later by design — if the
intelligence loop does not surface opportunities worth making videos about, production automation
is worthless.

## Open blocking questions

Three external facts are unconfirmed and each blocks a phase — see the table in
`engine/docs/PROJECT_STATE.md`. Confirm against live documentation at the start of the relevant
phase. **Do not implement against assumed API values.**
