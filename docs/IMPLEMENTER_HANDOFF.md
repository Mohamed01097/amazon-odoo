# Amazon Egypt FBA Connector — Implementer Onboarding & Handoff

**Module:** `sdlc_amazon_connector`
**Odoo Version:** 19 Community
**Database:** `amazon_prod12sep`
**Amazon Instance:** Amazon Egypt Production (id=6)
**Marketplace:** Amazon Egypt (`ARBP9OOSHTCHU`)
**Seller ID:** `A2LMV58FMUN2ZD`
**Handoff Date:** 2026-09-16 UTC
**Document Scope:** Complete onboarding, functional guide, operating manual, accounting handoff, and safety guide

---

## Table of Contents

1. [Executive Overview](#1-executive-overview)
2. [Module Tour for a First-Time Implementer](#2-module-tour)
3. [Amazon Instance Configuration](#3-instance-configuration)
4. [Product & SKU Mapping](#4-product-sku-mapping)
5. [FBA Inventory Architecture](#5-fba-inventory-architecture)
6. [Opening Inventory / Go-Live History](#6-opening-inventory)
7. [Cutover V2 — Explained for Functional Users](#7-cutover-v2)
8. [Order Import Workflow](#8-order-import-workflow)
9. [Current Live Automation](#9-current-live-automation)
10. [Critical Safety Rules](#10-critical-safety-rules)
11. [Current Production Status](#11-current-production-status)
12. [Monitoring & Daily Operations](#12-monitoring-daily-operations)
13. [Troubleshooting Guide](#13-troubleshooting-guide)
14. [Accounting & Settlement Handoff](#14-accounting-settlement-handoff)
15. [First Settlement Test Procedure](#15-first-settlement-test)
16. [Returns / Removals / Reimbursements](#16-returns-removals-reimbursements)
17. [Inbound FBA Shipments](#17-inbound-fba)
18. [Product / Price Policy Decisions](#18-product-price-policy)
19. [Responsibility Matrix](#19-responsibility-matrix)
20. [Do-Not-Touch List](#20-do-not-touch-list)
21. [Implementer First-Day Checklist](#21-first-day-checklist)
22. [Go-Live Status Matrix](#22-go-live-status-matrix)
23. [Final Handoff Summary](#23-final-handoff-summary)
24. [Implementer Start Here](#implementer-start-here)
25. [Handoff Acceptance Checklist](#handoff-acceptance-checklist)

---

## 1. Executive Overview

### What This Integration Does

This custom Odoo module connects Amazon Egypt's Seller Central to Odoo 19 Community. It allows the business to manage Amazon sales, inventory, and financial data inside Odoo without needing to switch between systems.

The client sells car care products (IONIC brand) on Amazon Egypt using **Fulfillment by Amazon (FBA)**. This means:

- Products are stored in Amazon's warehouse in Egypt.
- Amazon picks, packs, and ships orders to customers.
- Amazon collects payment from customers.
- Amazon periodically settles payments to the seller's bank account, after deducting fees and commissions.

Odoo's role is to:

- Know what orders Amazon received and fulfilled.
- Track FBA inventory levels (how many units Amazon holds).
- Record the financial impact of sales, fees, refunds, and payouts.
- Provide a single source of truth for business reporting.

### How Amazon and Odoo Communicate

```
Amazon Seller Central
        ↕
Amazon SP-API (Selling Partner API)
        ↕
sdlc_amazon_connector (this Odoo module)
        ↕
Odoo Database (orders, products, stock, accounting)
```

The connector uses Amazon's SP-API to **read** order data, inventory levels, settlements, returns, and other evidence. It can also **write** to Amazon (push prices, push stock levels), but those write features are intentionally disabled for this FBA rollout.

### High-Level Data Flows

#### Orders

Amazon receives a customer order → The connector imports it into Odoo → An Odoo sale order is created → When Amazon fulfills the order, Odoo records the fulfillment and reduces FBA inventory.

#### Inventory

Amazon holds physical stock in its warehouse → The connector reads Amazon's inventory report daily → Odoo maintains its own FBA inventory records that are depleted as orders are fulfilled → Daily audits compare Odoo's records against Amazon's actual counts.

#### Products

Amazon has product listings identified by SKU → Each Amazon SKU is linked to an Odoo product → This mapping is required for orders and inventory to work correctly.

#### Prices

Prices can be pulled from Amazon (read) or pushed to Amazon (write). Currently, **price push is disabled**. Price decisions require client approval.

#### Settlements

Amazon sends settlement reports every 2 weeks showing: revenue collected, fees deducted, refunds, reimbursements, and the net payout amount. The connector can import these and create draft accounting entries. **This is not yet configured** — it requires the accountant to set up journals and account mappings first.

#### Payments / Payouts

Amazon deposits the net settlement amount into the seller's bank. The connector can record payout evidence and help reconcile the Amazon clearing account against bank transactions. **Not yet configured.**

#### Returns

When a customer returns a product to Amazon's FBA warehouse, the connector can import the return evidence. Returns do **not** automatically increase Odoo stock — they are informational records. **Not yet enabled.**

#### Removals

The seller can request Amazon to ship FBA stock back or dispose of it. The connector tracks removal orders and shipment evidence. **Not yet enabled.**

#### Reimbursements

Amazon may reimburse the seller for lost or damaged FBA inventory. The connector imports reimbursement records as financial evidence. **Not yet enabled.**

#### Inbound FBA Shipments

When the seller ships new stock to Amazon's FBA warehouse, the connector manages the inbound plan: packing, placement, labels, tracking, dispatch, and receiving. The inbound workflow is **active** and can be used through the Odoo interface.

---

## 2. Module Tour

After logging into Odoo, look for the **Amazon** top-level menu in the main navigation bar. All connector functionality lives under this menu.

### Menu Structure

#### Amazon > Dashboard

- **Purpose:** Overview of the Amazon integration status.
- **What you see:** Summary cards, connection status, recent activity.
- **Actions:** Read-only overview. No dangerous operations.
- **When to use:** Daily — to get a quick health check.

#### Amazon > Configuration

| Submenu | Purpose | Actions |
|---|---|---|
| **Instances** | Amazon account connection settings | View/edit instance configuration |
| **Settings** | Module-level settings | System admin only |
| **User Guide** | Built-in documentation | Read-only |
| **AI Features Overview** | AI tool descriptions | Read-only |

- **When to use Instances:** To review connection settings, sync intervals, FBA locations, accounting mappings.
- **Caution:** Do not change interval fields or enable `auto_sync` without reading Section 10 (Safety Rules).

#### Amazon > Alerts

| Submenu | Purpose |
|---|---|
| **Active Alerts** | Current operational alerts (connection issues, stuck jobs, anomalies) |
| **Alert History** | Past alerts |

- **Actions:** Read-only. Alerts are generated automatically.
- **When to use:** Daily — check for any connection or data issues.

#### Amazon > Catalog

| Submenu | Purpose | Actions |
|---|---|---|
| **Products** | Amazon product listings linked to Odoo products | View mappings, check SKU/ASIN |
| **Initial Product Setup** | First-time product setup wizard | **WRITE** — creates/links products |
| **Import / Map Products** | Import or link Amazon products | **WRITE** — creates/links products |

- **When to use Products:** To verify all Amazon SKUs are mapped to Odoo products.
- **When to use Setup/Import:** Only when adding new products or fixing unmapped SKUs. Discuss with operations first.

#### Amazon > Orders

| Submenu | Purpose | Actions |
|---|---|---|
| **All Orders** | All imported Amazon orders | View orders and their Odoo sale orders |
| **FBM Orders** | Merchant-fulfilled orders | Not used (FBA only) |
| **FBA Orders** | FBA-fulfilled orders | View FBA orders |
| **Import Jobs** | Order import job history | View job status, errors, progress |
| **Status Sync Jobs** | Order status sync job history | View status sync progress |

- **Important buttons on orders:** None that modify Amazon. Orders are read-only records of what Amazon reported.
- **When to use:** To verify orders are importing correctly. Check Import Jobs if orders seem missing.

#### Amazon > Delivery

| Submenu | Purpose |
|---|---|
| **All Deliveries** | Odoo delivery orders linked to Amazon orders |
| **Pending / Shipped / Delivered / Cancelled** | Filtered views by status |
| **Tracking Numbers** | Shipment tracking information |

- **Note:** For FBA, Amazon handles delivery. These records reflect Amazon's fulfillment evidence.

#### Amazon > FBA

This is the most important section for FBA operations.

| Submenu | Purpose | Actions |
|---|---|---|
| **Inbound Shipments** | Send new stock to Amazon's warehouse | **WRITE** — creates inbound plans, submits to Amazon |
| **Inventory Health** | FBA inventory audit runs comparing Odoo vs Amazon | View audit results |
| **FBA Sale Stock Events** | Individual fulfillment depletion records | View event processing status |
| **Inventory Differences** | Per-SKU comparison from audits | View discrepancies |
| **Removal Orders** | Requests to return/dispose FBA stock | **WRITE** — can submit removal requests to Amazon |
| **Removal Shipments** | Shipment evidence for removals | View shipment status |
| **Disposal Orders** | Disposal evidence | View |
| **Legacy Inventory Reports** | Older inventory report format | View |
| **MCF Outbound Orders** | Multi-Channel Fulfillment | Not currently used |

- **Key screen — FBA Sale Stock Events:** This shows every fulfillment event. Each row represents one order item's fulfillment. The `state` column tells you if it was processed successfully (`done`), is waiting (`pending`), needs attention (`manual_review`), or failed.
- **Key screen — Inventory Health:** Shows daily audit comparisons. Open a completed audit to see per-SKU Odoo vs Amazon quantities.

#### Amazon > AI Tools

AI-powered features (pricing suggestions, listing optimization, demand forecasting, etc.). These are **optional** and require an AI API key to be configured. Currently **not configured**.

#### Amazon > Returns & Refunds

| Submenu | Purpose | Current State |
|---|---|---|
| **Customer Returns** | FBA customer return evidence | Not yet importing |
| **Return Import Runs** | Import job history | Empty |
| **Inventory Adjustments** | Lost/damaged/found adjustments | Not yet importing |
| **Reimbursements** | Amazon reimbursement records | Not yet importing |

- **Actions:** Import buttons are **READ** from Amazon. They do not modify Amazon.
- **When to enable:** After client approval. See Section 16.

#### Amazon > Accounting

| Submenu | Purpose | Current State |
|---|---|---|
| **Settlement Reports** | Amazon settlement data | Empty — not yet importing |
| **Settlement Financial Lines** | Line-by-line settlement details | Empty |
| **Amazon Payouts** | Payout evidence and bank reconciliation | Empty |
| **VCS Tax Reports** | VAT Calculation Service reports | Not configured |

- **This entire section requires accountant configuration before use.** See Section 14.

#### Amazon > Reports

| Submenu | Purpose |
|---|---|
| **Seller Rating** | Amazon seller performance metrics |
| **Sync Reports** | Summary of sync operations |
| **Sync Logs (Raw)** | Detailed API call logs |

- **When to use Sync Logs:** When troubleshooting API errors or verifying what the connector sent/received.

### Amazon Instance Form

Open **Amazon > Configuration > Instances** and click on "Amazon Egypt Production". The form has multiple tabs/sections:

| Section | What It Contains | Status |
|---|---|---|
| **Connection** | API credentials, marketplace, seller ID | Configured |
| **FBA Configuration** | Warehouse, FBA locations, cutover date | Configured |
| **Sync Schedule** | Sync intervals and auto-sync toggle | Partially configured (see Safety Rules) |
| **Settlement / Accounting** | Journals, accounts, strategy | **NOT configured** |
| **AI Features** | AI API key and intervals | Not configured |

**Important buttons on the Instance form:**

| Button | Action | Safe? |
|---|---|---|
| **Test Connection** | Tests API connectivity | Yes — read-only |
| **Sync Products** | Pulls product catalog from Amazon | Mostly safe — may create new Amazon product records |
| **Import Orders** | Creates an order import job | Safe — read-only from Amazon, creates Odoo records |
| **Sync Status** | Syncs order status updates | Safe — read-only from Amazon |
| **Run Audit** | Creates an FBA inventory audit | Safe — read-only snapshot |
| **Export Stock** | **Pushes Odoo stock to Amazon** | **DANGEROUS — DO NOT USE** |
| **Push Prices** | **Pushes Odoo prices to Amazon** | **DANGEROUS — DO NOT USE** |
| **Full Sync** | Runs everything including exports | **DANGEROUS — DO NOT USE** |
| **Import Settlements** | Imports settlement reports | Safe to read, but requires accounting setup first |

---

## 3. Instance Configuration

### Connection Configuration

| Field | Current Value | Notes |
|---|---|---|
| Name | Amazon Egypt Production | Display name |
| Marketplace ID | ARBP9OOSHTCHU | Amazon Egypt marketplace |
| Seller ID | A2LMV58FMUN2ZD | Amazon seller account |
| Refresh Token | CONFIGURED | Do not change without developer |
| Client ID (LWA) | CONFIGURED | Do not change without developer |
| Client Secret (LWA) | CONFIGURED | Do not change without developer |
| Active | Yes | Instance is operational |
| Company | Eisa Heikal For Commercial & Industrial Investment (id=1) | Odoo company |

### FBA Configuration

| Field | Current Value | Notes |
|---|---|---|
| FBA Warehouse | WH (id=1) | Main Odoo warehouse |
| FBA Source Location | Physical Locations/WH/Stock (id=5) | Source for outbound shipments to Amazon |
| FBA Transit Location | Amazon FBA Transit (id=29) | In-transit to Amazon |
| FBA Received Location | WH/Stock/Amazon FBA Received / Staging (id=30) | Amazon confirmed receipt |
| FBA Sellable Location | WH/Stock/Amazon FBA Sellable (id=31) | Available for sale on Amazon |
| FBA Reserved Location | WH/Stock/Amazon FBA Reserved (id=32) | Reserved for pending orders |
| FBA Unsellable Location | WH/Stock/Amazon FBA Unsellable (id=33) | Damaged/defective at Amazon |
| FBA Return Source | Amazon FBA Customer Returns (id=34) | Customer return evidence |
| FBA Sold / Customers | Amazon FBA Sold / Customers (id=35) | Destination for fulfilled items |
| FBA Removal Transit | Amazon FBA Removal Transit (id=36) | Removal in transit |
| FBA Disposal / Loss | Amazon FBA Disposal / Inventory Loss (id=37) | Disposed or lost |
| Ship-From Partner | id=1 | Partner for inbound shipments |
| Removal Return Partner | id=1 | Partner for removal receipts |
| FBA Sale Stock Cutover | 2026-09-15 21:02:11 UTC | **DO NOT CHANGE** — See Section 7 |

### Order Synchronization

| Field | Current Value | Meaning | Risk of Changing |
|---|---|---|---|
| `order_sync_interval` | 30min | How often the master scheduler would queue order imports | Low (master scheduler is off) |
| `order_status_sync_enabled` | **True** | Status sync runs independently every 15 minutes | Do not disable without developer |
| `order_status_sync_interval` | 15 minutes | How often status sync checks for updates | Low — can adjust if needed |
| `last_order_sync` | 2026-09-16 13:56:46 | Last successful order import timestamp | Do not manually change |
| `last_status_sync_at` | 2026-09-16 17:11:18 | Last successful status sync timestamp | Do not manually change |
| `initial_order_import_from` | (not set) | Only used for first-ever import | Not relevant now |

### Product Synchronization

| Field | Current Value | Notes |
|---|---|---|
| `product_sync_interval` | daily | Would sync daily if master scheduler were on (it's off) |
| `last_product_sync` | 2026-09-14 10:29:25 | Last product sync |

### Stock Synchronization

| Field | Current Value | Notes |
|---|---|---|
| `stock_push_interval` | **disabled** | **Defense-in-depth — prevents accidental stock push** |
| `stock_pull_interval` | disabled | Stock pull is manual-only by design |
| `last_stock_sync` | 2026-09-16 13:26:47 | Last inventory audit timestamp |

### Price Synchronization

| Field | Current Value | Notes |
|---|---|---|
| `price_push_interval` | **disabled** | **Defense-in-depth — prevents accidental price push** |
| `price_pull_interval` | disabled | Price pull is manual-only by design |

### Settlement Synchronization

| Field | Current Value | Notes |
|---|---|---|
| `settlement_sync_interval` | **disabled** | **Defense-in-depth — accounting not configured** |
| `settlement_accounting_strategy` | settlement_based | Chosen strategy, but journals not configured |
| `settlement_accounting_cutoff_date` | (not set) | **Must be set by accountant before importing settlements** |
| `last_settlement_sync_at` | (never) | No settlements imported yet |

### AI Features

| Field | Current Value |
|---|---|
| AI API Key | NOT CONFIGURED |
| All AI intervals | weekly (but inert without API key) |

---

## 4. Product & SKU Mapping

### How It Works

Every product sold on Amazon has a **SKU** (Seller Stock Keeping Unit) — a unique code the seller assigns. Amazon also assigns an **ASIN** (Amazon Standard Identification Number) to each product listing.

The connector creates an `amazon.product` record for each Amazon SKU. This record must be linked to an Odoo `product.product` record for orders and inventory to work.

**If a SKU is not mapped to an Odoo product:**
- Orders containing that SKU will be imported, but the order line may be incomplete.
- FBA sale stock events cannot deplete inventory for unmapped products.
- Inventory audits will show the SKU as "unmapped."

**If a SKU is mapped:**
- Orders create complete Odoo sale orders with the correct product.
- Fulfillment events create stock moves from FBA Sellable to FBA Sold/Customers.
- Inventory audits can compare Odoo quantities against Amazon quantities.

### Current Product Mapping Status

All 19 Amazon products are **fully mapped** to Odoo products. All are IONIC car care products.

| # | SKU | ASIN | Odoo Product |
|---|---|---|---|
| 1 | 24-BHT6-LWJ7 | B0BPYF4S5H | IONIC Dashboard Protectant |
| 2 | 6224003090026 | B0DGLR6B23 | IONIC No Rinse Wash & Wax (1kg) |
| 3 | 6225000411319 | B0BPYB4RPR | IONIC Car Shampoo Concentrated |
| 4 | 6225000411388 | B0CHFQGVQR | Ionic Nano Ceramic and Wax |
| 5 | 7C-ADQ8-HTSV | B0HHYVDW2Y | IONIC Microfiber Car Wash Mitt |
| 6 | 7S-2OKY-YSE0 | B0BPYM96H5 | IONIC Interior Detailer |
| 7 | 9A-J2EN-DBFW | B0F5BM9THK | IONIC Microfiber Car Drying Towel |
| 8 | 9J-KM0L-FO5I | B0BPYJR7WQ | IONIC Tire Shine |
| 9 | A6-RDKR-RHZ6 | B0BPYLBBRP | IONIC Waterless Wash & Wax |
| 10 | FD-FUU2-SGIC | B0F5BGGYJJ | IONIC Microfiber Cleaning Towels |
| 11 | IONIC-KIT-CS-01 | B0HHFT3TPJ | IONIC Car Shampoo Wash Kit |
| 12 | IONIC-KIT-NR-01 | B0HHFFC77D | IONIC No Rinse Car Wash Kit |
| 13 | Ion-1110 | B0BZZKFQWR | IONIC No Rinse Wash & Wax |
| 14 | J6-TVWG-OV1I | B0DDLF1VCG | IONIC Car Shampoo |
| 15 | PE-FCJU-3D68 | B0FPG8TY71 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 16 | PX-KEWE-P4MR | B0FPG9SN51 | IONIC Ultra Thick Microfiber (1200 GSM) |
| 17 | UT-6XOE-DDRN | B0FPG9VHQV | IONIC Ultra Thick Microfiber (1200 GSM) |
| 18 | XT-K255-V5H8 | B0BPYK1FKS | IONIC Car Leather Conditioner |
| 19 | XL-ALNF-5LTF | B0FPG9TV9F | IONIC Ultra Thick Microfiber (1200 GSM) |

### Unmapped SKUs in Inventory Audits

All 19 `amazon.product` records in the connector are fully mapped to Odoo products. However, Amazon's inventory API may return additional SKUs that exist on the Amazon side but have no corresponding `amazon.product` record in Odoo. These appear as "unmapped" in inventory audit results.

**Current unmapped SKUs (as of 2026-09-16):**

| SKU | Amazon Sellable | Amazon Reserved | Amazon Unsellable | Amazon Inbound |
|---|---|---|---|---|
| M7-X72T-G8NN | 0 | 0 | 0 | 0 |
| O8-DZJQ-EDXW | 0 | 0 | 0 | 0 |

Both SKUs have zero quantity across all Amazon inventory categories. They may be inactive or delisted Amazon listings. They do **not** affect order processing or stock depletion because no orders reference them.

**What to watch for:**
- If an unmapped SKU develops non-zero quantity, it means Amazon is holding stock for a product Odoo doesn't know about. Investigate and map it.
- If a new customer order references an unmapped SKU, the order will import but the FBA sale stock event will enter `manual_review` with error code `UNMAPPED_FBA_SKU`.
- Use **Amazon > Catalog > Products** to check mapping status.
- Use **Amazon > Catalog > Import / Map Products** to link new SKUs to Odoo products.

---

## 5. FBA Inventory Architecture

### The Concept

Amazon's FBA warehouse holds the seller's physical stock. The connector mirrors this in Odoo using special stock locations that represent different states of inventory at Amazon.

**Think of it like this:** Odoo doesn't physically control FBA stock. Instead, it maintains a **shadow copy** of what Amazon reports, so the business can see inventory levels, track depletion from sales, and reconcile with Amazon's actual counts.

### FBA Location Map

```
                                ┌─────────────────────┐
Seller Warehouse (WH/Stock) ───→│ Amazon FBA Transit   │ (id=29, transit)
                                │ Stock in transit to  │
                                │ Amazon's warehouse   │
                                └─────────┬───────────┘
                                          ↓
                                ┌─────────────────────┐
                                │ FBA Received/Staging │ (id=30, internal)
                                │ Amazon confirmed     │
                                │ receipt, pending     │
                                │ disposition          │
                                └─────────┬───────────┘
                                          ↓
                    ┌─────────────────────┼──────────────────────┐
                    ↓                     ↓                      ↓
          ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
          │ FBA Sellable    │   │ FBA Reserved     │   │ FBA Unsellable  │
          │ (id=31)         │   │ (id=32)          │   │ (id=33)         │
          │ Available for   │   │ Reserved for     │   │ Damaged or      │
          │ sale on Amazon  │   │ pending orders   │   │ defective       │
          └────────┬────────┘   └──────────────────┘   └─────────────────┘
                   ↓
          ┌─────────────────┐
          │ FBA Sold /      │ (id=35, customer)
          │ Customers       │
          │ Fulfilled and   │
          │ shipped to      │
          │ customer        │
          └─────────────────┘
```

**Additional locations:**

| Location | ID | Purpose |
|---|---|---|
| FBA Customer Returns | 34 | Evidence location for customer returns |
| FBA Removal Transit | 36 | Stock being returned from Amazon to seller |
| FBA Disposal / Inventory Loss | 37 | Disposed of or lost by Amazon |

### What Each Location Means

| Location | Stock arrives from | Stock leaves to | What moves it |
|---|---|---|---|
| **FBA Transit** (29) | WH/Stock (dispatch picking) | FBA Received (receiving sync) | Inbound shipment dispatch → receiving |
| **FBA Received** (30) | FBA Transit (receiving) | Sellable/Reserved/Unsellable (reviewed transfer) | Reviewed inventory disposition |
| **FBA Sellable** (31) | FBA Received (disposition) | FBA Sold/Customers (fulfillment) | FBA sale stock events (automatic) |
| **FBA Reserved** (32) | Internal Amazon movement | Back to Sellable or to Sold | Amazon-managed; reflected in audits |
| **FBA Unsellable** (33) | Damage/defect at Amazon | Removal or disposal | Amazon-managed; reflected in audits |
| **FBA Sold / Customers** (35) | FBA Sellable (fulfillment) | — | FBA sale stock events (automatic) |
| **FBA Removal Transit** (36) | FBA stock (removal) | WH/Stock (physical receipt) | Removal shipment evidence |
| **FBA Disposal** (37) | FBA stock (disposal) | — | Disposal evidence |

### Operational Stock vs Amazon Evidence

- **Odoo Operational Stock** = FBA Sellable + FBA Reserved + FBA Unsellable + FBA Transit + FBA Received
- **Amazon Reports** = Sellable + Reserved + Unsellable + Inbound (shipped + receiving)

Daily inventory audits compare these. Small differences are normal due to timing (orders fulfilled between import and audit). Large persistent differences require investigation.

---

## 6. Opening Inventory / Go-Live History

### What Happened

On **September 15, 2026**, the FBA integration went live. The opening FBA stock was seeded into Odoo based on a snapshot of Amazon's actual FBA inventory at that moment.

### Opening Snapshot

| Reference | Details |
|---|---|
| Audit Reference | FBAAUDIT/00007 |
| Snapshot Time | 2026-09-15 21:02:11 UTC |
| Method | Amazon `getInventorySummaries` API → standard Odoo inventory adjustments |

### Opening Quantities

| FBA Location | Opening Quantity |
|---|---|
| Sellable | 944 |
| Reserved | 48 |
| Unsellable | 8 |
| Transit | 1,571 |
| **Total** | **2,571** |

### Important Rules

- This opening stock was a one-time operation. **DO NOT seed it again.**
- The opening quantities were verified against Amazon's live API at the time.
- All subsequent inventory changes come from order fulfillments, inbound receiving, or reviewed adjustments — never from re-importing the opening snapshot.
- The audit reference (FBAAUDIT/00007) is the permanent record of this baseline.

---

## 7. Cutover V2 — Explained for Functional Users

### The Business Problem

Before Odoo went live on September 15, 2026, Amazon had already been fulfilling orders for about a year. Those historical orders had already reduced the physical FBA stock.

When the opening inventory was loaded into Odoo (944 Sellable units), that number **already reflected** all the historical fulfillments. For example, if Amazon started with 1,000 units and fulfilled 56 orders, the opening snapshot showed 944 units remaining.

**The problem:** When Odoo imports those old historical orders and sees "Amazon fulfilled 1 unit," it would naturally want to subtract 1 unit from Sellable. But that fulfillment is already baked into the 944 opening number. Subtracting it again would be **double-counting**, making Odoo's stock incorrect.

### How Cutover V2 Solves This

Cutover V2 maintains a **baseline** — a record of how many units of each order item Amazon had already fulfilled before the cutover date. When an old order is imported, the system checks the baseline and only depletes the **net new** quantity.

**The key fields on each event:**

| Field | Abbreviation | Meaning |
|---|---|---|
| `amazon_cumulative_fulfilled_qty` | **C** | Total units Amazon reports as fulfilled for this order item (cumulative, not incremental) |
| `cutover_baseline_fulfilled_qty` | **B** | Units Amazon had already fulfilled *before* go-live, from the baseline snapshot |
| `processed_fulfilled_qty` | **P** | Units Odoo has already accounted for (either from baseline initialization or from previous stock depletions) |
| `last_delta_qty` | **D** | The most recent stock depletion that was applied |

**How it works — step by step:**

**Step 1 — Event creation (first import of a pre-cutover order item):**

The system looks up B from the baseline. It then **initializes P to B**. This is the critical step: by setting P = B at creation, the system records that B units were already fulfilled before go-live and should not be depleted again.

**Step 2 — Processing (stock depletion):**

The processor calculates: `D = C - P`

Because P was initialized to B, this effectively means `D = C - B` on the first processing. If D > 0, a stock picking moves D units from Sellable to Sold/Customers.

After processing, P is updated: `P = P + D` (which now equals C).

**Step 3 — Re-import (idempotent):**

If the same order item is imported again with the same C, the processor recalculates: `D = C - P`. Since P already equals C from the previous processing, D = 0. Nothing happens.

**Step 4 — Subsequent fulfillment update:**

If Amazon later reports a higher C (e.g., a partial shipment completed), the processor calculates D = C_new - P_previous. Only the *new* units are depleted.

**Example — Pre-cutover order:**

> Order item: Amazon fulfilled 5 units total (C = 5).
> Baseline: 4 of those were fulfilled before go-live (B = 4).
>
> At creation: P is initialized to 4 (= B).
> Processing: D = C - P = 5 - 4 = **1 unit** → 1 unit depleted from Sellable.
> After processing: P = 4 + 1 = 5.
>
> Re-import with C still = 5: D = 5 - 5 = **0** → Nothing happens.

**Example — Post-cutover order:**

> New order item: Amazon fulfilled 2 units (C = 2).
> No baseline applies (order is after cutover).
>
> At creation: P is initialized to 0.
> Processing: D = C - P = 2 - 0 = **2 units** → 2 units depleted from Sellable.
> After processing: P = 0 + 2 = 2.

**Why this matters:** The baseline (B) is never subtracted during processing — it is absorbed into P at event creation time. This means B is used exactly once. There is no risk of double-subtraction.

### Current Cutover V2 State

| Field | Value |
|---|---|
| Cutover Run | id=3 |
| State | **activated** |
| History Start | 2025-09-15 21:02:11 UTC (one year before cutover) |
| Cutover Timestamp | 2026-09-15 21:02:11 UTC |
| Baseline Count | **15,269** order item baselines |
| Unique Orders Covered | 13,321 |
| Unique SKUs Covered | 18 |
| Total Fulfilled Before Cutover | 16,860 units |

### What You Need to Know

1. **The cutover is working.** It was validated with real orders and confirmed to prevent double-depletion.
2. **Orders before the cutover date** (Sept 15, 2026 21:02:11 UTC) are automatically checked against the baseline. Only the net new fulfillment is depleted.
3. **Orders after the cutover date** are treated as 100% new. All fulfilled quantities are depleted from Sellable.

### Manual Review Conditions

Sometimes the system flags an event for manual review instead of processing it automatically:

| Condition | What It Means | What To Do |
|---|---|---|
| **CUTOVER_BASELINE_OUTSIDE_COVERAGE** | An old order's purchase date falls outside the 1-year baseline coverage window, and the baseline is zero | Investigate the order. It may be very old or have unusual dates. |
| **CUTOVER_BASELINE_EXCEEDS_CUMULATIVE** | The baseline says more units were fulfilled before cutover than Amazon now reports in total | Data anomaly. Check if Amazon adjusted the order. |

If you see `manual_review` events in **Amazon > FBA > FBA Sale Stock Events**, investigate before retrying. See Section 13 for troubleshooting.

### Critical Rules

> **DO NOT** rebuild Cutover Run 3.
>
> **DO NOT** modify the cutover timestamp (2026-09-15 21:02:11).
>
> **DO NOT** delete or modify baseline records.
>
> **DO NOT** create another active cutover run.
>
> These are permanent historical records. Changing them would corrupt the depletion logic for all orders.

---

## 8. Order Import Workflow

### How Orders Flow from Amazon to Odoo

```
Step 1: Amazon receives a customer order
                ↓
Step 2: Order Status Sync cron runs every 15 minutes
        Finds new and updated orders via LastUpdatedAfter
        Creates/updates amazon.sale.order records
                ↓
Step 3: For each order:
        → Creates or updates amazon.sale.order (Amazon order record)
        → Creates or updates sale.order (Odoo sale order)
        → For each FBA order item:
          → Creates or updates amazon.fba.sale.stock.event
                ↓
Step 4: Event processor cron runs every 1 minute
        For each pending event:
        → Checks Cutover V2 baseline (if pre-cutover order)
        → Calculates delta (new units to deplete)
        → If delta > 0: creates stock picking
          FBA Sellable → FBA Sold / Customers
        → Sets event state to 'done'
                ↓
Step 5: Sellable inventory is reduced
```

### Two Import Mechanisms

The connector has two ways to discover orders from Amazon:

**1. Order Import (CreatedAfter) — Cron 24, currently inactive**
- Finds **new** orders created since the last import.
- Uses the order's `PurchaseDate` (when the customer placed it).
- This cron was activated at go-live but has since been deactivated. It is a safe read-only dispatcher and can be re-enabled if needed.

**2. Order Status Sync (LastUpdatedAfter) — Cron 26, active every 15 minutes**
- Finds orders that were **created or updated** since the last sync.
- Catches both new orders and fulfillment changes on existing orders (e.g., partial shipments completing).
- This is the **currently active** mechanism for discovering orders.

**Current state:** The Order Status Sync (cron 26) is the primary order discovery mechanism. It catches new orders because any new order is also a recently "updated" order from Amazon's perspective. The separate Order Import (cron 24) provides an additional safety net using `CreatedAfter` if re-enabled.

### Duplicate Protection

- Each Amazon order has a unique `amazon_order_ref`. The connector will not create duplicate Odoo orders for the same Amazon order.
- Each FBA sale stock event is unique by `(amazon_order_ref, amazon_order_item_id)`. Processing the same order item again updates the existing event rather than creating a duplicate.
- The event processor uses **cumulative** quantities. If Amazon says "2 units fulfilled" and Odoo already processed 1, only 1 more unit is depleted. If Amazon still says "2 units fulfilled" on re-import, the delta is 0 — nothing happens.

### Partial Fulfillment

If Amazon fulfills an order in multiple shipments:
1. First import: C=1 → Odoo depletes 1 unit.
2. Status sync later: C=2 → Odoo depletes 1 more unit (delta = 2-1 = 1).
3. Status sync again: C=2 → Delta = 0, nothing happens.

### Cancellations

Order cancellations are detected by the status sync. A cancelled order does not create stock depletion (C=0). If an order was partially fulfilled before cancellation, only the fulfilled quantity is depleted.

---

## 9. Current Live Automation

### Active Crons (15 total)

These scheduled jobs are currently running automatically:

#### Primary Order Pipeline

| ID | Name | Interval | Purpose |
|---|---|---|---|
| **25** | Process Order Import Jobs | 1 min | Executes queued import jobs (calls Amazon API, creates orders) |
| **26** | Sync Order Statuses | 15 min | Creates and processes status sync jobs (catches fulfillment updates) |
| **27** | Process FBA Sale Stock Events | 1 min | Processes pending fulfillment events (depletes Sellable inventory) |

> **Note:** Cron 24 (Import All Orders) was activated during the initial go-live but has since been **deactivated**. New order imports are currently driven by the Order Status Sync (cron 26), which catches both new and updated orders via `LastUpdatedAfter`. If order import throughput needs to increase, cron 24 can be re-enabled — it is a safe read-only dispatcher. Consult the developer before changing.

#### Inbound Operations

| ID | Name | Interval | Purpose |
|---|---|---|---|
| **35** | Poll Inbound Operations | 1 min | Processes inbound shipment plan jobs |
| **36** | Synchronize Inbound Receiving | 30 min | Syncs receiving evidence from Amazon |

#### Inventory Monitoring

| ID | Name | Interval | Purpose |
|---|---|---|---|
| **39** | Enqueue Daily FBA Inventory Audits | daily | Creates daily audit runs |
| **40** | Process FBA Inventory Audits | 5 min | Executes audit runs (reads Amazon inventory) |

#### Infrastructure & Monitoring

| ID | Name | Interval | Purpose |
|---|---|---|---|
| **46** | Check Connection Health | 15 min | Tests API connectivity |
| **47** | Refresh Operations Dashboard | 15 min | Updates dashboard statistics |
| **48** | Detect Stuck Jobs | 10 min | Flags jobs that seem stalled |
| **49** | Dispatch Eligible Retries | 5 min | Retries safe failed operations |
| **50** | Evaluate Operational Alerts | 15 min | Generates alerts for anomalies |
| **51** | Clean Successful Operational Logs | daily | Cleans up old success logs |
| **52** | Process Removal Orders and FBA Events | 5 min | Processes removal/return/event jobs |
| **56** | Match FBA Reimbursements | daily | Links reimbursement records |

### Disabled Crons (18 total)

These are intentionally disabled. **Do not enable without reading Section 10.**

#### Order Import Dispatcher (Currently Off)

| ID | Name | Why Disabled |
|---|---|---|
| **24** | Import All Orders (FBM + FBA) | Was activated at go-live, later deactivated. Safe read-only cron — can be re-enabled if needed. Consult developer. |

#### Dangerous — Write to Amazon

| ID | Name | Why Disabled |
|---|---|---|
| **29** | Update Product Prices | **WRITES prices to Amazon** — no price policy approved |
| **33** | Export Stock Levels | **WRITES stock to Amazon** — FBA stock is Amazon-managed |
| **42** | Full Bidirectional Sync | **Bundles reads AND writes** — never enable for FBA |
| **43** | Master Auto-Sync Scheduler | **Dispatches everything including writes** — see Section 10 |

#### Not Yet Configured

| ID | Name | Why Disabled |
|---|---|---|
| **30** | Import Settlement Reports | Accounting not configured |
| **53** | Import FBA Customer Returns | Awaiting client approval |
| **54** | Import FBA Inventory Adjustments | Awaiting client approval |
| **55** | Import FBA Reimbursements | Awaiting client approval |
| **37** | Refresh Removal Status | Awaiting client approval |

#### Not Applicable

| ID | Name | Why Disabled |
|---|---|---|
| **32** | Import FBM Orders | FBA only — no merchant fulfillment |
| **34** | Update FBM Order Status | FBA only |
| **31** | Check Canceled Orders | Legacy — covered by status sync |
| **28** | Sync Products | No automated product sync policy |
| **38** | Enqueue Audits (Compatibility) | Replaced by cron 39 |
| **41** | Pull Prices | No-op by design |
| **44** | Smart Alert Scan | Optional |
| **45** | Calculate Product Health Scores | Optional |

---

## 10. Critical Safety Rules

### The Three Things That Must NEVER Happen

#### 1. Do Not Push Stock to Amazon

FBA inventory is managed by Amazon. Amazon knows what is in its warehouse. Pushing Odoo stock numbers to Amazon would overwrite Amazon's actual inventory counts with Odoo's shadow copy, which may not be identical due to timing.

**Consequences:** Amazon could list incorrect availability, leading to overselling or underselling.

**Protected by:**
- `stock_push_interval` = **disabled** on instance 6
- `Export Stock Levels` cron (id=33) = **inactive**
- `Full Bidirectional Sync` cron (id=42) = **inactive**
- `Master Auto-Sync Scheduler` cron (id=43) = **inactive**

#### 2. Do Not Push Prices to Amazon

No price push policy has been approved. Pushing prices could change Amazon listing prices to incorrect values.

**Protected by:**
- `price_push_interval` = **disabled** on instance 6
- `Update Product Prices` cron (id=29) = **inactive**

#### 3. Do Not Enable the Master Auto-Sync Scheduler

The Master Scheduler (`cron_amazon_master_scheduler`, id=43) is a dispatcher that runs every 15 minutes and triggers ALL sync operations for instances with `auto_sync_enabled = True`. This includes the dangerous stock push and price push.

Even though `stock_push_interval` and `price_push_interval` are set to `disabled` as defense-in-depth, enabling the Master Scheduler creates an unnecessary risk path.

**Protected by:**
- `auto_sync_enabled` = **False** on instance 6
- Master Scheduler cron (id=43) = **inactive**

### Defense-in-Depth Settings

These three interval fields were intentionally set to `disabled` as an extra safety layer:

| Field | Value | Purpose |
|---|---|---|
| `stock_push_interval` | disabled | Even if Master Scheduler is accidentally enabled, stock push will not fire |
| `price_push_interval` | disabled | Even if Master Scheduler is accidentally enabled, price push will not fire |
| `settlement_sync_interval` | disabled | Prevents settlement import before accounting is configured |

### Summary of Safety Controls

| What | Setting | Cron | Both Required? |
|---|---|---|---|
| Stock push | `stock_push_interval=disabled` | Export Stock (id=33) inactive | Either blocks it |
| Price push | `price_push_interval=disabled` | Update Prices (id=29) inactive | Either blocks it |
| Master dispatch | `auto_sync_enabled=False` | Master Scheduler (id=43) inactive | Either blocks it |
| Full sync | N/A | Full Sync (id=42) inactive | Cron must be active |
| Settlement import | `settlement_sync_interval=disabled` | Import Settlement (id=30) inactive | Either blocks it |

---

## 11. Current Production Status

*Snapshot taken: 2026-09-16 17:15 UTC*

### Order & Event Counts

| Metric | Count |
|---|---|
| Amazon orders imported | 43 |
| Odoo sale orders | 43 |
| FBA sale stock events (done) | 55 |
| FBA sale stock events (pending) | 0 |
| FBA sale stock events (manual_review) | 0 |
| FBA sale stock events (failed) | 0 |
| Stock moves | 82 |
| Stock pickings | 33 |
| Import jobs (total / failed) | 11 / 0 |
| Status sync jobs (total / failed) | 23 / 0 |

### FBA Inventory

| Location | Quantity |
|---|---|
| Sellable | 902 |
| Reserved | 48 |
| Unsellable | 8 |
| Transit | 1,571 |
| **Total** | **2,529** |

### Depletion Reconciliation

| Item | Value |
|---|---|
| Opening Sellable | 944 |
| Total depleted (Sum of D) | 42 |
| Expected Sellable | 944 − 42 = 902 |
| Actual Sellable | 902 |
| **Match** | **Yes** |

This reconciliation confirms internal consistency: every unit subtracted from Sellable is accounted for by a processed FBA sale stock event. It does **not** mean Odoo's Sellable will always exactly match Amazon's live Sellable count — see "Audit Interpretation" below.

### Latest Inventory Audit

| Audit | FBAAUDIT/00011 |
|---|---|
| Date | 2026-09-16 13:26 UTC |
| Amazon records read | 20 |
| Matched | 4 |
| Mismatched | 14 |
| Unmapped | 2 |
| Not returned | 1 |

### Audit Interpretation

**Depletion reconciliation** (944 − sum(D) = current Sellable) proves Odoo's *internal* stock accounting is correct — every event was processed exactly once with the right delta.

**Audit mismatches** compare Odoo's per-SKU quantities against Amazon's *live inventory snapshot*. Temporary differences are expected because:

- Orders fulfilled by Amazon between the last import and the audit snapshot reduce Amazon's count but not yet Odoo's.
- Amazon may reclassify stock between Sellable and Reserved for pending orders.
- Amazon-side inventory adjustments (lost, found, damaged) are not yet imported into Odoo.

The 14 mismatches in FBAAUDIT/00011 are predominantly Sellable/Reserved distribution differences from this timing effect. The 2 unmapped SKUs (M7-X72T-G8NN, O8-DZJQ-EDXW) are Amazon-side listings with zero quantity — see Section 4 for details.

**When to investigate:** If the same SKU shows a persistent and growing mismatch across multiple consecutive audits, or if the total Sellable gap exceeds 50 units, escalate to the developer.

---

## 12. Monitoring & Daily Operations

### Daily Check (5 minutes)

| # | What to Check | Where | What's Normal | Escalate If |
|---|---|---|---|---|
| 1 | Connection health | Amazon > Dashboard or Alerts | Green / healthy | Connection errors persist > 1 hour |
| 2 | Order import jobs | Amazon > Orders > Import Jobs | Recent jobs with state "done" | Jobs stuck in "running" or state "failed" |
| 3 | FBA sale stock events | Amazon > FBA > FBA Sale Stock Events, filter by state | All "done" | Any "manual_review" or "failed" events |
| 4 | Operational alerts | Amazon > Alerts > Active Alerts | Zero or informational alerts | Critical alerts |
| 5 | Sellable trend | Amazon > FBA > Inventory Health | Gradual decrease matching sales | Sudden large drops or increases |

### Weekly Check (15 minutes)

| # | What to Check | Where | What to Look For |
|---|---|---|---|
| 1 | Inventory audit comparison | Amazon > FBA > Inventory Health | Open latest completed audit. Sellable gap should be small (< 30 units). |
| 2 | Unmapped SKUs | Amazon > Catalog > Products | Any new Amazon SKUs without Odoo product links. Map them. |
| 3 | Stuck jobs | Amazon > Operations > Job Monitor | Jobs older than 24 hours in non-terminal state. |
| 4 | Sync logs | Amazon > Reports > Sync Logs | Recurring errors or throttling warnings. |
| 5 | Event statistics | Filter events by date | Are events being created and processed consistently? |

### What Is Normal

- **Small Sellable gaps** (Odoo > Amazon by 5-20 units): Normal. Orders fulfilled between import cycles.
- **Reserved differences**: Normal. Amazon reserves stock for orders being prepared.
- **Import jobs completing in 1-3 batches**: Normal for ~50 orders/day.
- **Status sync running every 15 minutes**: Normal and expected.

### What Requires Escalation

| Symptom | Severity | Action |
|---|---|---|
| `manual_review` event | Medium | Investigate the specific order. See Troubleshooting. |
| Failed import job | High | Check error message. May be API issue. |
| Sellable discrepancy > 50 units | High | Run manual audit and compare. |
| Connection health failing | Critical | Check API credentials. Contact developer. |
| Cron not running | Critical | Check Settings > Technical > Scheduled Actions. |

---

## 13. Troubleshooting Guide

### Orders Stopped Importing

**Symptoms:** No new orders appearing. Status sync jobs show no recent activity.

**Where to look:**
- Settings > Technical > Scheduled Actions > "Amazon: Sync Order Statuses" (id=26) — is it active?
- Amazon > Orders > Status Sync Jobs — is there a stuck or failed job?
- Amazon > Alerts — any connection errors?

**Safe actions:**
- Check that cron 26 (Sync Order Statuses) is active.
- Check the last status sync job's state and error message.
- Click "Import Orders" on the instance form to manually trigger an import.
- If cron 24 (Import All Orders) is needed for additional coverage, it is safe to re-enable — consult the developer.

**When to call developer:** If the cron is active but jobs aren't being created, or if jobs fail with technical errors.

### Order Exists in Amazon but Not in Odoo

**Check:**
1. Is the order within the import window? Check `last_order_sync` on the instance.
2. Was the order imported but with a different reference? Search by Amazon order ID.
3. Is there a failed import job that might have skipped it?

**Safe action:** Click "Import Orders" on the instance form. The next import will include any missing orders from after `last_order_sync`.

### Order Imported but Stock Didn't Decrease

**Check:**
1. Amazon > FBA > FBA Sale Stock Events — find the event for this order item.
2. Is the event state "done"? → Stock should have decreased. Check the picking.
3. Is the event state "pending"? → Wait for the processor cron (runs every minute).
4. Is the event state "manual_review"? → See below.
5. Is the event state missing? → The order item may not be FBA, or the product may be unmapped.

**Check the event details:**
- `amazon_cumulative_fulfilled_qty` (C) — what Amazon reports as fulfilled
- `processed_fulfilled_qty` (P) — what Odoo has already processed
- `last_delta_qty` (D) — the last depletion amount

If C=0, Amazon hasn't fulfilled this item yet. No depletion expected.

### Stock Decreased Twice (Double Depletion)

This should **not** happen if Cutover V2 is working correctly.

**Verify:**
1. Check if there are duplicate events for the same order item (same `amazon_order_ref` + `amazon_order_item_id`).
2. Check the event's B (baseline) value for pre-cutover orders.

**When to call developer:** Immediately. This is a critical data integrity issue.

### Event Stuck in `pending`

**Check:**
1. Is the `Process FBA Sale Stock Events` cron active? (id=27)
2. Does the event have a `next_run_at` in the future?
3. Is the product mapped and stockable?

**Safe action:** Open the event. If it looks correct, click "Retry" to requeue it.

### Event in `manual_review`

**Check the error code:**

| Error Code | Meaning | Action |
|---|---|---|
| `CUTOVER_BASELINE_OUTSIDE_COVERAGE` | Old order outside the 1-year baseline window with B=0 | Verify the order date. If truly historical, the items were fulfilled before the baseline coverage period. Developer may need to add a manual baseline. |
| `CUTOVER_BASELINE_EXCEEDS_CUMULATIVE` | B > C — baseline says more were fulfilled than Amazon now reports | Data anomaly. Amazon may have adjusted the order. Investigate the order on Amazon Seller Central. |

**Safe action:** Do not retry without understanding the cause. Document the order reference and contact developer.

### Amazon/Odoo Stock Mismatch in Audit

**Normal causes:**
- Orders fulfilled between import and audit (Odoo > Amazon by a few units)
- Reserved distribution differences

**Abnormal causes (investigate):**
- Persistent mismatch growing over time
- Odoo shows significantly less stock than Amazon
- Specific SKU consistently mismatched

**Safe action:** Run a fresh audit (Amazon > FBA > Inventory Health > New). Compare with previous audits. If the gap is growing, escalate.

### Unmapped SKU in Audit

**Symptoms:** Audit shows "unmapped" status for a SKU.

**Action:**
1. Go to Amazon > Catalog > Products.
2. Search for the SKU.
3. If it exists but has no Odoo product link, use the Link function.
4. If it doesn't exist, use Import / Map Products to pull it from Amazon.

### Amazon API Quota Exceeded

**Symptoms:** Sync logs show HTTP 429 (Too Many Requests) or throttling messages.

**Action:** This is temporary. The connector has built-in retry logic. Wait 15-30 minutes and check if operations resume. If throttling persists for hours, reduce sync frequency or contact developer.

### Cron Not Running

**Check:** Settings > Technical > Scheduled Actions. Search for "Amazon". Verify the cron's Active checkbox and Last Execution date.

**If a cron is inactive that should be active:** See the active cron list in Section 9. Only enable crons from the "Active" list.

---

## 14. Accounting & Settlement Handoff

### Current State

**Order and FBA stock integration is LIVE.** Accounting and settlement configuration is **NOT YET COMPLETE.**

No settlement reports have been imported. No accounting entries have been created. No payout reconciliation has been performed.

### What Needs to Be Configured

| # | Configuration | Current State | Required Decision | Who Decides | Blocking? |
|---|---|---|---|---|---|
| 1 | Settlement Journal | **Not set** | Create or select a journal for Amazon settlement entries | Accountant + Implementer | Yes — blocks settlement accounting |
| 2 | Amazon Payout Bank Journal | **Not set** | Select the bank journal where Amazon deposits arrive | Accountant | Yes — blocks payout reconciliation |
| 3 | Amazon Clearing Account | **Not set** | Create or select a clearing/suspense account for the Amazon payout cycle | Accountant | Yes — blocks settlement accounting |
| 4 | Settlement Cutoff Date | **Not set** | Choose the date from which settlements should create accounting entries | Accountant + Client | Yes — blocks settlement import |
| 5 | Settlement Accounting Strategy | `settlement_based` | Confirm this is correct (vs. invoice-aware) | Accountant | Confirm only |
| 6 | Amazon Sales Account | **Not set** | Revenue account for Amazon sales | Accountant | Yes |
| 7 | Amazon Fee Account | **Not set** | Expense account for Amazon referral/commission fees | Accountant | Yes |
| 8 | Amazon FBA Fee Account | **Not set** | Expense account for FBA fulfillment fees | Accountant | Yes |
| 9 | Amazon Refund Account | **Not set** | Account for customer refunds | Accountant | Yes |
| 10 | Amazon Reimbursement Account | **Not set** | Account for Amazon reimbursements (lost/damaged stock) | Accountant | Yes |
| 11 | Amazon Shipping Account | **Not set** | Account for shipping credits/charges | Accountant | Yes |
| 12 | Amazon Promotion Account | **Not set** | Account for promotional discounts | Accountant | Yes |
| 13 | Amazon Tax Account | **Not set** | Account for tax amounts | Accountant | Yes |
| 14 | Amazon Adjustment Account | **Not set** | Account for miscellaneous adjustments | Accountant | Yes |
| 15 | Amazon Other Credit Account | **Not set** | Catch-all for uncategorized credits | Accountant | Yes |
| 16 | Amazon Other Debit Account | **Not set** | Catch-all for uncategorized debits | Accountant | Yes |
| 17 | Amazon Suspense Account | **Not set** | Temporary holding account for unclassifiable items | Accountant | Yes |
| 18 | Egyptian Tax Configuration | **Not configured** | Tax-inclusive behavior, tax mapping, VAT treatment | Accountant + Tax advisor | Yes |

### What Each Account Is For

**Settlement Journal:** The accounting journal where Amazon settlement entries are posted (e.g., "Amazon Egypt Settlements").

**Amazon Clearing Account:** A balance-sheet account that represents "money Amazon owes us." Settlement entries credit this account for revenue and debit it for fees. When Amazon's bank transfer arrives, it clears this account.

**Amazon Sales Account:** Revenue account (income statement) for the product sale amount.

**Amazon Fee Account:** Expense account for Amazon's referral fee (percentage of sale price).

**Amazon FBA Fee Account:** Expense account for Amazon's fulfillment fee (picking, packing, shipping).

**Amazon Refund Account:** Contra-revenue or expense account for customer refunds Amazon processes.

**Amazon Reimbursement Account:** Income account for reimbursements Amazon pays when they lose or damage seller inventory.

**Amazon Shipping Account:** Account for shipping charges and credits in settlements.

**Amazon Promotion Account:** Account for promotional discounts Amazon applies.

**Amazon Tax Account:** Account for tax amounts collected and remitted.

**Amazon Payout Bank Journal:** The bank journal in Odoo where Amazon's actual bank transfer arrives. Used to reconcile the clearing account.

### Accounting Flow

```
Amazon Settlement Report
        ↓
Import into Odoo (read-only data)
        ↓
Review settlement lines
(revenue, fees, refunds, reimbursements, tax, adjustments)
        ↓
Create Accounting Entry (DRAFT only)
        ↓
Accountant reviews draft journal entry
        ↓
Accountant posts entry (standard Odoo action)
        ↓
Clearing account now has a balance
        ↓
Amazon bank transfer arrives
        ↓
Register payout / match bank transaction
        ↓
Reconcile clearing account
```

**Important:** The "Create Accounting Entry" action creates a **draft** journal entry. It does NOT automatically post. The accountant must review and post manually using standard Odoo accounting workflow.

---

## 15. First Settlement Test Procedure

**This is a procedure for when the accountant is ready. DO NOT execute it now.**

### Prerequisites

All items in Section 14 must be configured first.

### Step-by-Step

1. **Configure all account mappings** on the Amazon instance form (Section 14).
2. **Set the settlement cutoff date** — typically the go-live date (2026-09-15) or the date agreed with the accountant.
3. **Import ONE settlement manually:**
   - Go to Amazon > Configuration > Instances > Amazon Egypt Production.
   - Click "Import Settlements" button.
   - This reads settlement data from Amazon — it does NOT create accounting entries.
4. **Review the imported settlement:**
   - Go to Amazon > Accounting > Settlement Reports.
   - Open the imported settlement.
   - Review the settlement lines (Amazon > Accounting > Settlement Financial Lines).
   - Verify: revenue amounts, fee categories, refunds, tax, total matches Amazon's report.
5. **Create draft accounting entry:**
   - On the settlement report, click "Create Accounting Entry" (if available).
   - This creates a DRAFT journal entry — it is NOT posted yet.
6. **Accountant review:**
   - Open the draft journal entry.
   - Verify each line: revenue, referral fees, FBA fees, refunds, tax, clearing balance.
   - Compare the total against the Amazon settlement report's "Total" amount.
7. **If correct:** Accountant posts the entry using standard Odoo "Post" button.
8. **If incorrect:** Fix the account mappings, delete the draft entry, and repeat from step 5.
9. **Payout reconciliation:**
   - When Amazon's bank transfer arrives, register it in the bank journal.
   - Reconcile the bank transaction against the Amazon clearing account balance.
10. **Repeat** with 2-3 more settlements before considering automation.
11. **Only after written approval:** Consider enabling the `Import Settlement Reports` cron (id=30) and setting `settlement_sync_interval` to `daily`.

### What NOT to Do

- Do NOT enable the settlement import cron before all accounts are mapped.
- Do NOT post journal entries without accountant review.
- Do NOT assume the first import will have correct mappings — test and adjust.

---

## 16. Returns / Removals / Reimbursements

These features import evidence from Amazon. None are currently active.

### Customer Returns

| Aspect | Details |
|---|---|
| **What it imports** | Records of products returned by customers to Amazon's FBA warehouse |
| **Does it change Odoo stock?** | **No** — returns are informational evidence only. Returned stock goes to Amazon's FBA warehouse, not to the seller's Odoo warehouse. |
| **Does it change accounting?** | No — refund accounting comes from settlements, not returns |
| **Current cron** | `Import FBA Customer Returns` (id=53) — **disabled** |
| **Data imported so far** | 0 return reports |
| **Business decision needed** | Client must understand that returns ≠ automatic stock credit |
| **Enable when** | After client approval, set cron active |

### Inventory Adjustments

| Aspect | Details |
|---|---|
| **What it imports** | Records of inventory lost, damaged, found, or adjusted at Amazon's warehouse |
| **Does it change Odoo stock?** | Depends on policy. Currently set to `informational` — imports data only, does not move stock |
| **Does it change accounting?** | No |
| **Current cron** | `Import FBA Inventory Adjustments` (id=54) — **disabled** |
| **Data imported so far** | 0 adjustments |
| **Business decision needed** | Confirm `informational` policy is correct, or decide if adjustments should create stock moves |
| **Enable when** | After client confirms policy |

### Reimbursements

| Aspect | Details |
|---|---|
| **What it imports** | Records of Amazon reimbursing the seller for lost/damaged FBA inventory |
| **Does it change Odoo stock?** | No |
| **Does it change accounting?** | Not directly — reimbursement amounts appear in settlements |
| **Current crons** | `Import FBA Reimbursements` (id=55) — **disabled**; `Match FBA Reimbursements` (id=56) — **active** (links records) |
| **Data imported so far** | 0 reimbursements |
| **Business decision needed** | Client approval to start importing |
| **Enable when** | After client approval |

### Removal Orders

| Aspect | Details |
|---|---|
| **What it does** | Allows requesting Amazon to ship FBA stock back or dispose of it. Tracks removal order status and shipment evidence. |
| **Does it change Odoo stock?** | Yes, when the removal shipment is processed — moves stock from FBA locations to Removal Transit or Disposal |
| **Does it change accounting?** | Not directly |
| **Current cron** | `Refresh Removal Status` (id=37) — **disabled** |
| **Data imported so far** | 0 removal orders |
| **Business decision needed** | Client must have removal orders to track |
| **Enable when** | When the client starts using FBA removals |

---

## 17. Inbound FBA Shipments

### Overview

When the seller needs to send new stock to Amazon's FBA warehouse, the connector manages the entire inbound workflow.

### Inbound Flow

```
1. Create Inbound Plan (Amazon > FBA > Inbound Shipments)
   → Tells Amazon what products and quantities you want to send
   
2. Generate Packing Options
   → Amazon suggests how to pack the items
   
3. Confirm Packing + Set Packing Information
   → Specify box dimensions and contents
   
4. Generate Placement Options
   → Amazon determines which fulfillment center(s) to ship to
   
5. Confirm Placement
   → Accept the placement (may involve multiple destinations)
   
6. Generate Transportation
   → Choose shipping method
   
7. Confirm Transportation
   → Finalize shipping arrangements
   
8. Print Labels (product labels + box labels)
   → Apply to physical boxes
   
9. Submit Tracking (for self-ship)
   → Enter carrier tracking numbers
   
10. Create Dispatch Picking
    → Creates an Odoo stock picking: WH/Stock → FBA Transit
    
11. Validate Dispatch
    → Physical goods leave the warehouse. Stock moves to FBA Transit.
    
12. Receiving Sync (automatic, every 30 minutes)
    → Amazon confirms receipt. Stock moves from Transit to Received/Staging.
    
13. Inventory Disposition (reviewed)
    → Stock moves from Received to Sellable/Reserved/Unsellable
```

### Currently Active Inbound Crons

- **Poll Inbound Operations** (id=35) — 1 min — processes inbound plan jobs
- **Synchronize Inbound Receiving** (id=36) — 30 min — syncs receiving evidence

### Important Notes

- Steps 1-9 require **manual action** by the operations team. The connector provides the interface but does not automate business decisions.
- Step 10 (dispatch picking) changes Odoo stock. Only validate when goods physically leave.
- Step 12 (receiving) is automatic — the cron checks Amazon for receipt confirmation.
- The Ship-From Partner (id=1) and Removal Return Partner (id=1) are configured.

---

## 18. Product / Price Policy Decisions

These decisions must be made by the client and documented before enabling related automation.

| # | Decision | Options | Impact | Current State |
|---|---|---|---|---|
| 1 | Who is the product master? | **Odoo** (manage listings locally, push to Amazon) or **Amazon** (manage on Seller Central, pull to Odoo) | Determines sync direction | Not decided |
| 2 | Should new Amazon SKUs auto-create Odoo products? | **Yes** (Sync Products cron creates them) or **No** (manual linking only) | Affects unmapped SKU handling | Currently manual |
| 3 | Should product sync run automatically? | **Yes** (enable cron id=28) or **No** (manual only) | Determines if new listings auto-appear in Odoo | Currently manual |
| 4 | Should prices be pushed from Odoo to Amazon? | **Yes** (enable price push) or **No** (manage prices on Amazon only) | Major — wrong prices could affect revenue | Currently disabled |
| 5 | What happens to unmapped SKUs? | **Alert** + **manual link** or **auto-create** | Affects order import completeness | Currently: unmapped SKUs noted in audits |
| 6 | Should Odoo ever push stock levels to Amazon? | Almost certainly **No** for FBA | Incorrect stock push could cause overselling | Disabled and protected |

**Action for Implementer:** Review these decisions with the client. Document the answers. Only then consider enabling the related automation.

---

## 19. Responsibility Matrix

| Area | Developer | Implementer | Accountant | Client Operations |
|---|---|---|---|---|
| API credentials & connection | Configures | Monitors health | — | — |
| Product SKU mapping | — | Links products, resolves unmapped | — | Provides SKU policy |
| Chart of Accounts | — | Assists setup | **Decides and creates** | — |
| Settlement account mappings | — | Configures in Odoo | **Decides accounts** | — |
| Egyptian tax configuration | — | Configures in Odoo | **Decides tax treatment** | — |
| Settlement validation | — | Imports data | **Reviews and posts** | — |
| Payout reconciliation | — | Assists | **Reconciles** | — |
| Cutover V2 | **Maintains — do not touch** | — | — | — |
| Cron management (safety) | **Approves changes** | Monitors | — | — |
| Daily monitoring | — | **Primary** | — | Reviews reports |
| Inbound shipments | — | Assists first time | — | **Plans and executes** |
| Price policy | — | Configures if approved | — | **Decides** |
| Return/removal/adjustment policy | — | Enables after approval | — | **Decides** |
| Bug fixes & code changes | **Handles** | Reports issues | — | Reports issues |

---

## 20. Do-Not-Touch List

These items are critical to the integrity of the live system. Do not modify them without developer approval.

| # | Item | Location | Consequence of Changing |
|---|---|---|---|
| 1 | **Cutover Run 3** | `amazon.fba.sale.stock.cutover.run` id=3 | Corrupts double-depletion prevention for all historical orders |
| 2 | **Cutover timestamp** | `fba_sale_stock_cutover_at` on instance 6: `2026-09-15 21:02:11` | Changes which orders are treated as historical vs. live |
| 3 | **History start date** | `history_start_at` on cutover run 3: `2025-09-15 21:02:11` | Changes baseline coverage window |
| 4 | **15,269 baseline records** | `amazon.fba.sale.stock.cutover.baseline` | Corrupts per-order depletion calculations |
| 5 | **FBA location IDs** (29-37) | `stock.location` | Breaks all stock movement logic |
| 6 | **`auto_sync_enabled`** | Instance 6, must stay **False** | Enables Master Scheduler which pushes stock and prices |
| 7 | **Master Scheduler** cron (id=43) | Must stay **inactive** | Dispatches dangerous write operations |
| 8 | **Full Sync** cron (id=42) | Must stay **inactive** | Runs all operations including writes |
| 9 | **Export Stock** cron (id=33) | Must stay **inactive** | Pushes stock to Amazon |
| 10 | **Update Prices** cron (id=29) | Must stay **inactive** | Pushes prices to Amazon |
| 11 | **`stock_push_interval`** | Instance 6, must stay **disabled** | Defense-in-depth against stock push |
| 12 | **`price_push_interval`** | Instance 6, must stay **disabled** | Defense-in-depth against price push |
| 13 | **Opening stock moves** | stock.picking/move from 2026-09-15 | Historical record of opening inventory |
| 14 | **API credentials** | refresh_token, client_id, client_secret | Breaks all Amazon connectivity |

---

## 21. Implementer First-Day Checklist

### Day 1 — Orientation

- [ ] Read Section 1 (Executive Overview) — understand what the integration does
- [ ] Log into Odoo and navigate the Amazon menu (Section 2)
- [ ] Open the Amazon instance form (Amazon > Configuration > Instances)
- [ ] Review the 19 product mappings (Amazon > Catalog > Products)
- [ ] Browse the FBA locations (Inventory > Configuration > Locations, filter "Amazon")
- [ ] Check the live order pipeline: open Amazon > Orders > Import Jobs — confirm recent jobs completed
- [ ] Check FBA Sale Stock Events (Amazon > FBA > FBA Sale Stock Events) — confirm all "done"
- [ ] Read Section 7 (Cutover V2) — understand why it exists and what it protects
- [ ] Read Section 9 (Current Live Automation) — know which crons are active
- [ ] Read Section 10 (Critical Safety Rules) — know what NOT to do
- [ ] Read Section 20 (Do-Not-Touch List)
- [ ] **DO NOT activate any additional automation on Day 1**

### Day 2 — Accounting Preparation

- [ ] Meet with the accountant
- [ ] Review Section 14 (Accounting & Settlement Handoff)
- [ ] Walk through the 18 configuration items together
- [ ] Agree on the Chart of Accounts for Amazon
- [ ] Agree on the settlement cutoff date
- [ ] Confirm the settlement accounting strategy
- [ ] Discuss Egyptian tax treatment
- [ ] **DO NOT import settlements yet** — just prepare the account structure

### Day 3+ — Settlement Test

- [ ] Configure all accounts on the Amazon instance form
- [ ] Follow the procedure in Section 15 (First Settlement Test)
- [ ] Import one settlement
- [ ] Create a draft accounting entry
- [ ] Have the accountant review
- [ ] Iterate until correct
- [ ] Document the final account mappings

### Week 2+ — Feature Enablement

- [ ] After settlement accounting is validated, consider enabling settlement import cron
- [ ] Discuss with client: returns, adjustments, reimbursements (Section 16)
- [ ] Discuss with client: product/price policy (Section 18)
- [ ] Enable features one at a time with monitoring

---

## 22. Go-Live Status Matrix

| Feature | Status | Owner | Next Action |
|---|---|---|---|
| Amazon API Connection | **LIVE** | Developer | Monitor |
| Product SKU Mapping | **COMPLETE** (19/19 mapped) | Implementer | Monitor for new SKUs |
| FBA Opening Stock | **COMPLETE** (FBAAUDIT/00007) | Developer | Historical record |
| Cutover V2 | **ACTIVATED** (15,269 baselines) | Developer | Do not touch |
| Order Import (cron 24) | **INACTIVE** — was used at go-live, now off | Developer | Re-enable if needed (safe, read-only) |
| Order Status Sync (cron 26) | **LIVE** (15-min cron) — primary order discovery | Automated | Monitor daily |
| FBA Sale Stock Depletion | **LIVE** (1-min cron) | Automated | Monitor for manual_review |
| FBA Inventory Audit | **LIVE** (daily + 5-min processor) | Automated | Review weekly |
| Inbound Shipments | **LIVE** (crons active) | Operations | Use when shipping to Amazon |
| Connection Health | **LIVE** (15-min monitoring) | Automated | Check alerts |
| Operational Monitoring | **LIVE** (dashboard, alerts, stuck jobs) | Automated | Check daily |
| Stock Push to Amazon | **DISABLED** — not required for FBA | Developer | Do not enable |
| Price Push to Amazon | **DISABLED** — no policy approved | Client + Developer | Requires client decision |
| Product Sync Automation | **DISABLED** | Implementer | Requires policy decision |
| Settlement Import | **WAITING** — accountant must configure | Accountant + Implementer | Configure accounts first |
| Settlement Accounting | **WAITING** — journals/accounts not set | Accountant | Full configuration needed |
| Payout Reconciliation | **WAITING** — bank journal not set | Accountant | After settlement setup |
| Customer Returns Import | **DECISION REQUIRED** | Client | Approve to enable |
| Inventory Adjustments Import | **DECISION REQUIRED** | Client | Approve to enable |
| Reimbursements Import | **DECISION REQUIRED** | Client | Approve to enable |
| Removal Order Tracking | **DECISION REQUIRED** | Client | Approve if using removals |
| Egyptian Tax Configuration | **WAITING** | Accountant + Tax advisor | Critical for correct entries |
| AI Features | **NOT CONFIGURED** | Optional | Requires API key |

---

## 23. Final Handoff Summary

### A. What Is Already LIVE

The FBA order and inventory pipeline is fully operational:

- Amazon orders are discovered and updated via the Order Status Sync every 15 minutes.
- FBA fulfillments automatically deplete Sellable inventory in Odoo.
- Cutover V2 prevents double-depletion for historical orders.
- Daily inventory audits compare Odoo against Amazon.
- Inbound shipment workflow is active for sending new stock to Amazon.
- Connection health, alerts, and operational monitoring are active.
- All 19 products are mapped. All data is reconciled.

### B. What the Implementer Must Complete

1. **Settlement Accounting Configuration** — work with the accountant to:
   - Select or create the settlement journal
   - Map all 15+ Amazon fee/revenue/refund accounts
   - Set the settlement cutoff date
   - Configure Egyptian tax treatment
   - Set up the payout bank journal
   - Test with one real settlement before enabling automation

2. **Business Policy Decisions** — work with the client to decide:
   - Product sync policy (automatic or manual)
   - Price management policy (Odoo master or Amazon master)
   - Whether to enable returns, adjustments, reimbursements, and removal tracking

3. **Ongoing Monitoring** — establish daily and weekly check routines per Section 12.

### C. What Must NOT Be Changed

| Item | Why |
|---|---|
| Cutover V2 Run 3, baselines, cutover timestamp | Prevents double-depletion of historical orders |
| `auto_sync_enabled` (must stay False) | Prevents Master Scheduler from dispatching stock/price push |
| Master Scheduler, Full Sync, Export Stock, Update Prices crons (must stay inactive) | Prevents writing to Amazon |
| `stock_push_interval`, `price_push_interval` (must stay disabled) | Defense-in-depth protection |
| FBA location structure (ids 29-37) | Foundation of all inventory tracking |
| Opening stock reference (FBAAUDIT/00007) | Historical audit trail |
| API credentials | Breaks all connectivity if changed incorrectly |

---

## IMPLEMENTER START HERE

If you are reading this document for the first time, follow these rules:

1. **Do not reconfigure the technical integration.** API credentials, marketplace settings, FBA locations, and instance connection fields are already configured and working. Changing them will break the live pipeline.

2. **Do not rebuild opening inventory.** The opening FBA stock was loaded once on 2026-09-15 and is a permanent historical baseline. Never re-seed, re-import, or "refresh" it.

3. **Do not modify Cutover V2.** Run 3 with 15,269 baselines is activated and protecting all historical orders from double-depletion. Do not rebuild, delete, or modify baselines or timestamps.

4. **Do not enable Master Auto-Sync (cron 43), Full Sync (cron 42), Export Stock (cron 33), or Update Prices (cron 29).** These write to Amazon and are intentionally disabled. The `auto_sync_enabled` flag must remain False. The `stock_push_interval` and `price_push_interval` must remain `disabled`.

5. **Spend Day 1 understanding the live order/FBA workflow.** Follow the First-Day Checklist in Section 21. Read the module tour, navigate the menus, check that orders are flowing, events are processing, and audits are completing.

6. **The first remaining implementation work is Accounting & Settlement configuration.** Work with the accountant to configure the settlement journal, clearing account, fee/revenue accounts, tax treatment, and cutoff date. See Section 14.

7. **Follow the controlled first-settlement procedure** (Section 15) before enabling settlement automation. Import one settlement, create a draft entry, have the accountant review, iterate until correct, then consider automation.

8. **When in doubt, ask the developer.** Any change to crons, intervals, sync flags, or cutover state should be discussed with the developer first.

---

## HANDOFF ACCEPTANCE CHECKLIST

Complete this checklist after your first week of onboarding.

### Understanding (Day 1)

- [ ] I have read Sections 1-11 of this document
- [ ] I can navigate the Amazon menu in Odoo and find all key screens
- [ ] I understand which crons are active and what they do
- [ ] I understand the Do-Not-Touch list and why each item is protected
- [ ] I know the difference between depletion reconciliation and audit comparison
- [ ] I have verified that orders are importing and events are processing

### Monitoring (Day 2-3)

- [ ] I have performed a daily check per Section 12
- [ ] I have reviewed the latest inventory audit
- [ ] I know where to find sync logs and operational alerts
- [ ] I know when to escalate vs. when to wait

### Accounting Preparation (Day 3-5)

- [ ] I have reviewed Section 14 with the accountant
- [ ] The accountant has confirmed the chart of accounts for Amazon
- [ ] We have agreed on the settlement cutoff date
- [ ] We have agreed on the settlement accounting strategy

### First Settlement Test (Week 2+)

- [ ] All account mappings are configured on the instance form
- [ ] One settlement has been imported successfully
- [ ] A draft accounting entry has been created and reviewed
- [ ] The accountant has approved the entry structure
- [ ] The payout bank journal is configured

### Sign-Off

| Role | Name | Date | Signature |
|---|---|---|---|
| Implementer | | | |
| Accountant | | | |
| Client Operations | | | |
| Developer | | | |

---

*This document was prepared on 2026-09-16 and updated with a QA pass against the production database `amazon_prod12sep` and the `sdlc_amazon_connector` source code. Production snapshot verified at 2026-09-16 17:15 UTC. No production data or configuration was modified during preparation.*
