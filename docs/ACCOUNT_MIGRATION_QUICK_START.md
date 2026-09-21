# Account Migration Quick Start

This document is short enough to paste into a new AI chat session. For the full context, read `docs/AI_CONTINUATION_PROMPT.md`.

---

## Project

Amazon Egypt FBA connector for Odoo 19 Community. Module: `sdlc_amazon_connector` v19.0.10.6.1.

## Server

- Host: `ionic-odoo-dev-01` (Ubuntu 24.04)
- Repo: `/opt/odoo19/custom-addons/amazon-odoo`
- DB: `amazon_prod12sep` (PostgreSQL 16)
- Service: `odoo19.service`
- Config: `/etc/odoo19.conf`
- Logs: `/var/log/odoo19/odoo19.log`

## Current State (verified 2026-09-20)

- **LIVE:** Order Status Sync (cron 26, 15 min), Event Processor (cron 27, 1 min), Inventory Audits (daily), Inbound Shipments, Monitoring
- **43 orders**, 55 events (all `done`), **889 Sellable** (944 opening − 55 depleted = 889 VERIFIED)
- **Cutover V2 Run 3:** activated, 15,269 baselines, prevents double-depletion
- **15 active crons, 18 disabled**
- **Settlement Accounting:** NOT configured (all 15 account fields NULL)

## Critical Do-Not-Touch

- `auto_sync_enabled` = False — KEEP IT
- `stock_push_interval` = disabled — KEEP IT
- `price_push_interval` = disabled — KEEP IT
- Crons 29, 33, 42, 43 — MUST stay inactive (write to Amazon)
- Cutover Run 3, baselines, cutover_at timestamp — NEVER modify
- Opening stock FBAAUDIT/00007 — NEVER re-seed
- FBA locations 29-37 — NEVER change

## Next Task

Settlement Accounting configuration with the accountant. See `docs/IMPLEMENTER_HANDOFF.md` Sections 14-15.

## Documents to Read

1. `CLAUDE.md` — safety rules
2. `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md` — verified numbers
3. `docs/IMPLEMENTER_HANDOFF.md` — full functional guide
4. `docs/ACCOUNT_MIGRATION_MASTER_HANDOFF.md` — complete technical handoff
5. `docs/OPERATIONS_RUNBOOK.md` — troubleshooting and server ops
6. `docs/AI_CONTINUATION_PROMPT.md` — ready-to-paste prompt for new AI

## DB Access

```bash
sudo -u odoo19 psql -d amazon_prod12sep -c "SELECT ..."
```
