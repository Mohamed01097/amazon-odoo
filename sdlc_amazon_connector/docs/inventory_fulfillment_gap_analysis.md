# Inventory and Fulfillment Reverse-Engineering Gap Analysis

Generated: 2026-07-24  
Module: `sdlc_amazon_connector`  
Database inspected read-only: `amazon`  
Module path: `/home/odoo16/odoo/v19/amazon-odoo/sdlc_amazon_connector`

## A. Executive Summary

Inventory/Fulfillment is not production-ready.

Already working from executable code:

- Product mapping by SKU.
- Product/listing sync stores Amazon quantity on `amazon.product.amazon_qty`.
- MFN stock push exists via Amazon `JSON_LISTINGS_FEED`.
- FBA inventory report download exists.
- Amazon orders create draft Odoo Sale Orders.
- Sale Orders can be assigned FBA/FBM warehouse if configured.
- FBM shipment confirmation exists through an Amazon fulfillment feed.

Partially working or unsafe:

- Amazon → Odoo FBA stock pull exists, but writes `stock.quant` directly and synchronously.
- Shipment confirmation can be sent to Amazon without feed-result confirmation and without strong idempotency.
- Partial shipment support is effectively broken.
- FBA inbound and MCF outbound models exist, but are incomplete.

Advertised but not fully implemented:

- Bidirectional stock sync as a production feature.
- FBA warehouse/location model.
- Inventory background jobs.
- Inventory pagination persistence.
- Safe stock adjustment flow.
- Full fulfillment/tracking synchronization.
- Partial/multi-package shipment support.

Recommendation: Inventory/Fulfillment should be rebuilt as a dedicated production subsystem, reusing the working API client, SKU mapping, sync-log, and background-job patterns.

## B. Scope Confirmation

Inspected only:

- `/home/odoo16/odoo/v19/amazon-odoo/sdlc_amazon_connector`
- read-only records from database `amazon`
- standard Odoo model names only to understand connector interactions

No code was modified during the investigation. No module upgrade was run. No live Amazon API call was made.

Odoo Enterprise source was not inspected.

No external paid connector, Odoo Enterprise Amazon connector, GitHub connector, or unrelated custom module was used as a reference.

## C. Module Path and Version

Module path:

```text
/home/odoo16/odoo/v19/amazon-odoo/sdlc_amazon_connector
```

Manifest version:

```text
19.0.3.2.0
```

Database installed version:

```text
19.0.3.2.0
```

Declared dependencies:

| Dependency | Classification |
|---|---|
| `sale_management` | Standard Odoo addon |
| `stock` | Standard Odoo addon |
| `contacts` | Standard Odoo addon |
| `account` | Standard Odoo addon |
| `mail` | Standard Odoo addon |
| `purchase` | Standard Odoo addon |
| `delivery` | Standard Odoo addon |

No Enterprise-only dependency or Enterprise Amazon connector dependency was found.

Read-only DB evidence:

- Instances: `ProductionApp`, `app1`
- FBA warehouse configured: no
- FBM warehouse configured: no
- Amazon products: `16`
- Mapped products: `16`
- AFN mapped products: `16`
- MFN mapped products: `0`
- Amazon orders: `1232`
- AFN orders: `1232`
- MFN orders: `0`
- Linked Sale Orders: `1232`
- Connector-tagged pickings: `0`
- FBA inventory reports: `0`
- Inbound shipments: `0`
- Outbound/MCF orders: `0`

## D. Architecture Map

Relevant user buttons:

| UI | Button | Method |
|---|---|---|
| Amazon Instance | Export Stock | `amazon.instance.action_export_stock()` |
| Amazon Instance | Pull Stock | `amazon.instance.action_pull_stock()` |
| Amazon Instance | Full Sync | `amazon.instance.action_full_sync()` |
| Amazon FBA Inventory Report | Download Report | `amazon.fba.inventory.report.action_download_report()` |
| Amazon FBA Inventory Report | Process Report | `amazon.fba.inventory.report.action_process_report()` |
| Amazon Order / Delivery | Confirm Shipment | `amazon.sale.order.action_confirm_shipment()` |
| Stock Picking | Export Tracking | `stock.picking.action_export_tracking_to_amazon()` |
| Inbound Shipment | Create/Submit/Check/Get Labels | `amazon.inbound.shipment.*` |
| MCF Outbound | Submit/Check/Cancel | `amazon.outbound.order.*` |

