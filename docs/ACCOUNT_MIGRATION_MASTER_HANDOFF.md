# Account Migration Master Handoff

**Purpose:** Complete knowledge transfer for continuing the Amazon Egypt FBA Odoo 19 integration project in a new AI account, ChatGPT session, or by a new developer.

**Prepared:** 2026-09-20 ~20:15 UTC
**Verified against:** Production database `amazon_prod12sep` and source code on server `ionic-odoo-dev-01`

---

## Table of Contents

1. [Environment](#1-environment)
2. [Git / Code State](#2-git--code-state)
3. [Connector Architecture from Zero](#3-connector-architecture-from-zero)
4. [Amazon Instance Current State](#4-amazon-instance-current-state)
5. [Product Mapping](#5-product-mapping)
6. [FBA Inventory Architecture](#6-fba-inventory-architecture)
7. [Opening Stock History](#7-opening-stock-history)
8. [Cutover V2 — Full Technical Explanation](#8-cutover-v2)
9. [Production Test History](#9-production-test-history)
10. [Current Orders](#10-current-orders)
11. [Current Stock / Event State](#11-current-stock--event-state)
12. [Automation / Crons](#12-automation--crons)
13. [Amazon Write Operations](#13-amazon-write-operations)
14. [Accounting / Settlement Status](#14-accounting--settlement-status)
15. [Implementer Responsibilities](#15-implementer-responsibilities)
16. [Client Responsibilities](#16-client-responsibilities)
17. [Technical Owner Responsibilities](#17-technical-owner-responsibilities)
18. [Troubleshooting Runbook](#18-troubleshooting-runbook)
19. [Server Operations](#19-server-operations)
20. [Test Suite](#20-test-suite)
21. [Decision Log](#21-decision-log)
22. [Open Items / Next Steps](#22-open-items--next-steps)
23. [Temporary Production Scripts](#23-temporary-production-scripts)
24. [Documentation Index](#24-documentation-index)

---

## 1. Environment

| Component | Value |
|---|---|
| Server hostname | ionic-odoo-dev-01 |
| OS | Ubuntu 24.04.4 LTS |
| Odoo version | 19.0.0 final (Community) |
| Python | 3.12.3 via venv at `/opt/odoo19/venv/` |
| Odoo service | `odoo19.service` (systemd) — **ACTIVE** |
| Odoo config | `/etc/odoo19.conf` |
| Odoo log | `/var/log/odoo19/odoo19.log` |
| Nginx | 1.24.0 — reverse proxy on port 80 to 127.0.0.1:8069 |
| PostgreSQL | 16.15 |
| Custom addons | `/opt/odoo19/custom-addons/amazon-odoo` |
| Git remote | `git@github.com:Mohamed01097/amazon-odoo.git` |
| Branch | `main` |
| Module | `sdlc_amazon_connector` version **19.0.10.6.1** |

### Databases

| Database | Size | Role |
|---|---|---|
| **amazon_prod12sep** | 107 MB | **PRODUCTION** — Odoo is configured to use this (`db_name` in config) |
| amazon_prod | 110 MB | Historical/older database — **NOT used by running Odoo service** |
| test_cutover_v2 | 81 MB | Test database for Cutover V2 development — **NOT production** |

### Credential Locations (DO NOT EXPOSE VALUES)

| Credential | Where |
|---|---|
| Amazon SP-API (Refresh Token, LWA Client ID/Secret) | `amazon_instance` table, id=6 |
| Odoo DB password | `/etc/odoo19.conf` (`db_host=False` means local socket auth, no password needed) |
| Odoo admin password | `/etc/odoo19.conf` (`admin_passwd`) |

To verify Amazon credentials work: Odoo UI > Amazon > Configuration > Instances > "Test Connection" button.

---

## 2. Git / Code State

| Item | Value |
|---|---|
| Branch | main |
| HEAD commit | `b9cd146` "claude edits" |
| Ahead of origin | 1 commit |
| Modified | `docs/CURRENT_STATUS.md` |
| Untracked | `docs/IMPLEMENTER_HANDOFF.md`, `docs/IMPLEMENTER_HANDOFF_AR.md`, `docs/CURRENT_STATUS_AR.md`, plus new migration docs |

### Key Source Files

#### Core Models

| File | Purpose | Live? |
|---|---|---|
| `amazon_instance.py` (128K) | Instance configuration, sync scheduling, all field definitions | YES |
| `amazon_fba_sale_stock.py` (45K) | FBA sale stock event processing, depletion logic, Cutover V2 integration | YES — critical |
| `amazon_fba_sale_stock_cutover.py` (22K) | Cutover V2 run, baseline building, shipment evidence | YES — critical |
| `amazon_sale_order.py` (43K) | Amazon order model, sale order creation | YES |
| `amazon_order_import_job.py` (30K) | Order import job processing | YES |
| `amazon_order_status_sync_job.py` (23K) | Order status sync job processing | YES |
| `amazon_inventory_reconciliation.py` (59K) | FBA inventory audits, per-SKU comparison | YES |
| `amazon_product.py` (116K) | Product model, SKU mapping, catalog sync | YES |
| `amazon_api.py` (85K) | SP-API wrapper (all Amazon API calls) | YES |
| `amazon_inbound_shipment.py` (84K) | Inbound FBA plan management | YES |
| `amazon_inbound_shipping.py` (112K) | Inbound shipping, labels, tracking | YES |
| `amazon_inbound_receiving.py` (47K) | Inbound receiving sync | YES |
| `amazon_settlement.py` (62K) | Settlement import, accounting entry creation | Implemented, NOT configured |
| `amazon_payout.py` (42K) | Payout evidence, bank reconciliation | Implemented, NOT configured |
| `amazon_return.py` (20K) | Customer returns import | Implemented, NOT enabled |
| `amazon_removal_order.py` (35K) | Removal/disposal tracking | Implemented, NOT enabled |
| `amazon_phase7.py` (114K) | Returns, removals, adjustments, reimbursements processing | Implemented, NOT enabled |
| `amazon_operations.py` (95K) | Operational monitoring, alerts, retries, dashboard | YES |
| `amazon_fba_inventory.py` (5K) | FBA inventory summary model | YES |

#### Supporting Files

| File | Purpose |
|---|---|
| `amazon_smart_alerts.py` | AI-powered alert scanning (optional) |
| `ai_pricing.py`, `ai_listing.py`, `ai_forecast.py`, `ai_reviews.py` | AI features (optional, not configured) |
| `sale_order_inherit.py` | Extends sale.order with Amazon fields |
| `stock_picking_inherit.py` | Extends stock.picking with Amazon fields |
| `stock_location_inherit.py` | Extends stock.location with FBA types |
| `account_move_inherit.py` | Extends account.move with settlement link |

#### Test Files (21 test modules)

All under `sdlc_amazon_connector/tests/`:

| File | What It Tests |
|---|---|
| `test_fba_cutover_v2.py` (45K) | Cutover V2 logic — baselines, delta, coverage, B>C, manual_review |
| `test_fba_sale_stock.py` (32K) | FBA sale stock events, depletion, idempotency |
| `test_fba_stock_structure.py` (9K) | FBA location structure |
| `test_fba_dispatch.py` (18K) | Outbound dispatch pickings |
| `test_fba_inbound_plan_phase2.py` (29K) | Inbound plan creation |
| `test_fba_packing_placement_phase3.py` (59K) | Packing/placement options |
| `test_fba_shipping_phase4.py` (42K) | Shipping, labels, tracking |
| `test_fba_receiving_phase5.py` (24K) | Receiving sync |
| `test_inventory_reconciliation_phase6.py` (20K) | Inventory audits |
| `test_operations_phase65.py` (24K) | Operational monitoring |
| `test_phase7_fba_events.py` (22K) | Phase 7 events processing |
| `test_fba_customer_returns.py` (14K) | Customer returns |
| `test_fba_removal_disposal.py` (17K) | Removals/disposals |
| `test_fba_reimbursements.py` (17K) | Reimbursements |
| `test_orders_api_v2026.py` (7K) | Orders API behavior |
| `test_settlement_accounting.py` (20K) | Settlement accounting entries |
| `test_settlement_payout.py` (16K) | Settlement payout matching |
| `test_payout_clearing.py` (16K) | Payout clearing reconciliation |
| `test_final_e2e.py` (28K) | Mocked end-to-end flow |

**DO NOT run tests on production.** Use a test database.

---

## 3. Connector Architecture from Zero

### Data Flow Overview

```
Amazon Seller Central (Egypt)
        ↕ SP-API (Selling Partner API, EU region)
sdlc_amazon_connector (Odoo module)
        ↕ ORM
Odoo 19 Database (amazon_prod12sep)
```

### Two-Tier Cron Architecture

The connector uses **dispatchers** (create jobs) and **processors** (execute jobs):

- **Dispatcher** creates a job record (e.g., import job, status sync job)
- **Processor** picks up pending jobs and executes them (calls Amazon API, creates records)

This separation provides: audit trail, retry capability, rate limiting, and crash recovery.

### Feature Status

#### Order Import — LIVE

Amazon orders flow into Odoo via:
1. **Order Status Sync** (cron 26, every 15 min) — primary mechanism. Uses `LastUpdatedAfter` to find new/updated orders.
2. Each sync creates `amazon.order.status.sync.job` records
3. Job processor creates/updates `amazon.sale.order` → `sale.order` → `amazon.fba.sale.stock.event`
4. Event processor (cron 27, every 1 min) processes pending events → creates stock pickings (Sellable→Sold)

**Duplicate protection:** Unique constraint on `(instance_id, amazon_order_ref, amazon_order_item_id)` for events, and `amazon_order_ref` for orders.

#### FBA Sale Stock Depletion — LIVE

Each fulfilled order item creates an `amazon.fba.sale.stock.event`. The processor:
1. Acquires an advisory lock on (instance, product)
2. Calculates `delta = C - P` (cumulative fulfilled - already processed)
3. If delta > 0: creates a stock picking from FBA Sellable (31) → FBA Sold/Customers (35)
4. Updates P = P + delta, state = 'done'

For pre-cutover orders, P is initialized to B (baseline) at creation, so delta only covers new fulfillments.

#### Cutover V2 — LIVE

See Section 8 for full explanation. Prevents double-depletion of historical orders.

#### Inventory Audits — LIVE

Daily audit (cron 39) enqueues an `amazon.inventory.reconciliation.run`. Processor (cron 40) calls `getInventorySummaries` API and compares per-SKU quantities against Odoo's FBA location quants.

#### Inbound FBA Shipments — LIVE

Full inbound workflow: plan creation → packing → placement → shipping → labels → tracking → dispatch picking → receiving sync. Steps 1-9 are manual; receiving sync (cron 36) is automatic.

#### Product Mapping — COMPLETE

19 `amazon.product` records, all mapped to Odoo products by SKU.

#### Settlements — IMPLEMENTED BUT NOT CONFIGURED

Settlement import, financial line parsing, draft journal entry creation, and payout matching are coded. ALL 15 accounting fields on instance 6 are NULL. No settlements imported.

#### Returns / Adjustments / Reimbursements — IMPLEMENTED BUT NOT ENABLED

Import crons (53, 54, 55) are disabled. Zero records imported. Awaiting client approval.

#### Removal Orders — IMPLEMENTED BUT NOT ENABLED

Cron 37 disabled. Zero removals tracked.

#### Price Push — DISABLED (Safety)

Cron 29 inactive, `price_push_interval` = disabled. **DO NOT USE** without explicit client and developer approval.

#### Stock Push — DISABLED (Safety)

Cron 33 inactive, `stock_push_interval` = disabled. **DO NOT USE** for FBA. Amazon manages its own warehouse stock.

#### Full Sync — DISABLED (Safety)

Cron 42 inactive. Bundles reads AND writes. **NEVER enable for FBA.**

#### Master Scheduler — DISABLED (Safety)

Cron 43 inactive, `auto_sync_enabled` = False. **MUST stay off.**

#### AI Features — NOT CONFIGURED

No API key. All AI intervals are inert.

---

## 4. Amazon Instance Current State

*Verified from database 2026-09-20*

### Core Configuration — CONFIGURED

| Field | Value | Status |
|---|---|---|
| id | 6 | — |
| name | Amazon Egypt Production | — |
| seller_id | A2LMV58FMUN2ZD | CONFIGURED |
| marketplace_id | ARBP9OOSHTCHU | CONFIGURED |
| region | eu | CONFIGURED |
| fulfillment_program | none | Default |
| company_id | 1 | CONFIGURED |
| active | True | — |

### FBA Locations — CONFIGURED

| Field | Location ID | Status |
|---|---|---|
| fba_warehouse_id | 1 (WH) | CONFIGURED |
| fba_source_location_id | 5 (WH/Stock) | CONFIGURED |
| fba_transit_location_id | 29 | CONFIGURED |
| fba_received_location_id | 30 | CONFIGURED |
| fba_sellable_location_id | 31 | CONFIGURED |
| fba_reserved_location_id | 32 | CONFIGURED |
| fba_unsellable_location_id | 33 | CONFIGURED |
| fba_return_source_location_id | 34 | CONFIGURED |
| fba_sold_customer_location_id | 35 | CONFIGURED |
| fba_removal_transit_location_id | 36 | CONFIGURED |
| fba_disposal_location_id | 37 | CONFIGURED |
| fba_ship_from_partner_id | 1 | CONFIGURED |
| fba_removal_return_partner_id | 1 | CONFIGURED |

### Sync Settings — PARTIALLY CONFIGURED

| Field | Value | Status |
|---|---|---|
| auto_sync_enabled | **False** | CONFIGURED — must stay False |
| order_status_sync_enabled | True | CONFIGURED |
| order_status_sync_interval | 15 min | CONFIGURED |
| order_sync_interval | 30min | CONFIGURED (but master scheduler is off) |
| stock_push_interval | **disabled** | CONFIGURED — defense-in-depth |
| price_push_interval | **disabled** | CONFIGURED — defense-in-depth |
| settlement_sync_interval | **disabled** | CONFIGURED — accounting not ready |
| fba_sale_stock_cutover_at | 2026-09-15 21:02:11 | CONFIGURED — DO NOT CHANGE |

### Accounting — NOT CONFIGURED (BLOCKING)

All 15 account/journal fields are **NULL**:
- settlement_journal_id, amazon_payout_bank_journal_id, amazon_clearing_account_id
- amazon_sales_account_id through amazon_suspense_account_id

**Status:** BLOCKING — accountant must configure before settlement import.

---

## 5. Product Mapping

*Verified from database 2026-09-20*

**19 amazon.product records, ALL mapped to Odoo products.** Zero unmapped.

| ID | SKU | ASIN | Odoo Product ID | Product Name |
|---|---|---|---|---|
| 1 | 24-BHT6-LWJ7 | B0BPYF4S5H | 2 | IONIC Dashboard Protectant |
| 2 | 6224003090026 | B0DGLR6B23 | 3 | IONIC No Rinse Wash & Wax (1kg) |
| 3 | 6225000411319 | B0BPYB4RPR | 4 | IONIC Car Shampoo Concentrated |
| 4 | 6225000411388 | B0CHFQGVQR | 5 | Ionic Nano Ceramic and Wax |
| 5 | 7C-ADQ8-HTSV | B0HHYVDW2Y | 6 | IONIC Microfiber Car Wash Mitt |
| 6 | 7S-2OKY-YSE0 | B0BPYM96H5 | 7 | IONIC Interior Detailer |
| 7 | 9A-J2EN-DBFW | B0F5BM9THK | 8 | IONIC Microfiber Car Drying Towel |
| 8 | 9J-KM0L-FO5I | B0BPYJR7WQ | 9 | IONIC Tire Shine |
| 9 | A6-RDKR-RHZ6 | B0BPYLBBRP | 10 | IONIC Waterless Wash & Wax |
| 10 | FD-FUU2-SGIC | B0F5BGGYJJ | 11 | IONIC Microfiber Cleaning Towels |
| 11 | IONIC-KIT-CS-01 | B0HHFT3TPJ | 12 | IONIC Car Shampoo Wash Kit |
| 12 | IONIC-KIT-NR-01 | B0HHFFC77D | 13 | IONIC No Rinse Car Wash Kit |
| 13 | Ion-1110 | B0BZZKFQWR | 14 | IONIC No Rinse Wash & Wax |
| 14 | J6-TVWG-OV1I | B0DDLF1VCG | 15 | IONIC Car Shampoo |
| 15 | PE-FCJU-3D68 | B0FPG8TY71 | 16 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 16 | PX-KEWE-P4MR | B0FPG9SN51 | 17 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 17 | UT-6XOE-DDRN | B0FPG9VHQV | 18 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 18 | XT-K255-V5H8 | B0BPYK1FKS | 19 | IONIC Car Leather Conditioner |
| 19 | XL-ALNF-5LTF | B0FPG9TV9F | 20 | IONIC Ultra Thick Microfiber (1200 GSM) |

**Unmapped Amazon-side SKUs:** M7-X72T-G8NN and O8-DZJQ-EDXW appear in audit results with zero quantity across all categories. These have no `amazon.product` record — they are Amazon-side listings only.

**When a new SKU appears:** Use Amazon > Catalog > Import / Map Products to create an `amazon.product` record and link it to an Odoo product.

---

## 6. FBA Inventory Architecture

### Location Map (verified IDs)

| Location | ID | Odoo Type | Current On-Hand | Purpose |
|---|---|---|---|---|
| Amazon FBA Transit | 29 | transit | 1,571 | Stock in transit to Amazon |
| FBA Received / Staging | 30 | internal | 0 | Amazon confirmed receipt, pending disposition |
| **FBA Sellable** | **31** | **internal** | **889** | **Available for sale — depleted by order fulfillment** |
| FBA Reserved | 32 | internal | 48 | Reserved for pending orders (Amazon-managed) |
| FBA Unsellable | 33 | internal | 8 | Damaged/defective at Amazon |
| FBA Customer Returns | 34 | internal | 0 | Return evidence location |
| **FBA Sold / Customers** | **35** | **customer** | **55** | **Destination for fulfilled items** |
| FBA Removal Transit | 36 | transit | 0 | Removal shipment in transit |
| FBA Disposal / Loss | 37 | internal | 0 | Disposed or lost |

### Stock Movement Patterns

| From | To | What Triggers It | Count (current) |
|---|---|---|---|
| Inventory Adjustment (11) | Sellable (31) | Opening stock seed | 18 |
| Inventory Adjustment (11) | Reserved (32) | Opening stock seed | 14 |
| Inventory Adjustment (11) | Transit (29) | Opening stock seed | 13 |
| Inventory Adjustment (11) | Unsellable (33) | Opening stock seed | 4 |
| **Sellable (31)** | **Sold/Customers (35)** | **FBA sale stock event processing** | **46** |

### Odoo vs Amazon

Odoo maintains a **shadow copy** of Amazon's FBA inventory. It is not the source of truth for physical stock — Amazon is. Daily audits compare the two, and small differences are expected due to fulfillment timing.

---

## 7. Opening Stock History

### What Happened

On 2026-09-15 21:02:11 UTC, the `getInventorySummaries` API was called to snapshot Amazon's actual FBA inventory. This data was used to create Odoo inventory adjustments that seeded the FBA location quants.

### Opening Snapshot (VERIFIED from database)

| Reference | FBAAUDIT/00007 |
|---|---|
| Database record | `amazon_inventory_reconciliation_run` id=7 |
| State | completed |
| Created | 2026-09-15 21:02:14 UTC |
| Amazon records read | 20 |

| Location | Opening Quantity | Verified |
|---|---|---|
| Sellable | 944 | YES (sum of stock.move qty from location 11→31 = 944) |
| Reserved | 48 | YES |
| Unsellable | 8 | YES |
| Transit | 1,571 | YES |
| **Total** | **2,571** | — |

### Important Rules

- FBAAUDIT/00007 is the **historical opening snapshot**, NOT current Amazon inventory
- Opening stock was a **one-time operation**. DO NOT re-seed.
- The seeding script (`/tmp/seed_fba_opening_stock.py`) is HISTORICAL ONLY — DO NOT RE-RUN
- 49 stock.move records were created for the opening (18+14+13+4=49)
- No accounting entries were created for the opening adjustment

---

## 8. Cutover V2 — Full Technical Explanation

### Why It Exists

Before Odoo went live on 2026-09-15, Amazon had been fulfilling orders for ~1 year. The opening inventory (944 Sellable) already reflected all historical fulfillments. When Odoo imports those old orders and sees "Amazon fulfilled N units," it would subtract N from Sellable — but that fulfillment is already baked into 944. This would cause **double-depletion**.

### The Solution

Cutover V2 maintains **baselines** — records of how many units Amazon had already fulfilled per order item before the cutover date. When importing a pre-cutover order, the system:

1. Looks up B (baseline fulfilled qty) for the order item
2. **Initializes P to B** at event creation
3. Processes using `D = C - P` (not `D = C - B - P`)
4. After processing: `P = P + D`

Because P starts at B, the first processing effectively computes `D = C - B`. B is used exactly once — at initialization — and is never subtracted separately during processing.

### Source Code Proof

**Event creation** (`amazon_fba_sale_stock.py`, line 317):
```python
values['processed_fulfilled_qty'] = baseline  # P initialized to B
```

**Processing** (`amazon_fba_sale_stock.py`, line 795):
```python
delta = self.amazon_cumulative_fulfilled_qty - self.processed_fulfilled_qty  # D = C - P
```

**After processing** (line 806):
```python
'processed_fulfilled_qty': self.processed_fulfilled_qty + delta  # P = P + D
```

### Cutover Run 3 (VERIFIED from database)

| Field | Value |
|---|---|
| Run ID | 3 |
| State | **activated** |
| history_start_at | 2025-09-15 21:02:11 UTC |
| cutover_at | 2026-09-15 21:02:11 UTC |
| Report windows | 13 completed, 0 failed |
| Raw shipment rows | 15,724 |
| Included rows | 15,724 (0 excluded) |
| **Baseline count** | **15,269** |
| Unique orders | 13,321 |
| Unique SKUs | 18 |
| **Total B** | **16,860** |
| Safety delay | 4 hours |
| Created | 2026-09-15 21:54:58 |
| Activated | 2026-09-16 08:40:54 |

### Models

- `amazon.fba.sale.stock.cutover.run` — the run itself (state machine: draft → building → ready → activated)
- `amazon.fba.sale.stock.cutover.baseline` — per-order-item baseline (15,269 records)
- `amazon.fba.sale.stock.cutover.shipment` — raw shipment evidence (15,724 records)

### Pseudocode for V2 Decision Logic

```
ON EVENT CREATION (pre-cutover order):
    B = lookup_baseline(order_ref, item_id)
    IF order outside coverage window AND B == 0:
        state = manual_review (CUTOVER_BASELINE_OUTSIDE_COVERAGE)
    ELIF B > C:
        state = manual_review (CUTOVER_BASELINE_EXCEEDS_CUMULATIVE)  
    ELSE:
        P = B  # CRITICAL: P initialized to B
        IF C > B AND product is storable:
            state = pending  # will be processed
        ELSE:
            state = done  # C == B, nothing to deplete

ON EVENT PROCESSING:
    D = C - P
    IF D > 0:
        create_picking(Sellable → Sold, qty=D)
        P = P + D
    state = done
```

### MUST NEVER Be Changed

- Cutover Run 3 state, timestamps, baselines
- The 15,269 baseline records
- The cutover_at timestamp (2026-09-15 21:02:11)
- The history_start_at (2025-09-15 21:02:11)

---

## 9. Production Test History

### A. First Post-Cutover Import (2026-09-16 ~10:24)

- 7 new post-cutover orders imported
- All events created with P=0 (no baseline needed)
- Stock depleted correctly from Sellable
- **Proved:** Post-cutover orders fully deplete

### B. Pre-Cutover Baseline Validation (2026-09-16 ~11:21)

- 3 historical orders imported (purchase dates: 2025-09-12, 2026-05-30, 2026-07-12)
- 4 events created
- B matched C for all → D=0 → no stock movement
- **Proved:** Cutover V2 correctly prevents double-depletion

### C. Controlled Catch-Up (2026-09-16 ~12:43)

- Imported remaining orders within the catch-up window
- By end of catch-up: 41 orders, 52 events
- All events processed successfully (state='done')
- Zero manual_review, zero failed

### D. Ongoing Automation (2026-09-16 onward)

- Status Sync (cron 26) continued discovering orders
- By 2026-09-20: 43 orders, 55 events, 414 status sync jobs
- All events remain `done`, zero failures
- Reconciliation holds: 944 − 55 = 889 = actual Sellable

**IMPORTANT:** The numbers above are HISTORICAL VALIDATION RESULTS from Sept 16. The CURRENT state (Sept 20) is in Section 11.

---

## 10. Current Orders

*Verified from database 2026-09-20*

**43 amazon.sale.order records, 43 sale.order records.**

The 3 earliest orders (purchase dates 2025-09-12, 2026-05-30, 2026-07-12) are **pre-cutover orders** imported during baseline validation. Their events have D=0.

The remaining 40 orders are **post-cutover orders** (purchase dates 2026-09-15 onward). Their events deplete stock normally.

3 orders have `amazon_status = Canceled` — their events have D=0 (no fulfillment to deplete).

---

## 11. Current Stock / Event State

*Verified from database 2026-09-20 ~20:10 UTC*

### FBA Stock

| Location | Quantity |
|---|---|
| FBA Sellable (31) | **889** |
| FBA Reserved (32) | 48 |
| FBA Unsellable (33) | 8 |
| FBA Transit (29) | 1,571 |
| FBA Sold/Customers (35) | 55 |

### Reconciliation

| Item | Value |
|---|---|
| Opening Sellable | 944 |
| Sum of D (all done events) | 55 |
| Expected | 944 − 55 = 889 |
| Actual quant at location 31 | 889 |
| **Match** | **YES** |

### Events

| State | Count |
|---|---|
| done | 55 |
| pending | 0 |
| manual_review | 0 |
| failed | 0 |

### Stock Moves/Pickings

| Type | Count |
|---|---|
| Total stock.move | 95 |
| Total stock.picking | 46 |
| Opening adjustments (11→FBA locations) | 49 |
| Sale depletion (31→35) | 46 moves, 55 units |

### Jobs

| Type | Total | Failed |
|---|---|---|
| Import jobs | 11 | 0 |
| Status sync jobs | 414 | 0 |

---

## 12. Automation / Crons

*Verified from database 2026-09-20*

`auto_sync_enabled` = **False** on instance 6.

### Active Crons (15)

| ID | Name | Interval | Effect | Risk |
|---|---|---|---|---|
| 25 | Process Order Import Jobs | 1 min | Executes queued import jobs | READ from Amazon |
| 26 | Sync Order Statuses | 15 min | Creates status sync jobs | READ from Amazon |
| 27 | Process FBA Sale Stock Events | 1 min | Depletes Sellable stock | WRITES Odoo stock |
| 35 | Poll Inbound Operations | 1 min | Processes inbound jobs | READ from Amazon |
| 36 | Synchronize Inbound Receiving | 30 min | Syncs receiving evidence | READ from Amazon |
| 39 | Enqueue Daily FBA Inventory Audits | daily | Creates audit runs | Internal |
| 40 | Process FBA Inventory Audits | 5 min | Reads Amazon inventory | READ from Amazon |
| 46 | Check Connection Health | 15 min | Tests API | READ from Amazon |
| 47 | Refresh Operations Dashboard | 15 min | Updates stats | Internal |
| 48 | Detect Stuck Jobs | 10 min | Flags stalled jobs | Internal |
| 49 | Dispatch Eligible Retries | 5 min | Retries failed ops | Mixed |
| 50 | Evaluate Operational Alerts | 15 min | Generates alerts | Internal |
| 51 | Clean Successful Operational Logs | daily | Cleans logs | Internal |
| 52 | Process Removal Orders and FBA Events | 5 min | Processes jobs | Mixed |
| 56 | Match FBA Reimbursements | daily | Links records | Internal |

### Inactive Crons (18)

| ID | Name | Risk | Why Disabled |
|---|---|---|---|
| 24 | Import All Orders | READ only | Was used at go-live, later deactivated. Safe to re-enable. |
| 28 | Sync Products | Low | No product sync policy |
| **29** | **Update Product Prices** | **WRITES AMAZON** | No price policy |
| 30 | Import Settlement Reports | READ only | Accounting not configured |
| 31 | Check Canceled Orders | Low | Legacy, covered by status sync |
| 32 | Import FBM Orders | N/A | FBA only |
| **33** | **Export Stock Levels** | **WRITES AMAZON** | FBA stock is Amazon-managed |
| 34 | Update FBM Order Status | N/A | FBA only |
| 37 | Refresh Removal Status | Low | Awaiting client |
| 38 | Enqueue Audits (Compatibility) | N/A | Replaced by 39 |
| 41 | Pull Prices | N/A | No-op |
| **42** | **Full Bidirectional Sync** | **WRITES AMAZON** | Never for FBA |
| **43** | **Master Auto-Sync Scheduler** | **WRITES AMAZON** | Must stay off |
| 44 | Smart Alert Scan | Low | Optional |
| 45 | Calculate Product Health Scores | Low | Optional |
| 53 | Import FBA Customer Returns | READ only | Awaiting client |
| 54 | Import FBA Inventory Adjustments | READ only | Awaiting client |
| 55 | Import FBA Reimbursements | READ only | Awaiting client |

---

## 13. Amazon Write Operations

### READ-ONLY / SAFE OPERATIONS

These only read from Amazon and write to the Odoo database:

- Order import (creates Odoo records from Amazon data)
- Status sync (reads order updates)
- Inventory audit (reads inventory levels)
- Settlement import (reads settlement data)
- Returns/adjustments/reimbursements import (reads evidence)
- Inbound receiving sync (reads receipt evidence)
- Product sync (reads catalog)
- Connection health check
- Price pull

### AMAZON WRITE OPERATIONS — REQUIRE EXPLICIT APPROVAL

| Operation | Cron | Interval Field | What It Changes on Amazon |
|---|---|---|---|
| **Export Stock** | 33 | `stock_push_interval` | **Overwrites Amazon's FBA inventory counts** — could cause overselling/underselling |
| **Update Prices** | 29 | `price_push_interval` | **Changes product prices on Amazon listings** — could affect revenue |
| **Full Sync** | 42 | N/A | **Runs all operations including stock export and price push** |
| **Master Scheduler** | 43 | `auto_sync_enabled` | **Dispatches all sync types** for instances where auto_sync=True |

All four are currently disabled with **dual protection**: cron inactive AND interval/flag set to disabled/False.

### Inbound Shipment Submission

Inbound operations (creating plans, confirming packing/placement/shipping) DO write to Amazon, but this is an intended operational workflow managed through the Odoo UI, not an automated background operation.

---

## 14. Accounting / Settlement Status

**Current state: NOT CONFIGURED. Zero settlements imported. Zero accounting entries.**

### What Is Ready

- Settlement import code is implemented
- Journal entry creation code is implemented
- Payout matching code is implemented
- Settlement accounting strategy is set to `settlement_based`

### What Is Missing (BLOCKING)

All 15 account/journal fields on instance 6 are NULL:

1. Settlement Journal
2. Amazon Payout Bank Journal
3. Amazon Clearing Account
4. Amazon Sales Account
5. Amazon Fee Account
6. Amazon FBA Fee Account
7. Amazon Refund Account
8. Amazon Reimbursement Account
9. Amazon Shipping Account
10. Amazon Promotion Account
11. Amazon Tax Account
12. Amazon Adjustment Account
13. Amazon Other Credit Account
14. Amazon Other Debit Account
15. Amazon Suspense Account

Plus: Settlement cutoff date is not set. Egyptian tax configuration is not done.

### First Settlement Test Procedure (DO NOT EXECUTE NOW)

1. Accountant configures all accounts/journals on instance form
2. Set settlement_accounting_cutoff_date
3. Click "Import Settlements" button — reads ONE settlement from Amazon
4. Review imported settlement lines
5. Click "Create Accounting Entry" — creates DRAFT only
6. Accountant reviews draft entry
7. If correct: accountant posts via standard Odoo "Post" button
8. If incorrect: fix mappings, delete draft, repeat
9. Repeat with 2-3 more settlements
10. Only then consider enabling cron 30 and setting `settlement_sync_interval` to `daily`

See `docs/IMPLEMENTER_HANDOFF.md` Sections 14-15 for complete details.

---

## 15. Implementer Responsibilities

### Must Do

- Business configuration of settlement accounting (with accountant)
- Account mapping and journal setup
- Settlement validation and UAT
- Product mapping governance (monitor for new SKUs)
- Daily and weekly operational monitoring
- Client communication for feature enablement decisions

### Must NOT Do Without Developer

- Modify Cutover V2 (Run 3, baselines, timestamps)
- Enable crons 29, 33, 42, or 43
- Change `auto_sync_enabled`, `stock_push_interval`, or `price_push_interval`
- Re-seed opening inventory
- Modify FBA location IDs
- Run manual SQL against production
- Delete events or stock moves

---

## 16. Client Responsibilities

Decisions required from the client:

1. **Chart of Accounts** for Amazon Egypt (with accountant)
2. **Settlement cutoff date** — from when to create accounting entries
3. **Commission/fee treatment** — how to account for Amazon fees
4. **Refund treatment** — contra-revenue or expense
5. **Egyptian tax treatment** — VAT-inclusive, mappings
6. **Product sync policy** — Odoo master or Amazon master
7. **Price management policy** — push from Odoo or manage on Amazon
8. **Returns/adjustments/reimbursements** — approve import enablement
9. **Removal tracking** — approve if using FBA removals
10. **Settlement UAT sign-off** — approve entry structure before automation

---

## 17. Technical Owner Responsibilities

Remains the responsibility of the developer/technical owner:

- Connector code maintenance and bug fixes
- Cutover V2 integrity
- FBA sale stock event logic
- Amazon API changes and SP-API updates
- Race condition handling
- Production incident response
- Module upgrades and migrations
- Cron strategy changes (requires developer approval)
- Code deployments

---

## 18. Troubleshooting Runbook

See `docs/OPERATIONS_RUNBOOK.md` Section 4 for detailed troubleshooting procedures covering:

1. Orders stopped importing
2. Order exists in Amazon but not in Odoo
3. Stock not depleted after import
4. Double depletion suspected
5. Event stuck in pending
6. Event in manual_review
7. Amazon/Odoo stock mismatch
8. SP-API quota exceeded
9. Authentication failure
10. Cron not running
11. Unmapped SKU

---

## 19. Server Operations

See `docs/OPERATIONS_RUNBOOK.md` for complete server operations including:

- Service management (start/stop/restart)
- Log viewing
- Git operations
- Module upgrade procedure
- Database queries
- Backup procedure

Key paths:
- Odoo config: `/etc/odoo19.conf`
- Odoo logs: `/var/log/odoo19/odoo19.log`
- Odoo venv: `/opt/odoo19/venv/`
- Module: `/opt/odoo19/custom-addons/amazon-odoo/sdlc_amazon_connector/`

---

## 20. Test Suite

21 test files covering: Cutover V2, FBA sale stock, stock structure, dispatch, inbound (4 phases), receiving, inventory reconciliation, operations, Phase 7 events, customer returns, removal/disposal, reimbursements, orders API, settlement accounting, settlement payout, payout clearing, end-to-end.

**DO NOT run tests on production database.** Use `test_cutover_v2` or create a fresh test database.

To run tests:
```bash
/opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
  -c /etc/odoo19.conf \
  --test-enable \
  --test-tags sdlc_amazon_connector \
  -d test_cutover_v2 \
  --stop-after-init
```

---

## 21. Decision Log

| Decision | Reason | Consequence | Can It Be Changed? | Who Approves? |
|---|---|---|---|---|
| Cutover V2 exists | Prevents double-depletion of ~13K historical orders | P initialized to B at event creation; D = C - P | No — changing would corrupt historical data | Developer only |
| Opening inventory seeded from FBAAUDIT/00007 | Establishes Odoo's starting FBA stock from Amazon's actual counts | 944 Sellable is the permanent baseline | No — must never re-seed | Developer only |
| Cutover boundary = snapshot start (2026-09-15 21:02:11) | Orders before this timestamp had fulfillments baked into opening stock | Any order with purchase_date before this is checked against baseline | No | Developer only |
| 1-year history coverage window | Covers orders as old as 2025-09-15 | Orders older than this with B=0 go to manual_review | Could be extended but requires new baseline data | Developer |
| Stock depletion is delta-based | Supports partial fulfillment, re-import, idempotency | D = C - P; only new units are depleted | No — core design | Developer only |
| Amazon write operations disabled | FBA stock is Amazon-managed; no price policy approved | Stock push, price push, full sync, master scheduler all off | Yes — with explicit client + developer approval | Client + Developer |
| Status sync is primary order discovery | Catches new and updated orders via LastUpdatedAfter | Cron 26 runs every 15 min; cron 24 available as backup | Yes — cron 24 can be re-enabled | Developer |
| Settlement strategy = settlement_based | Simpler than invoice-aware; recommended for initial setup | Each settlement creates one journal entry | Yes — accountant decision | Accountant |
| Returns are informational only | Returned stock goes to Amazon warehouse, not seller | Returns don't increase Odoo stock | Can be changed to stock-moving | Client + Developer |

---

## 22. Open Items / Next Steps

### P0 — Must Complete Before Financial Integration

| # | Item | Owner | Type |
|---|---|---|---|
| 1 | Configure all 15 settlement account/journal fields | Accountant + Implementer | Accounting |
| 2 | Set settlement_accounting_cutoff_date | Accountant | Accounting |
| 3 | Configure Egyptian tax treatment | Accountant + Tax advisor | Accounting |
| 4 | Test first settlement import | Implementer | Functional |
| 5 | Create and validate draft journal entry | Accountant | Accounting |
| 6 | Configure payout bank journal | Accountant | Accounting |

### P1 — Should Complete Soon

| # | Item | Owner | Type |
|---|---|---|---|
| 7 | Establish daily/weekly monitoring routine | Implementer | Monitoring |
| 8 | Client decision on returns/adjustments/reimbursements import | Client | Business |
| 9 | Enable settlement import automation (after P0 complete) | Implementer | Functional |
| 10 | Push uncommitted documentation to git remote | Developer | Technical |

### P2 — Enhancement / Optional

| # | Item | Owner | Type |
|---|---|---|---|
| 11 | Client decision on product sync policy | Client | Business |
| 12 | Client decision on price management policy | Client | Business |
| 13 | AI features configuration (API key) | Client | Optional |
| 14 | Re-enable cron 24 for additional import coverage | Developer | Technical |
| 15 | Removal order tracking setup | Client | Business |

---

## 23. Temporary Production Scripts

These scripts were found in `/tmp/` on the server. They were used during the initial go-live and Cutover V2 activation.

| Script | Purpose | Safe to Re-Run? |
|---|---|---|
| `/tmp/build_cutover.py` | Early cutover build attempt | **NO — HISTORICAL ONLY** |
| `/tmp/build_cutover_v2.py` | Cutover V2 build | **NO — HISTORICAL ONLY** |
| `/tmp/build_baselines_safe.py` | Safe baseline building | **NO — baselines already built** |
| `/tmp/resume_cutover_build.py` | Resume interrupted build | **NO — HISTORICAL ONLY** |
| `/tmp/activate_cutover_v2.py` | Activate cutover run | **NO — already activated** |
| `/tmp/seed_fba_opening_stock.py` | Seed opening inventory | **NO — NEVER RE-RUN** |
| `/tmp/fresh_amazon_snapshot.py` | Take Amazon inventory snapshot | Read-only, could be reused for diagnostics |
| `/tmp/catchup_audit_snapshot.py` | Catch-up audit snapshot | Read-only, could be reused |
| `/tmp/list_post_cutover_orders.py` | List post-cutover orders | Read-only diagnostic |
| `/tmp/precutover_baseline_test.py` | Validate pre-cutover baselines | Read-only diagnostic |

**DO NOT move these into the module.** They are one-time production scripts, not reusable code.

---

## 24. Documentation Index

| File | Purpose | Current? | Must Read? | Audience |
|---|---|---|---|---|
| `CLAUDE.md` | AI operating rules, safety constraints | Current | YES | AI/Developer |
| `AGENTS.md` | AI agent configuration | Current | No | AI |
| `docs/PROJECT_CONTEXT.md` | Project background | Current | Useful | Developer |
| `docs/ARCHITECTURE.md` | Technical architecture | Current | Useful | Developer |
| `docs/BUSINESS_REQUIREMENTS.md` | Business requirements | Current | Useful | Implementer |
| `docs/DEVELOPMENT_WORKFLOW.md` | Development process | Current | Useful | Developer |
| `docs/TESTING_GUIDE.md` | Test procedures | Current | Useful | Developer |
| `docs/SECURITY_AND_SECRETS.md` | Security guidelines | Current | YES | All |
| `docs/AMAZON_API_GUIDELINES.md` | SP-API usage notes | Current | Useful | Developer |
| `docs/DECISIONS.md` | Architectural decisions | Current | YES | Developer |
| `docs/CHANGELOG.md` | Change history | Partially current | No | Developer |
| `docs/CURRENT_STATUS.md` | Feature status overview | **Updated 2026-09-16** — numbers are from Sep 16 snapshot | YES | All |
| `docs/CURRENT_STATUS_AR.md` | Arabic translation of status | Same as above | Implementer (Arabic) | Arabic implementer |
| `docs/IMPLEMENTER_HANDOFF.md` | Complete implementer guide (25 sections) | **Created 2026-09-16** — production values from Sep 16 | YES | Implementer |
| `docs/IMPLEMENTER_HANDOFF_AR.md` | Arabic translation of handoff | Same as above | Implementer (Arabic) | Arabic implementer |
| `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md` | **Verified current production state** | **Created 2026-09-20** — LATEST | YES | All |
| `docs/ACCOUNT_MIGRATION_MASTER_HANDOFF.md` | This document | Created 2026-09-20 | YES | AI/Developer |
| `docs/ACCOUNT_MIGRATION_QUICK_START.md` | Short version for new chat | Created 2026-09-20 | YES | AI |
| `docs/AI_CONTINUATION_PROMPT.md` | Ready-to-paste prompt | Created 2026-09-20 | YES | AI |
| `docs/OPERATIONS_RUNBOOK.md` | Server ops and troubleshooting | Created 2026-09-20 | YES | Operations |
| `docs/IMPLEMENTER_FIRST_DAY.md` | Day 1 quick guide | Created 2026-09-20 | Useful | Implementer |
| `docs/AMAZON_FBA_IMPLEMENTER_HANDOFF.md` | Older implementer handoff | **Outdated** — superseded by IMPLEMENTER_HANDOFF.md | No | — |
| `docs/AMAZON_FBA_BUSINESS_FLOW_SUMMARY.md` | Business flow summary | Older but useful background | Optional | Implementer |
| `sdlc_amazon_connector/docs/` | Module-level technical docs | Current | Developer | Developer |

### Contradictions Found

- `docs/CURRENT_STATUS.md` and `docs/IMPLEMENTER_HANDOFF.md` show production values from **2026-09-16 17:15 UTC** (Sellable=902, Sum(D)=42, 23 status sync jobs). The actual state as of 2026-09-20 is: Sellable=889, Sum(D)=55, 414 status sync jobs. These documents were NOT updated because they represent the verified state at their creation time. The latest verified state is in `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md`.

---

*This document was prepared on 2026-09-20 using read-only verification against the production database `amazon_prod12sep` and source code inspection. No production data, configuration, or code was modified during preparation. No secrets were included.*
