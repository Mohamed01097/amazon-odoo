# Production State Snapshot — 2026-09-20

**Verified at:** 2026-09-20 ~20:10 UTC (read-only database queries, no modifications)
**Database:** `amazon_prod12sep`
**Server:** `ionic-odoo-dev-01`
**Odoo Service:** `odoo19.service` — ACTIVE

---

## Amazon Instance (id=6)

| Field | Value |
|---|---|
| Name | Amazon Egypt Production |
| Seller ID | A2LMV58FMUN2ZD |
| Marketplace ID | ARBP9OOSHTCHU |
| Region | eu |
| Company | Eisa Heikal For Commercial & Industrial Investment (id=1) |
| Active | True |
| `auto_sync_enabled` | **False** |
| `order_status_sync_enabled` | True |
| `fba_sale_stock_cutover_at` | 2026-09-15 21:02:11 |
| `last_order_sync` | 2026-09-16 13:56:46 |
| `last_status_sync_at` | **2026-09-20 19:56:51** |
| `last_stock_sync` | 2026-09-20 17:29:13 |
| `last_product_sync` | 2026-09-14 10:29:25 |
| `order_sync_interval` | 30min |
| `order_status_sync_interval` | 15 minutes |
| `stock_push_interval` | **disabled** |
| `price_push_interval` | **disabled** |
| `settlement_sync_interval` | **disabled** |
| `settlement_accounting_strategy` | settlement_based |
| `settlement_accounting_cutoff_date` | NULL |
| `last_settlement_sync_at` | NULL |

## FBA Locations

| Location | ID | Current On-Hand |
|---|---|---|
| FBA Transit | 29 | 1,571.00 |
| FBA Received / Staging | 30 | 0 |
| FBA Sellable | 31 | **889.00** |
| FBA Reserved | 32 | 48.00 |
| FBA Unsellable | 33 | 8.00 |
| FBA Customer Returns | 34 | 0 |
| FBA Sold / Customers | 35 | 55.00 |
| FBA Removal Transit | 36 | 0 |
| FBA Disposal / Inventory Loss | 37 | 0 |

## Depletion Reconciliation

| Item | Value |
|---|---|
| Opening Sellable (FBAAUDIT/00007, 2026-09-15) | 944 |
| Total depleted — Sum(D) from events | 55 |
| Expected Sellable (944 − 55) | **889** |
| Actual Sellable (stock.quant at location 31) | **889** |
| **Match** | **YES** |

## Stock Moves

| Category | Count |
|---|---|
| Total stock.move records | 95 |
| Total stock.picking records | 46 |
| Opening inventory adjustments → Sellable (11→31) | 18 moves |
| Opening → Reserved (11→32) | 14 moves |
| Opening → Transit (11→29) | 13 moves |
| Opening → Unsellable (11→33) | 4 moves |
| Sale depletion (31→35) | **46 moves**, total qty **55** |

## Orders

| Metric | Count |
|---|---|
| Amazon orders (amazon.sale.order) | 43 |
| Odoo sale orders (sale.order with amazon_order_ref) | 43 |

## FBA Sale Stock Events

| State | Count |
|---|---|
| `done` | 55 |
| `pending` | 0 |
| `manual_review` | 0 |
| `failed` | 0 |
| **Total** | **55** |
| Sum of `last_delta_qty` | 55 |
| Sum of `processed_fulfilled_qty` | 59 |

## Import / Sync Jobs

| Type | Count | Failed |
|---|---|---|
| Import jobs (amazon.order.import.job) | 11 | 0 |
| Status sync jobs (amazon.order.status.sync.job) | **414** | 0 |

## Inventory Audits

| Audit | State | Date | Amazon Records | Matched | Mismatched | Unmapped |
|---|---|---|---|---|---|---|
| FBAAUDIT/00007 | completed | 2026-09-15 21:02:14 | 20 | 0 | 18 | 2 |
| FBAAUDIT/00016 (latest) | completed | 2026-09-20 17:29:11 | 20 | 0 | 18 | 2 |

## Cutover V2 — Run 3