Relevant cron jobs:

| Cron | Active in XML | Method | Inventory/Fulfillment role |
|---|---:|---|---|
| Amazon: Export Stock Levels | false | `amazon.instance.cron_export_stock()` | Synchronous stock push |
| Amazon: Pull Stock Levels | false | `amazon.instance.cron_pull_stock()` | Logs warning only; does not pull |
| Amazon: Master Auto-Sync Scheduler | false | `amazon.instance.cron_master_scheduler()` | Can call stock push; explicitly skips stock pull |
| Amazon: Update FBM Order Status | false | `amazon.instance.cron_update_fbm_order_status()` | Status sync, not shipment |
| Amazon: Process Order Import Jobs | true | `amazon.order.import.job.cron_process_order_import_jobs()` | Order import only |
| Amazon: Sync Order Statuses | true | `amazon.order.status.sync.job.cron_sync_order_statuses()` | Status sync only |

Relevant job models:

- Existing for orders: `amazon.order.import.job`
- Existing for order statuses: `amazon.order.status.sync.job`
- Missing for inventory: no inventory pull/push job model
- Missing for fulfillment: no shipment confirmation job model

Relevant API methods:

- `AmazonAPI.fetch_fba_inventory_report()`
- `AmazonAPI.fetch_fba_inventory_adjustment_report()`
- `AmazonAPI.fetch_fba_shipment_report()`
- `AmazonAPI.build_inventory_json_feed()`
- `AmazonAPI.build_order_fulfillment_feed_xml()`
- `AmazonAPI.get_inventory_summaries()` exists but has no caller
- `AmazonAPI.create_inbound_plan()`
- `AmazonAPI.get_inbound_plan()`
- `AmazonAPI.get_shipment_items()`
- `AmazonAPI.create_fulfillment_order()`
- `AmazonAPI.get_fulfillment_order()`
- `AmazonAPI.cancel_fulfillment_order()`

Odoo stock models touched:

- `stock.quant`: directly created in inventory pull/report adjustment
- `stock.warehouse`: selected from instance or first warehouse
- `stock.picking`: inherited for Amazon tracking export
- `sale.order`: inherited for Amazon links and picking tagging
- `product.product`: linked via Amazon SKU/default code

## E. Existing Features

Relevant file map:

| File | Purpose | Inventory/Fulfillment participation |
|---|---|---|
| `models/amazon_instance.py` | Main instance/config/API orchestration | Stock push, stock pull, shipment confirmation, FBA reports, inbound, MCF, crons |
| `models/amazon_api.py` | Amazon SP-API client | Reports, feeds, inventory summaries, inbound, outbound |
| `models/amazon_product.py` | Amazon product/SKU mapping | `amazon_qty`, fulfillment channel, MFN stock feed on listing update |
| `models/amazon_sale_order.py` | Amazon order model | SO creation, warehouse selection, delivery/tracking fields, shipment confirmation |
| `models/sale_order_inherit.py` | Odoo SO extension | Tags pickings after SO confirmation |
| `models/stock_picking_inherit.py` | Odoo picking extension | Auto/manual tracking export to Amazon |
| `models/amazon_fba_inventory.py` | FBA inventory report model | Downloads/processes reports; unsafe adjustment path |
| `models/amazon_inbound_shipment.py` | FBA inbound shipment model | Partial inbound plan/status support |
| `models/amazon_outbound_order.py` | MCF outbound model | Partial MCF create/status/cancel |
| `wizard/product_import_wizard.py` | Product CSV import/map wizard | Updates `amazon_qty` only; no Odoo stock/API push |
| `security/ir.model.access.csv` | Access rights | Broad access, no connector groups |
| `data/cron.xml` | Scheduled actions | Stock export/pull crons exist but disabled; pull cron skips |
| `views/instance_view.xml` | Instance UI | Stock buttons and warehouse fields |
| `views/fba_inventory_view.xml` | FBA report UI | Download/process buttons |
| `views/inbound_shipment_view.xml` | Inbound UI | Inbound shipment buttons |
| `views/outbound_order_view.xml` | MCF UI | MCF outbound buttons |
| `views/delivery_view.xml` | Amazon delivery UI | Delivery/tracking views |

