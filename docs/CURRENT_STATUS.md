# Current Status

**Last Updated:** 2026-09-16 17:15 UTC
**Module Version:** 19.0.10.6.1
**Database:** amazon_prod12sep
**Amazon Instance:** Amazon Egypt Production (id=6)

## Production-Live Features

The following are **active in production** as of 2026-09-16:

### Order Pipeline — LIVE
- Order import cron (id=24) — **currently inactive** (was activated at go-live, later deactivated). Safe read-only dispatcher; can be re-enabled if needed.
- Order import job processor (id=25) runs every 1 minute.
- Order status sync (id=26) runs every 15 minutes — **primary order discovery mechanism**.
- 43 Amazon orders imported, 43 Odoo sale orders created.
- Zero failed import or status sync jobs (11 import jobs, 23 status sync jobs total).

### FBA Sale Stock Depletion — LIVE
- FBA sale stock event processor (id=27) runs every 1 minute.
- 55 events processed (all `done`), zero `pending`, `failed`, or `manual_review`.
- Sellable depletion verified: 944 (opening) − 42 (depleted) = 902 (current). Matches.

### Cutover V2 — ACTIVATED
- Cutover Run 3: state=activated. 15,269 baselines covering 13,321 unique orders.
- history_start_at: 2025-09-15 21:02:11 UTC.
- cutover_at: 2026-09-15 21:02:11 UTC.
- Prevents double-depletion of historical orders. Validated with real production data.

### FBA Inventory Audits — LIVE
- Daily audit enqueue (id=39) and 5-minute audit processor (id=40) are active.
- Latest audit: FBAAUDIT/00011 (completed 2026-09-16).
- Depletion reconciliation confirmed: Odoo Sellable matches formula (944 − sum(D) = 902).

### FBA Inbound Shipments — LIVE
- Inbound operation poller (id=35) runs every 1 minute.
- Inbound receiving sync (id=36) runs every 30 minutes.

### Monitoring & Operations — LIVE
- Connection health check (id=46) — 15 min.
- Dashboard refresh (id=47) — 15 min.
- Stuck job detection (id=48) — 10 min.
- Eligible retry dispatch (id=49) — 5 min.
- Operational alert evaluation (id=50) — 15 min.
- Successful log cleanup (id=51) — daily.
- Process Removal Orders and FBA Events (id=52) — 5 min.
- Reimbursement matching (id=56) — daily.

### Product Mapping — COMPLETE
- 19 `amazon.product` records, all mapped to Odoo products by SKU.
- 2 additional Amazon-side SKUs (M7-X72T-G8NN, O8-DZJQ-EDXW) appear in audit results as "unmapped" — both have zero quantity across all categories.

### FBA Inventory — SEEDED
- Opening inventory loaded from FBAAUDIT/00007 (2026-09-15 21:02:11 UTC).
- Opening: Sellable=944, Reserved=48, Unsellable=8, Transit=1,571.
- Current (2026-09-16 17:15 UTC): Sellable=902, Reserved=48, Unsellable=8, Transit=1,571.
- Stock moves: 82. Stock pickings: 33.

### Defense-in-Depth — ACTIVE
- `stock_push_interval` = disabled (prevents accidental stock push to Amazon).
- `price_push_interval` = disabled (prevents accidental price push to Amazon).
- `settlement_sync_interval` = disabled (prevents settlement import before accounting setup).
- `auto_sync_enabled` = False (prevents Master Scheduler activation).

### Cron Summary
- **15 active** Amazon crons (see `docs/IMPLEMENTER_HANDOFF.md` Section 9 for full list).
- **18 inactive** Amazon crons (intentionally disabled — dangerous, not yet configured, or not applicable).

## Not Yet Configured — Requires Implementer/Accountant

### Settlement Accounting — WAITING
- Settlement accounting strategy set to `settlement_based`, but all 15 account/journal fields are NULL.
- No settlement journal, no clearing account, no payout bank journal configured.
- Zero settlements imported, zero accounting entries created.
- **Blocked on:** Accountant configuring chart of accounts and account mappings.

### Egyptian Tax Configuration — WAITING
- No tax configuration for Amazon Egypt settlements.
- **Blocked on:** Accountant and tax advisor decisions.

### Settlement Cutoff Date — WAITING
- Not set. Must be set before importing settlements.
- **Blocked on:** Accountant decision.

## Disabled — Requires Business Decision

### Customer Returns Import — DECISION REQUIRED
- Cron id=53 inactive. Zero return reports imported.
- **Blocked on:** Client approval to enable.

### Inventory Adjustments Import — DECISION REQUIRED
- Cron id=54 inactive. Zero adjustments imported.
- **Blocked on:** Client approval and policy confirmation (informational vs. stock-moving).

### Reimbursements Import — DECISION REQUIRED
- Cron id=55 inactive. Zero reimbursements imported.
- **Blocked on:** Client approval to enable.

### Removal Order Tracking — DECISION REQUIRED
- Cron id=37 inactive. Zero removal orders.
- **Blocked on:** Client having active removals.

### Product Sync Automation — DECISION REQUIRED
- Cron id=28 inactive.
- **Blocked on:** Client decision on product master (Odoo vs Amazon).

### Price Push — DISABLED (Safety)
- Cron id=29 inactive. Interval set to `disabled`.
- **Blocked on:** Client price policy decision + developer approval.

### Stock Push — DISABLED (Safety)
- Cron id=33 inactive. Interval set to `disabled`.
- Not required for FBA. Amazon manages its own warehouse stock.

### AI Features — NOT CONFIGURED
- No AI API key configured. All AI intervals are inert.
- **Blocked on:** Client decision and API key provisioning.

## Permanently Disabled

| Item | Why |
|---|---|
| Master Auto-Sync Scheduler (id=43) | Dispatches ALL sync types including dangerous writes. Must stay off. |
| Full Bidirectional Sync (id=42) | Bundles reads and writes. Never enable for FBA. |
| FBM order crons (id=32, 34) | FBA-only integration. No merchant fulfillment. |

## Open Risks

- Audit mismatches between Odoo and Amazon are expected due to fulfillment timing and Reserved reclassification. Persistent or growing gaps require investigation.
- 2 unmapped Amazon-side SKUs with zero quantity. Monitor for non-zero quantity developing.
- Accounting configuration is the primary remaining deliverable before full financial integration.
- Price and stock export remain disabled and must not be enabled without explicit developer and client approval.
- Report/feed/API behavior may change; check against official Amazon documentation periodically.

## Handoff Documentation

Full implementer onboarding guide: [`docs/IMPLEMENTER_HANDOFF.md`](IMPLEMENTER_HANDOFF.md)
