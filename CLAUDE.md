# CLAUDE.md

**This repository is the FY3 Beats legal / verification site. Nothing else belongs here.**

## What this is — HANDLE WITH CARE

`index.html`, `privacy.html`, `tos.html`, and the two `tiktok*.txt` files are served by GitHub
Pages and are **referenced by TikTok and Instagram API reviews**. Do not move, rename, or delete
them. A broken URL here means a failed platform review, not a broken link.

`.nojekyll` keeps Pages serving these files verbatim rather than running anything through Jekyll.
Leave it in place.

## Where the FY3 Media Engine went

The research-driven YouTube content operating system previously drafted here under `engine/` now
lives in its own repository:

**https://github.com/leekthatsfye-beep/fy3-media-engine**

It was separated on 2026-09-21 with `git subtree split -P engine`, so its commit history and
authorship are preserved there. What was `engine/docs/` is now `docs/` in that repository, and its
`CLAUDE.md` carries the project's operating rule.

**Do not add engine code, architecture documents, or project state to this repository.** All of
that work happens in `fy3-media-engine`. Equally, do not copy the legal-site files into that
repository — these pages must keep resolving at their current URLs here.

## Working on this repository

It is static HTML with no build step, no dependencies, and no tests. Changes are edits to the HTML
files themselves.

Before changing any of the four served pages, consider whether a platform review currently depends
on its URL or its content. When in doubt, ask rather than edit.