Briefly identified unrelated files:

- AI pricing/listing/forecast/reviews/health/smart alerts use inventory numbers for analytics only.
- Settlements, VCS, returns, ratings are not core Inventory/Fulfillment.
- Dashboard/controller/static files display stock/order KPIs but do not perform inventory sync.

## F. Feature Matrix

| Feature | Status | Evidence |
|---|---|---|
| 1. FBA inventory pull | Implemented but Unsafe | Pulls FBA report and may create `stock.quant` directly. |
| 2. FBM inventory pull | Missing | No Amazon → Odoo FBM stock pull flow. |
| 3. Odoo stock push to Amazon | Partially Implemented | MFN JSON Listings feed exists; no job/feed-result polling/buffer. |
| 4. FBA warehouse mapping | Partially Implemented | Field exists and used in SO/stock pull, but no location model or defaults. |
| 5. FBM warehouse mapping | Partially Implemented | Field used for stock quantity context and SO warehouse. |
| 6. FBA location creation | Missing | No FBA location fields/creation. |
| 7. Inventory synchronization cron | Partially Implemented | Export cron exists disabled; pull cron only logs warning. |
| 8. Inventory background jobs | Missing | No inventory job model. |
| 9. Inventory pagination | Missing | Inventory summaries NextToken method unused; no persistent pagination. |
| 10. Inventory rate-limit retry | Partially Implemented | Generic API retries exist; no job-level deferral. |
| 11. Sale Order warehouse selection | Partially Implemented | Uses FBA/FBM warehouse if configured. |
| 12. FBA Sale Order handling | Partially Implemented | AFN imported as draft SO; no FBA fulfillment stock flow. |
| 13. FBM Sale Order handling | Partially Implemented | Draft SO + picking tagging after confirmation. |
| 14. Delivery Order creation | Partially Implemented | Only via Odoo SO confirmation; not managed by connector import. |
| 15. Picking linkage | Partially Implemented | Tags pickings after SO confirmation/validation. |
| 16. Shipment confirmation | Implemented but Unsafe | Sends feed; no feed processing confirmation; weak idempotency. |
| 17. Tracking number synchronization | Implemented but Unsafe | Odoo → Amazon only; auto on picking validation. |
| 18. Partial shipment support | Broken | Single feed item overwritten by loop; no move-line quantity mapping. |
| 19. Multiple packages support | Missing | No package model/payload support. |
| 20. Cancellation handling | Partially Implemented | Status sync safe locally; no Amazon cancel shipment/order flow. |
| 21. FBA inbound shipment support | Partially Implemented | Inbound plan/status partial; tracking/labels incomplete. |
| 22. Duplicate prevention | Partially Implemented | Some unique constraints; shipment/feed idempotency missing. |
| 23. Multi-company isolation | Partially Implemented | Company field exists; no record rules and weak warehouse scoping. |
| 24. Community compatibility | Fully Implemented | No Enterprise dependency found. |
| 25. Audit logs | Partially Implemented | API request logs exist; inventory/fulfillment success logs incomplete. |

## G. FBA Flow

Actual existing FBA behavior:

1. Products can be marked AFN/FBA through `amazon.product.fulfillment_channel`.
2. Product sync can set fulfillment channel from Merchant Listings report.
3. FBA stock pull uses `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` report.
4. Quantity imported is `afn-fulfillable-quantity` or fallback `quantity`.
5. The connector updates `amazon.product.amazon_qty`.
6. If the product is mapped and `fba_warehouse_id` is set, it creates a `stock.quant` with `inventory_quantity`.
7. Imported AFN orders create draft Sale Orders.
8. Sale Order warehouse is set to `fba_warehouse_id` if configured.
9. No delivery is created unless the Sale Order is later confirmed.
10. Status sync does not validate delivery or create invoice by default.
11. FBA inbound shipment model exists, but it is partial.
12. FBA order shipment confirmation is blocked in `action_confirm_shipment()`.

Classification: FBA flow is partial and unsafe for stock.

## H. FBM Flow

Actual existing FBM behavior:

