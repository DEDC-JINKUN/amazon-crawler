# Integrated Amazon collection source lane

This worktree is the clean source-integration lane for PDP and independent Woot reviews. The current task is Goal 1 only: produce a Git-rebuildable source baseline. It is not a production or live-collection lane.

## Start here

Before source selection, import, review, testing, or handoff work:

1. Confirm the directory is exactly `D:/woring/爬虫/.worktrees/amazon-integrated-source-20260915`.
2. Read these authoritative inputs completely:
   - `D:/woring/爬虫/.worktrees/amazon-integrated-deploy/docs/integrated_collection_program_goal_20260915.md`
   - `D:/woring/爬虫/.worktrees/amazon-integrated-deploy/docs/source_manifest_20260915.json`
   - `D:/woring/爬虫/.worktrees/amazon-integrated-deploy/docs/agent_handoff_current.md`
3. Run `git branch --show-current`, `git log -3 --oneline`, and `git status --short` here.
4. Re-hash every proposed source file before copying it. The manifest is an observation, not permission to accept changed bytes.
5. Continue the first incomplete Goal 1 criterion; do not restart completed discovery work.

## Current checkpoint

- Branch: `codex/integrated-source-20260915`, based on `company/main@08468db`.
- The original `D:/woring/爬虫/Amazon Scraping` main worktree contains 176 user-owned changes. Treat it as a read-only source; preserve every unrelated change.
- The bounded 15-file Woot import has not happened. Fourteen hashes still matched; `tests/test_console_woot_reviews.py` changed from manifest SHA256 `d1eabdcf...`/8813 bytes to `b0082861...`/17509 bytes. Reconcile provenance and review that delta before any import.
- Pre-import clean baseline: 535 passed, 35 skipped, 4 failed. Two failures require untracked local fixtures; two PowerShell 5 tests time out under the Chinese worktree path. These are baseline facts, not Woot regressions.
- No production service, database, Worker, Amazon page, Woot endpoint, proxy credential, Nginx, systemd unit, or cloud runtime has been touched in Goal 1.

## Working contract

- Keep architecture decisions, integration, and final verification in the main agent.
- Use a scout only for a large read-only map, a worker only for an explicit non-overlapping file set, a verifier for fresh tests, and a reviewer after non-trivial changes. One delegation round per request; subagents do not delegate.
- Import files individually from the machine-readable manifest. Never merge the recovered PDP directory, dirty main worktree, or cloud-v5 audit directory wholesale.
- Keep `amazon_us` and `amazon_reviews` schemas, Workers, leases, states, and evidence separate. Woot code must not write `amazon_us.item_state` or `amazon_us.product_snapshot`.
- New code requires tests. Run focused offline tests, the complete offline suite, then a fresh UTF-8 isolated PostgreSQL suite. A skipped required engine test is pending, not passed.
- Update CHANGELOG and affected docs. Independent review must finish with P0=0 and P1=0 before Goal 1 can complete.
- Keep runtime data, real ASINs, outputs, caches, credentials, dumps, and `.env` outside Git.

## Hard boundaries

- Work only on code, docs, and isolated tests.
- Keep `Agent/<name>` in configurable UAs.
- Preserve source separation and fail-closed errors.
- Leave production, live collection, Review Campaign, `analysis_run`, authentication gateway, Nginx, and systemd work for later Goals.
- Leave the unfinished release-gate experiment in `amazon-integrated-deploy` untouched; it belongs to the stopped PDP Goal and is outside Goal 1.

Goal 1 is complete only when the source manifest is marked complete, all accepted production files are Git-tracked, a clean checkout reproduces the build/tests, the full required suites have zero failures, docs are current, independent review has no P0/P1, and an undeployed receipt identifies the exact commit and hashes.
