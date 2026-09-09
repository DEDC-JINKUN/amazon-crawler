---
name: "amazon-crawler-ops"
description: "Operate and supervise the Amazon US crawler: batch progress monitoring, CAPTCHA gate handling, proxy switch, competitor batch creation. Invoke when user asks to check/start/stop crawling, handle blocks, or push toward data targets."
---

# Amazon Crawler Operations

Supervise and operate the local Amazon US crawler stack (coordinator + worker + console + Postgres). Use the `amazon-crawler` MCP tools as the primary interface; fall back to CLI/database only for operations not covered by MCP.

## Stack Map

- **Console (HTTP API)**: `http://127.0.0.1:8771`, always pass `?tenant=amazon_us_main`
- **MCP server**: `scripts/crawler_mcp_server.py` (registered in `.mcp.json`, tools listed below)
- **Coordinator**: `scripts/batch_coordinator.py` — spawns workers, advisory-lock global (key 82024001, one per machine regardless of DB/tenant)
- **Worker**: `scripts/amazon_us_worker.py` — Firefox + geckodriver, human pacing built in
- **Production DB**: `dbname=amazon_us`, tenant `amazon_us_main` (⚠️ `.env` DSN points to `dbname=postgres` = old test data; for direct DB work set `$env:AMAZON_US_POSTGRES_DSN='host=127.0.0.1 port=5432 dbname=amazon_us user=postgres'` plus `PGPASSWORD` from `.env`)
- **Config**: `config/amazon_us.windows.human.toml` (pacing + proxy fields)
- **State**: `state/captcha_gate.json` (circuit-breaker gate)

## MCP Tools (preferred interface)

| Tool | Use |
|---|---|
| `overview` | Daily supervision: progress %, blocked/failed counts, traffic, recent runs |
| `list_batches` / `batch_status` | Batch progress + coordinator heartbeat |
| `batch_items` | Per-ASIN detail with status/q filters |
| `create_batch` | Async batch from ASIN array (≤5800/batch), returns batch_id; poll with `batch_status`. `collect_reviews` defaults false |
| `stop_batch` | Stop a running batch |
| `item_detail` | Product snapshot (price/rating/reviews/BSR) + collection state |
| `captcha_gate_status` / `clear_captcha_gate` | Circuit-breaker gate read/clear |

## Human-Pacing Iron Rules (never violate)

1. **0.1 req/s** global rate + **±15% jitter** — do not raise without explicit user approval
2. **Session rest**: every 200–320 pages, rest 5–15 min
3. **CAPTCHA gate**: 2 hard blocks/day or 5 consecutive failures → 6h pause (worker sleeps in-process, coordinator backs off respawns 30s)
4. **One worker per batch** (`--workers-per-batch 1`) — total request rate must stay at one-user level
5. **Night run**: follows user's explicit choice; default no forced downtime unless requested
6. **Proxy traffic budget**: monitor via `overview` traffic fields; alert user at ~80% of purchased GB

## Failure Handbook

### CAPTCHA blocked (batch shows blocked count rising)
1. `captcha_gate_status` — if `paused_until` set, worker is sleeping in-process (safe, survives coordinator restarts)
2. **Direct connection with high block rate**: let it sleep; do NOT clear-gate repeatedly (each clear = re-hitting the wall)
3. **After switching exit IP (VPN node / proxy)**: `clear_captcha_gate` to wake worker early (≤5 min) instead of waiting out the 6h pause
4. Gate resets naturally at UTC midnight (= 08:00 Beijing); `count` does not reset mid-day

### Worker exit / restart storm (history: 20 respawns in 50s burned MAX_WORKER_RESTARTS → whole batch cancelled)
- Symptom: batch suddenly shows mass cancelled, coordinator log shows rapid spawn loop
- Already fixed in code (worker sleeps out pauses in-process + coordinator 30s respawn backoff). If it recurs: stop coordinator, check gate file, restart coordinator — batch state lives in Postgres, crash-safe

### Console unreachable
- `overview` returns `console_unreachable` → check console process; restart: `.venv` python `scripts/collection_console.py` on port **8771** (8770 is occupied by a legacy deployment — never use)

### Proxy switch (planned maintenance pattern)
1. Get consent for timing (never mid-batch unless user approves)
2. Fill `proxy_url` / `proxy_username_env` / `proxy_password_env` in `config/amazon_us.windows.human.toml`; credentials live in `.env` (never inline in config)
3. Restart coordinator AND workers (coordinator is the env-var source for spawned workers)
4. `clear_captcha_gate`, verify exit IP is US residential, watch first ~20 items' block rate before leaving unattended

## Batch Creation Rules

- Own vs competitor: console `create_batch` tags everything `own`. Competitor batches created via CLI `create-batch --subject-type candidate` are NOT claimable by default workers (claim_task filters by subject_type) — resolve labeling before scale runs: either create competitor batches through the console (own tag + separate manifest CSV to distinguish) or extend claim filtering
- Always pass `idempotency_key` for programmatic creation (24h dedup on retry)
- Max 5800 ASINs per batch; split larger target lists
- Competitor discovery: `scripts/link_competitor_relations.py` extracts competitor ASINs from already-crawled own-product HTML (carousels/similar-items sections) — run with `--force` to refresh pool after own batches complete

## Supervision Cadence

- Active batch: check `overview` + `batch_status` every 10–15 min for first 30 min after start, then every 1–2 h
- Escalate to user when: block rate >5%, traffic >80% budget, batch terminal (report final stats), or coordinator heartbeat stale >2 min