1. Products can be marked MFN/FBM.
2. MFN stock push searches mapped `amazon.product` records with `fulfillment_channel = 'MFN'`.
3. Quantity source is Odoo `product.qty_available`, optionally in `fbm_warehouse_id` context.
4. Quantity is clamped to non-negative integer.
5. Stock feed is submitted through `JSON_LISTINGS_FEED`.
6. Imported MFN orders create draft Sale Orders.
7. Sale Order warehouse is set to `fbm_warehouse_id` if configured.
8. Pickings are created only after Sale Order confirmation.
9. Pickings are tagged as Amazon deliveries after confirmation.
10. If a done picking has tracking and is MFN, the module sends shipment confirmation to Amazon.
11. No robust partial shipment, package, feed-result polling, or duplicate-confirmation protection exists.

Classification: FBM flow is partial and shipment confirmation is unsafe.

## I. Amazon API Coverage

Implemented inventory/fulfillment API methods:

| Method | Endpoint / Operation | Used by |
|---|---|---|
| `fetch_fba_inventory_report()` | Reports API `GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA` | Stock pull, FBA report download |
| `fetch_fba_inventory_adjustment_report()` | Reports API `GET_FBA_INVENTORY_ADJUSTMENTS_DATA` | FBA adjustment report |
| `fetch_fba_shipment_report()` | Reports API `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL` | FBA shipment report |
| `build_inventory_json_feed()` | Builds `JSON_LISTINGS_FEED` inventory patch | Stock export |
| `submit_feed()` | Feeds API document/upload/create | Stock export, shipment confirmation |
| `build_order_fulfillment_feed_xml()` | Builds `POST_ORDER_FULFILLMENT_DATA` XML | Shipment confirmation |
| `get_inventory_summaries()` | `/fba/inventory/v1/summaries` | No callers found |
| `create_inbound_plan()` | `/inbound/fba/2024-03-20/inboundPlans` | Inbound plan |
| `get_inbound_plan()` | `/inbound/fba/2024-03-20/inboundPlans/{plan_id}` | Inbound status/import |
| `get_shipment_items()` | `/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}/items` | Inbound item status/import |
| `create_fulfillment_order()` | `/fba/outbound/2020-07-01/fulfillmentOrders` | MCF outbound submit |
| `get_fulfillment_order()` | `/fba/outbound/2020-07-01/fulfillmentOrders/{id}` | MCF status |
| `cancel_fulfillment_order()` | `/fba/outbound/2020-07-01/fulfillmentOrders/{id}/cancel` | MCF cancel |

Important gaps:

- `get_inventory_summaries()` is backend-only and unused.
- No inventory job uses `NextToken`.
- Report polling is synchronous and can wait up to 600 seconds.
- Feed submissions do not poll final feed processing result.
- Shipment XML is built by string concatenation without XML escaping.
- No Amazon tracking pull endpoint is implemented.

## J. Cron and Job Assessment

Inventory/fulfillment crons:

| XML ID | Name | Method | Active | Assessment |
|---|---|---|---:|---|
| `cron_amazon_export_stock` | Amazon: Export Stock Levels | `cron_export_stock()` | false | Calls synchronous stock export for every instance. |
| `cron_amazon_pull_stock` | Amazon: Pull Stock Levels | `cron_pull_stock()` | false | Does not pull; only logs warning. |
| `cron_amazon_master_scheduler` | Amazon: Master Auto-Sync Scheduler | `cron_master_scheduler()` | false | Can run stock push; explicitly skips stock pull. |
| `cron_amazon_update_fbm_status` | Amazon: Update FBM Order Status | `cron_update_fbm_order_status()` | false | Status sync only. |

Batching/retries/persistence:

- No inventory batch size field.
- No inventory job model.
- No inventory cursor/NextToken persistence.
- No inventory locking.
- No feed-result polling.
- No shipment confirmation queue.
- Generic API retry handles `429`/5xx, but long waits happen inside the same request/cron transaction.
- FBA report polling can block for up to 600 seconds.

Timeout risk: high for FBA reports and synchronous feed operations.

## K. Community Compatibility

The connector itself is valid for Odoo 19 Community from a dependency perspective:

- No Enterprise-only module dependency found.
- No `sale_amazon` / Odoo Enterprise Amazon connector reference found.
- No Enterprise XML ID inheritance found.
- The only inherited XML ID relevant here is `sale.view_order_form`, a standard Sales view.

Non-blocking Odoo 19 issues:

