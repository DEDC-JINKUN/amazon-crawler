# Product watch reporting release candidate

This candidate is **not deployed**. It adds only PostgreSQL records and local loopback endpoints; no migration deletes observations, evidence, reports or receipts.

## Migration order

1. Confirm a backup, a stopped consumer, target database and candidate commit.
2. Apply `schema/postgres_schema.sql`.
3. Apply, in order: `schema/migrations/20260905_product_watch.sql`, `schema/migrations/20260905_search_rank.sql`, then `schema/migrations/20260905_watch_delivery.sql`.
4. On the marked isolated database only, use `scripts/apply_recovery_migrations.py --mode dry-run`. Production apply requires `--mode apply --confirm-production --expected-head <candidate-commit>` and a clean worktree.

## Operation and recovery

- Run one bounded pass through DPAPI with `scripts/run_product_watch_secure.ps1 -ConfigPath <approved-config>`; pass `-Iterations` and `-IntervalSeconds` only for a finite periodic run. It reads completed crawler rows, normalizes them, reconciles unhandled events to the outbox, and does not fetch Amazon or expand raw HTML.
- An unconfigured channel leaves reports traceably pending/unknown. `--test-receipt-dir <directory>` is an explicitly local test outlet; its receipt is not an external delivery.
- A `sending` record older than 15 minutes becomes `unknown`, not automatically resent. Failed records retry with bounded backoff and stop automatic retries after three attempts.
- Code rollback means stop the watch consumer and revert its code. Do not delete the additive tables: they are business evidence and delivery audit records.

## Required production configuration

Before any live collection or external delivery, provide approved tenant, 3–5 owned ASINs and competitor map, keywords/search depth/frequency, BSR category, thresholds/time zone, one approved channel and recipient, plus target-machine running account. These values are intentionally absent from the example config.