| Field | Value |
|---|---|
| Run ID | 3 |
| State | **activated** |
| history_start_at | 2025-09-15 21:02:11 UTC |
| cutover_at | 2026-09-15 21:02:11 UTC |
| Report windows | 13 completed, 0 failed |
| Raw rows | 15,724 |
| Baseline count | **15,269** |
| Unique orders covered | 13,321 |
| Unique SKUs | 18 |
| Total B (fulfilled_before_cutover) | **16,860** |
| Safety delay hours | 4 |
| Shipment records | 15,724 |

## Product Mapping

- **19** amazon.product records, all mapped to Odoo products (0 unmapped)
- 2 Amazon-side SKUs (M7-X72T-G8NN, O8-DZJQ-EDXW) appear in audit results as "unmapped" — both zero quantity

## Active Crons (15)

| ID | Name | Interval |
|---|---|---|
| 25 | Process Order Import Jobs | 1 min |
| 26 | Sync Order Statuses | 15 min |
| 27 | Process FBA Sale Stock Events | 1 min |
| 35 | Poll Inbound Operations | 1 min |
| 36 | Synchronize Inbound Receiving | 30 min |
| 39 | Enqueue Daily FBA Inventory Audits | daily |
| 40 | Process FBA Inventory Audits | 5 min |
| 46 | Check Connection Health | 15 min |
| 47 | Refresh Operations Dashboard | 15 min |
| 48 | Detect Stuck Jobs | 10 min |
| 49 | Dispatch Eligible Retries | 5 min |
| 50 | Evaluate Operational Alerts | 15 min |
| 51 | Clean Successful Operational Logs | daily |
| 52 | Process Removal Orders and FBA Events | 5 min |
| 56 | Match FBA Reimbursements | daily |

## Inactive Crons (18)

| ID | Name | Risk |
|---|---|---|
| 24 | Import All Orders (FBM + FBA) | Safe to re-enable (read-only dispatcher) |
| 28 | Sync Products | Low risk |
| 29 | Update Product Prices | **WRITES to Amazon** |
| 30 | Import Settlement Reports | Safe read, but accounting not configured |
| 31 | Check Canceled Orders | Legacy |
| 32 | Import FBM Orders | Not applicable (FBA only) |
| 33 | Export Stock Levels | **WRITES to Amazon** |
| 34 | Update FBM Order Status | Not applicable |
| 37 | Refresh Removal Status | Awaiting client approval |
| 38 | Enqueue Audits (Compatibility) | Replaced by cron 39 |
| 41 | Pull Prices | No-op |
| 42 | Full Bidirectional Sync | **WRITES to Amazon — NEVER enable for FBA** |
| 43 | Master Auto-Sync Scheduler | **WRITES to Amazon — MUST stay off** |
| 44 | Smart Alert Scan | Optional |
| 45 | Calculate Product Health Scores | Optional |
| 53 | Import FBA Customer Returns | Awaiting client approval |
| 54 | Import FBA Inventory Adjustments | Awaiting client approval |
| 55 | Import FBA Reimbursements | Awaiting client approval |

## Accounting Status

ALL 15 accounting account fields are **NULL** on instance 6:
- settlement_journal_id, amazon_payout_bank_journal_id, amazon_clearing_account_id
- amazon_sales_account_id, amazon_fee_account_id, amazon_fba_fee_account_id
- amazon_refund_account_id, amazon_reimbursement_account_id, amazon_shipping_account_id
- amazon_promotion_account_id, amazon_tax_account_id, amazon_adjustment_account_id
- amazon_other_credit_account_id, amazon_other_debit_account_id, amazon_suspense_account_id

**Settlement accounting is NOT configured. No settlements imported. No accounting entries created.**

## Defense-in-Depth Status

| Protection | Status |
|---|---|
| `auto_sync_enabled` | **False** |
| `stock_push_interval` | **disabled** |
| `price_push_interval` | **disabled** |
| `settlement_sync_interval` | **disabled** |
| Master Scheduler (id=43) | **inactive** |
| Full Sync (id=42) | **inactive** |
| Export Stock (id=33) | **inactive** |
| Update Prices (id=29) | **inactive** |

## Databases

| Database | Size | Role |
|---|---|---|
| amazon_prod12sep | 107 MB | **PRODUCTION** (Odoo is configured to use this) |
| amazon_prod | 110 MB | Historical/older database (not currently used by Odoo service) |
| test_cutover_v2 | 81 MB | Test database used for Cutover V2 development |

---

*This snapshot was produced by read-only psql queries against the production database. No data was modified.*