- Some models still use deprecated `_sql_constraints` instead of `models.Constraint`, e.g. `amazon.product` and `amazon.inbound.shipment`.

## L. Bugs and Risks

### Critical

| File/method | Issue | Impact |
|---|---|---|
| `amazon.instance.action_pull_stock()` | Creates `stock.quant` directly with `sudo()` and `inventory_mode`. | Can corrupt/duplicate stock and bypass stock move traceability. |
| `amazon.fba.inventory.report._process_adjustment()` | Uses first warehouse found and direct `stock.quant` create. | Can adjust wrong warehouse/company stock. |
| `stock.picking.button_validate()` | Sends Amazon shipment confirmation inside the Odoo validation transaction. | Amazon may be updated even if Odoo transaction later fails. |
| `amazon.instance._confirm_order_shipment()` | Marks Amazon order `Shipped` immediately after feed creation, not after feed processing success. | Local status can become wrong if Amazon later rejects feed. |
| `amazon.instance._confirm_order_shipment()` | Multi-line loop overwrites `order_item_id`/`quantity` on one item dict. | Partial/multi-line shipments can be confirmed incorrectly. |

### High

| File/method | Issue | Impact |
|---|---|---|
| `amazon_api.wait_for_report()` | Synchronous polling up to 600 seconds. | Browser/cron timeout risk. |
| `amazon.instance.action_export_stock()` | Uses `qty_available` only; no reserved/outgoing/buffer logic. | Can oversell or push wrong availability. |
| `amazon_api.build_order_fulfillment_feed_xml()` | XML string concatenation without escaping. | Carrier/tracking/order values can break XML. |
| `security/ir.model.access.csv` | All connector models have broad read/write/create/unlink with no group. | Unauthorized users can trigger stock/shipment actions. |
| `amazon.instance` warehouse fields | No company domain/constraint. | Wrong company warehouse can be used. |
| `amazon.instance._submit_outbound_order()` | Sets `fulfillment_order_id = outbound.name`, ignoring Amazon response ID. | MCF tracking/status may query wrong identifier. |
| `amazon.instance._create_inbound_shipment_plan()` | Source country hardcoded to `IN`. | Wrong inbound plan data for Egypt/other marketplaces. |
| `amazon.instance._update_inbound_shipment_tracking()` | Only sets local state/date; no Amazon API call. | Tracking is not actually sent to Amazon inbound. |

### Medium

| File/method | Issue | Impact |
|---|---|---|
| `amazon.instance.action_pull_stock()` | Only pulls FBA report; no FBM pull. | Feature label “Pull Stock” is broader than implementation. |
| `amazon.fba.inventory.report._process_live_stock()` | Updates only `amazon_qty`. | No real Odoo stock reconciliation unless adjustment path is used. |
| `amazon.fba.inventory.report._process_fba_shipment()` | Only sets legacy `order_status = Shipped`. | Does not update raw status, Sale Order status, pickings, or chatter. |
| `amazon.instance.cron_pull_stock()` | Logs warning only. | Scheduled pull stock is advertised but inactive/nonfunctional. |
| `amazon_api.get_inventory_summaries()` | Implemented but unused. | Better inventory endpoint coverage is dead code. |
| `amazon.instance.action_export_stock()` | No feed status polling. | User sees feed submitted, not accepted/applied. |
| `amazon.product.action_update_in_amazon()` | Stock push errors only logged as warnings. | Product update may appear successful while stock update failed. |
| `amazon.inbound.shipment` | Uses `_sql_constraints`. | Deprecated on Odoo 19. |
| `amazon.product` | Uses `_sql_constraints`. | Deprecated on Odoo 19. |

### Low

| File/method | Issue | Impact |
|---|---|---|
| Dashboard/AI alert code | Uses `amazon_qty` as stock signal. | KPI/alerts can mislead if `amazon_qty` is stale. |
| `stock_picking._send_amazon_delivery_email()` | Sends local email after delivery validation. | Not Amazon sync, but can send unexpected customer email. |
| Module tests | No tests found. | Regression risk. |

## M. Missing Features

Proven missing from the module:

- Inventory background job model.
- Inventory push queue.
- Inventory pull queue.
- Persistent inventory pagination/NextToken.
- FBA Inventory Summaries-based pull flow.
- FBM Amazon → Odoo stock pull.
- Stock buffer/safety quantity/max quantity.
- Reserved/outgoing quantity calculation for Amazon push.
- Per-instance inventory sync batch size.
- Per-instance inventory direction setting.
- FBA/FBM stock source location fields.
- Automatic FBA warehouse/location creation.
- FBA virtual location strategy.
- Safe inventory adjustment using auditable adjustment workflow.
- Feed processing-result polling for stock and shipment feeds.
- Shipment confirmation idempotency key/model.
- Partial shipment based on Odoo move lines and Amazon order item IDs.
- Multiple package support.
- Carrier code mapping.
- Amazon → Odoo tracking pull.
- Safe rollback isolation for outbound Amazon calls.
- FBA inbound tracking update API call.
- FBA label download.
- MCF background job/cron.
- Record rules/company isolation.

## N. Reusable Existing Components

Keep and reuse:

- `AmazonAPI._amazon_request()` retry/log sanitization.
- `AmazonAPI.submit_feed()` feed document/upload/create flow.
- `AmazonAPI.build_inventory_json_feed()` as a starting point for MFN stock push.
- Product SKU mapping via `amazon.product.sku` and `odoo_product_id`.
- Unique Amazon order constraint on `amazon.sale.order`.
- Order import job architecture.
- Order status sync job architecture.
- `amazon.sync.log` model.
- Instance warehouse fields, after adding company constraints and location strategy.
- Sale Order / Picking Amazon link fields.

## O. Recommended Next Step

Recommendation: `6. Rebuild Inventory/Fulfillment`.

Reason:

The current code has useful pieces, but the production workflow is not just incomplete; key parts are unsafe:

- Direct `stock.quant` writes.
- No inventory jobs.
- No feed-result polling.
- No shipment idempotency.
- Broken partial shipment logic.
- Weak security and multi-company isolation.
- FBA/FBM warehouse/location design is incomplete.

Reusing components is reasonable, but the Inventory/Fulfillment feature should be rebuilt around persistent jobs, explicit stock policy, safe accounting/stock operations, and auditable Amazon feed lifecycle tracking.

## P. Proposed Implementation Phases

Do not implement yet.

Phase 1 — Safety foundation:

- Add inventory/fulfillment settings.
- Add company-safe warehouse/location constraints.
- Add stock buffer/safety/max quantity fields.
- Add inventory sync job model.
- Add shipment confirmation job model.
- Add sync-log coverage.
- Disable unsafe direct quant update paths or gate them behind explicit manual reconciliation mode.

Phase 2 — Inventory push/pull:

- Implement MFN stock push job.
- Calculate available quantity from configured warehouse/location.
- Apply buffer/reserved/outgoing logic.
- Submit JSON Listings feed.
- Poll feed processing result.
- Persist feed IDs, status, errors.
- Use batch size and retries.

Phase 3 — Fulfillment:

- Implement safe FBM shipment confirmation queue.
- Build escaped XML or structured payload safely.
- Support Amazon order item IDs per move line.
- Support partial shipments.
- Add carrier mapping.
- Add duplicate-confirmation protection.
- Keep FBA shipment handling read-only unless a separate FBA inventory phase is designed.

Testing phase:

- Unit tests for quantity calculation.
- Mock Amazon feed acceptance/rejection.
- Multi-line partial shipment tests.
- Multi-package tests.
- FBA vs FBM warehouse tests.
- Multi-company access tests.
- Restart/resume tests.
- Duplicate feed prevention tests.

## Q. Files That Would Need Modification

Likely files for a future implementation:

- `models/amazon_instance.py`
- `models/amazon_api.py`
- `models/amazon_product.py`
- `models/amazon_sale_order.py`
- `models/sale_order_inherit.py`
- `models/stock_picking_inherit.py`
- `models/amazon_fba_inventory.py`
- `models/amazon_inbound_shipment.py`
- `models/amazon_outbound_order.py`
- `models/amazon_sync_log.py`
- `security/ir.model.access.csv`
- `data/cron.xml`
- `views/instance_view.xml`
- `views/fba_inventory_view.xml`
- `views/delivery_view.xml`
- `views/inbound_shipment_view.xml`
- `views/outbound_order_view.xml`
- `__manifest__.py`
- new inventory/fulfillment job models and views
- new tests directory/files

No implementation was performed as part of the investigation.
