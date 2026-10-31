# Amazon–Odoo Project Technical Handoff

**Prepared:** 2026-10-28, via live read-only inspection of the repository, git history, and the production-adjacent database `amazon_prod12sep` running on server `ionic-odoo-dev-01`.

**Scope:** `sdlc_amazon_connector` Odoo 19 Community module only. Does not cover any other integration.

**Method note:** Every fact below was re-verified today by reading the current source tree, current git state, and current database content — not pulled from older documents in `docs/` or prior conversations. Where this report's numbers differ from older docs (`docs/CURRENT_STATUS.md`, `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md`, etc.), **this report is current and those are historical snapshots** — they are cited only for change history, never as current state.

---

# 1. Executive Summary

**What it does:** `sdlc_amazon_connector` is a custom Odoo 19 Community module that integrates Odoo with Amazon's Selling Partner API (SP-API) for a single Amazon Egypt seller account, with the current operational focus on **FBA (Fulfilled by Amazon)** order import, FBA inventory tracking, and FBA inbound shipment management. It also contains substantial code for FBM orders, settlements, returns, removals, reimbursements, and a set of AI-assisted tools, but — per live database evidence — most of those secondary areas are **implemented in code but not yet operationally active**.

**Current implementation status (confirmed):**
- Module installed, version `19.0.11.2.0`, state `installed` (no pending upgrade).
- **Live and running:** order import job processing, order status sync, FBA sale-stock depletion, inbound operation polling, inbound receiving sync, daily FBA inventory audits, operational health/monitoring crons.
- **Implemented but dormant (cron inactive, zero records):** settlement import, payout, returns import, removal-order import, FBA inventory-adjustment import, FBA reimbursement import, product price push, stock export, full bidirectional sync, master auto-sync scheduler.
- 43 Amazon sale orders imported, 55 FBA sale-stock events processed (all `done`, zero failures), 19 Amazon products all mapped to Odoo products.

**Current databases:**
- `amazon_prod12sep` — the database actually inspected for this report; contains the live Amazon Egypt Production instance (id=6) and all counts cited here.
- `amazon_prod` (111 MB) and `test_cutover_v2` (87 MB) also exist on the server but were **not** inspected for this report.
- An additional database literally named `amazon_prod12sep;` (semicolon included in the name, 22 MB) exists — see Section 10, flagged as an anomaly, not resolved here.
- The Odoo config (`/etc/odoo19.conf`) has **no fixed `db_name`** and `dbfilter = .*` — i.e., the Odoo service is not hard-pinned to one database. Which database is "the" production database is a configuration/operational fact, not something enforced by `odoo19.conf` — see Open Questions (Section 15).

**Current business scope:** Amazon Egypt FBA order and inventory synchronization is in active production use. Settlement accounting, returns, removals, and AI features are coded but not configured/enabled.

**Current recommended next step:** See Section 16 — configure settlement accounting (journals/accounts on the instance) is the correct next step if finance go-live is the goal; however, given a real anomaly found today (FBA Sellable quantity inconsistency, Section 10), the recommended *immediate* next step is to investigate that discrepancy before any new feature work.

---

# 2. Project Identity and Environment

All values below were read directly from the live server today.

| Item | Confirmed value |
|---|---|
| Odoo version/edition | Odoo 19 Community |
| Python version | 3.12.3 (system interpreter; `/opt/odoo19/venv/` exists but its `python3`/`python3.12` are symlinked to the system `/usr/bin/python3`, i.e. **not an isolated interpreter build**) |
| Ubuntu version | 24.04.4 LTS (Noble Numbat) |
| PostgreSQL version | 16.15 (cluster `16/main`) |
| PostgreSQL port | 5432 (confirmed in `postgresql.conf`) |
| Repository URL | `git@github.com:Mohamed01097/amazon-odoo.git` |
| Local repository path | `/opt/odoo19/custom-addons/amazon-odoo` |
| Main branch | `main` (only local branch; only remote branch is `origin/main`; no tags) |
| Odoo service name | `odoo19` (systemd) — confirmed `active (running)`, `enabled` |
| Odoo service uptime at check time | since 2026-10-23 06:34:10 EEST |
| Nginx | `active (running)`, `enabled`; single site `odoo19`; `server_name _` (catch-all, no domain bound); `proxy_pass` → `127.0.0.1:8069`; no TLS/SSL block in this config |
| Addons paths | `/opt/odoo19/odoo/odoo/addons`, `/opt/odoo19/odoo/addons`, `/opt/odoo19/custom-addons`, `/opt/odoo19/custom-addons/amazon-odoo` |
| Odoo config file | `/etc/odoo19.conf` |
| Odoo log path | `/var/log/odoo19/odoo19.log` |
| Data dir | `/opt/odoo19/data` |
| HTTP bind | `127.0.0.1:8069` (internal only; `proxy_mode = True`) |
| Workers | `workers = 0` — **not multi-process prefork**; single-process/threaded mode. Confirm intentional (see Section 10). |
| max_cron_threads | `1` |
| dbfilter | `.*` — multi-database; **no fixed `db_name` key present in config** |
| Backup paths | `odoo19-backup.service` / `odoo19-backup.timer` systemd units exist (referenced in `docs/OPERATIONS_RUNBOOK.md`); not independently re-verified today — treat as **assumption** pending a fresh check |
| Server hostname | `ionic-odoo-dev-01` |
| Database names present | `amazon_prod12sep` (231 MB), `amazon_prod` (111 MB), `test_cutover_v2` (87 MB), `ahmed_elhosieny` (268 MB, unrelated project), `amazon_prod12sep;` (22 MB, anomalous), `postgres` (system) |

No IPs are reported — not independently confirmed as safe/relevant to include.

---

# 3. Repository and Module Structure

## Repository layout (root)

```
/opt/odoo19/custom-addons/amazon-odoo/
├── sdlc_amazon_connector/        ← the module covered by this report
├── docs/                          ← repo-root project/handoff docs (not part of the installed module)
└── [latest commit adds an unrelated module — see note below]
```

**Important note on repo scope:** the current HEAD commit (`e9a34e9`, 2026-10-18) is titled *"Add initial implementation of PostgreSQL Query Deluxe module with basic query execution and PDF printing functionality."* This adds a **second, unrelated custom module** to the repository. It is explicitly **out of scope** for this handoff (per task instructions to cover only the Amazon integration) and is not analyzed further here — a future assistant should be aware this module exists in the same repo but is a separate concern.

## Main custom addon path

`/opt/odoo19/custom-addons/amazon-odoo/sdlc_amazon_connector/`

## Module manifest (`__manifest__.py`)

| Key | Value |
|---|---|
| name | `Odoo Amazon Connector` |
| version | `19.0.11.2.0` |
| summary | `Amazon SP-API Connector for Odoo with Orders, Products, FBA, Settlements, Inventory, Pricing, Returns, and AI Automation` |
| author / maintainer | `SDLC Corp` |
| category | `Sales` |
| license | `OPL-1` |
| depends | `sale_management`, `stock`, `contacts`, `account`, `mail`, `purchase`, `delivery` |
| installable | `True` |
| application | `True` |
| auto_install | `False` |
| external_dependencies | **Not declared** — see Risk in Section 10 |

Data files loaded (34 entries, in order): `security/security.xml`, `security/ir.model.access.csv`, then 28 view XML files, then `data/cron.xml`, `data/sequence.xml`.

Assets (`web.assets_backend`): `static/src/scss/fba_appointment.scss`, `static/src/js/amazon_dashboard.js`, `static/src/xml/amazon_dashboard.xml`.

## Dependencies / Python packages

- No `requirements.txt`, `setup.py`, or `pyproject.toml` anywhere in the repository.
- Manifest declares no `external_dependencies`.
- Code imports `requests` (SP-API HTTP calls) and `dateutil` (date parsing) directly — these are **undeclared third-party dependencies**; they currently work only because they happen to be present in the system/venv Python environment, not because the module declares or installs them.

## Models (`models/*.py` — 41 files)

| File | Model(s) defined (`_name`) or extended (`_inherit`) | Responsibility |
|---|---|---|
| `account_move_inherit.py` | `_inherit='account.move'` | Amazon-related fields/links on invoices |
| `ai_forecast.py` | `amazon.demand.forecast` | AI demand forecasting |
| `ai_listing.py` | `amazon.ai.listing` | AI listing optimization |
| `ai_pricing.py` | `amazon.ai.pricing`, `amazon.ai.pricing.wizard` | AI price suggestions, bulk reprice wizard |
| `ai_profit_calculator.py` | `amazon.profit.calculator`, `amazon.competitor.monitor`, `amazon.keyword.tracker`, `amazon.return.analyzer`, `amazon.sales.insights` | Profit/ROI, competitor pricing, SEO keywords, return-pattern analysis, sales insights |
| `ai_reviews.py` | `amazon.review.analysis`, `amazon.ai.chat` | AI review analysis, AI chat history |
| `amazon_api.py` | plain Python class `AmazonAPI` (not an Odoo model) | SP-API HTTP client wrapper — the single point of contact with Amazon's API |
| `amazon_fba_inventory.py` | `amazon.fba.inventory.report(.line)` | Legacy FBA inventory report model |
| `amazon_fba_options.py` | `amazon.fba.packing.option/group/group.item`, `amazon.fba.box(.line)`, `amazon.fba.bulk.box.wizard` | FBA packing options/groups/boxes |
| `amazon_fba_sale_stock.py` | `amazon.fba.sale.stock.event`; extends `amazon.sale.order.line`, `stock.picking` | **Core FBA stock-depletion event model** — the heart of the live order→stock pipeline |
| `amazon_fba_sale_stock_cutover.py` | `amazon.fba.sale.stock.cutover.baseline/run/shipment` | Historical "cutover" baseline evidence used to prevent double-depleting pre-go-live Amazon fulfillments |
| `amazon_inbound_operation_job.py` | `amazon.inbound.operation.job` | Async job tracking for Amazon inbound-plan operations |
| `amazon_inbound_receiving.py` | extends `amazon.inbound.shipment(.line)`, `amazon.fba.physical.shipment(.line)`, `stock.picking`, `stock.move`, `amazon.inbound.operation.job`; new `amazon.fba.inventory.discrepancy` | Receiving-side logic, inventory discrepancies |
| `amazon_inbound_shipment.py` | `amazon.inbound.shipment(.line)` | Core inbound shipment plan model |
| `amazon_inbound_shipping.py` | extends `amazon.inbound.shipment`, `amazon.fba.physical.shipment`, `stock.picking`; new `amazon.fba.delivery.window.option`, `amazon.fba.appointment.slot`, `amazon.fba.shipment.box`, `amazon.fba.transportation.option` | **Transportation / Ready-to-Ship / dispatch logic** — largest model file (160 KB) |
| `amazon_instance.py` | `amazon.instance` | Core connection/config model; hosts most cron entry-point methods |
| `amazon_inventory_reconciliation.py` | `amazon.inventory.reconciliation.run`, `amazon.inventory.reconciliation`; extends `amazon.instance`, `stock.picking` | FBA inventory audit/reconciliation |
| `amazon_operations.py` | `amazon.operation.control`, `amazon.operation.job.monitor`, `amazon.api.operation.metric`, `amazon.operations.dashboard`; extends `amazon.instance`, `amazon.order.import.job` | Operational observability — retries, job monitoring, metrics, dashboard |
| `amazon_order_import_job.py` | `amazon.order.import.job` | Order import job queue |
| `amazon_order_status_sync_job.py` | `amazon.order.status.sync.job` | Order status sync job queue — primary order-discovery mechanism |
| `amazon_outbound_order.py` | `amazon.outbound.order(.line)` | Amazon Multi-Channel Fulfillment (MCF) orders |
| `amazon_payout.py` | `amazon.payout`, `amazon.payout.allocation`; extends `amazon.settlement.report` | Bank payout evidence and allocation to settlements |
| `amazon_phase7.py` | extends `amazon.smart.alert`, `amazon.instance`; new `amazon.phase7.stock.service` (abstract), `amazon.fba.inventory.adjustment`, `amazon.fba.reimbursement`, `amazon.phase7.job` | Returns/adjustments/reimbursements processing + generic job queue |
| `amazon_product.py` | `amazon.product` | Amazon catalog/listing model, SKU↔product mapping, product-type detection |
| `amazon_product_health.py` | `amazon.product.health` | Product health scoring |
| `amazon_rating.py` | `amazon.rating.report(.line)` | Seller rating reports |
| `amazon_removal_order.py` | `amazon.removal.order(.line)`, `amazon.removal.shipment` | FBA removal orders |
| `amazon_return.py` | `amazon.return.report(.line)` | FBA customer return import |
| `amazon_sale_order.py` | `amazon.sale.order(.line)` | Mirror of Amazon orders in Odoo |
| `amazon_settlement.py` | `amazon.settlement.report(.line)` | Settlement report + financial lines |
| `amazon_smart_alerts.py` | `amazon.smart.alert` | Alerting |
| `amazon_sync_log.py` | `amazon.sync.log` | Sync operation audit log |
| `amazon_user_guidance.py` | `amazon.user.guidance` (transient) | In-app guidance |
| `amazon_vcs.py` | `amazon.vcs.tax.report(.line)` | VCS (Amazon's tax calculation service) report |
| `res_config_settings.py` | extends `res.config.settings` | Settings panel integration |
| `sale_order_inherit.py` | extends `sale.order`, `sale.order.line` | Amazon fields on native sale orders |
| `stock_location_inherit.py` | extends `stock.location` | Amazon FBA location-type fields |
| `stock_picking_inherit.py` | extends `stock.picking` | Amazon fields on pickings |

Non-model: `services/ai_service.py` — plain Python class `AmazonAIService`, centralizes calls to OpenAI/Anthropic/Google Gemini for listing generation, product-type detection, price optimization, inventory prediction, customer-reply generation, and error analysis.

## Controllers (`controllers/*.py`)

- `ai_chat_controller.py` — `AmazonAIChatController`: `POST /amazon/ai/chat`, `POST /amazon/ai/chat/history`, `POST /amazon/ai/dashboard-stats` (all `type=jsonrpc`, `auth=user`).
- `dashboard_controller.py` — `AmazonDashboardController`: `GET /amazon/dashboard/instances`, `/data`, `/ai-insights`, `/optimize-store` (all `type=jsonrpc`, `auth=user`).

## Wizards (`wizard/*.py`)

- `import_order_by_id_wizard.py` — `amazon.import.order.by.id.wizard` (transient). Manager-only, imports exactly one Amazon order by ID via `getOrder`; can hold the resulting FBA stock event for manual review before any stock move happens.
- `product_import_wizard.py` — `amazon.product.import.wizard`, `amazon.product.setup.wizard` (transient). Bulk CSV/Excel product mapping and setup.
- `schedule_appointment_wizard.py` — `amazon.fba.schedule.appointment.wizard` (transient). Confirms scheduling of one self-ship FC appointment slot; only the explicit "Confirm Schedule" button triggers the synchronous Amazon `scheduleSelfShipAppointment` write.
- `transportation_ready_to_ship_wizard.py` — `amazon.fba.transportation.ready.to.ship.wizard(.line)` (transient). Collects Ready-to-Ship date/time value(s) before (re)generating transportation options via Amazon's `generateTransportationOptions`.

## Views (`views/*.xml` — 30 files)

Key file: `views/menu.xml` defines the full menu tree under root `amazon_root` ("Amazon") with submenus for Dashboard, Operations, Configuration, Catalog, Orders, Delivery, FBA, AI Tools, Alerts, Returns & Refunds, Accounting, and Reports (~50 `<menuitem>` records). Action records (`ir.actions.act_window` / `ir.actions.server` / `ir.actions.client`) are spread across nearly every other view file, one action set per model area (instance, product, sale order FBM/FBA, inbound shipment, removal/disposal, settlement, payout, VCS, phase7 job, FBA inventory adjustment/reimbursement, all AI tool models, operations dashboard/metrics/monitor/retry-center).

## Security

- `security/security.xml` — `ir.module.category` (`module_category_sdlc_amazon_connector`), `res.groups.privilege`, three groups: `group_amazon_user`, `group_amazon_manager`, `group_amazon_technical_admin`; ~50 `ir.rule` multi-company record rules covering virtually every model in the module.
- `security/ir.model.access.csv` — access rows for essentially every model in the module, for the three Amazon groups plus `account.group_account_manager` (on settlement/payout/reimbursement models) and `stock.group_stock_manager` (on removal/inventory-adjustment models).

## Cron and data files

- `data/cron.xml` — 33 `ir.cron` records, all `noupdate="1"` (see Section 7 for the full table).
- `data/sequence.xml` — 10 `ir.sequence` records for settlement (`SETTLE/`), payout (`PAYOUT/`), return (`RET/`), removal (`REM/`), phase7 job (`FBA7/`), inbound shipment (`INB/`), legacy FBA inventory report (`FBAINV/`), inventory reconciliation run (`FBAAUDIT/`), rating (`RATE/`), VCS (`VCS/`), outbound/MCF (`MCF/`).
- No other seed CSV/XML data files exist in the module.

## Static assets

`static/description/` (~60 marketing images/banner/icon for the Odoo Apps listing) plus a duplicate `static/descriptionnn/` folder with the same assets — appears to be a legacy/unused leftover, not referenced by the manifest. `static/src/js/amazon_dashboard.js`, `static/src/xml/amazon_dashboard.xml`, `static/src/scss/fba_appointment.scss` — the dashboard frontend widget.

## Tests (`tests/*.py` — 29 files)

All `TransactionCase`-based. Full list with purpose in Section 13 / Section 20 equivalent below (see Section 13).

## Migrations (`migrations/`)

| Version | Script | Purpose |
|---|---|---|
| `19.0.10.2.0` | `post-migrate.py` | Makes already-mapped FBA products stockable (`is_storable=True`) without moving stock |
| `19.0.10.3.0` | `post-migrate.py` | Adds the sold/customer FBA location via `action_create_fba_stock_structure()` without changing quantities |
| `19.0.10.6.2` | `post-migrate.py` | Ready-to-Ship validation data cleanup; checks for in-flight `generate_transportation_options` jobs before cleanup |
| `19.0.11.2.0` | `pre-migrate.py` | Flips `ir.model.data.noupdate` to `True` for existing cron records before `data/cron.xml` reloads with `noupdate="1"`, so upgrades stop silently resetting operator-set `active`/`interval_number`/`interval_type`/`priority` |

## Documentation

**Module-level** (`sdlc_amazon_connector/docs/`, 9 files): `PHASE21_OPERATIONAL_READINESS.md`, `PHASE22_STAGING_ISOLATION.md`, `PHASE24_LIVE_API_UAT.md`, `inventory_fulfillment_gap_analysis.md`, `payout_clearing_reconciliation.md`, `phase7_amazon_sp_api.md`, `phase_6_5_operations.md`, `settlement_accounting_posting.md`, `settlement_v2_integration.md`.

**Repo-root** (`docs/`, sibling to the module, NOT part of the installed module — 20 files including `ACCOUNT_MIGRATION_MASTER_HANDOFF.md`, `IMPLEMENTER_HANDOFF.md` (+ Arabic translation), `CURRENT_STATUS.md` (+ Arabic translation), `OPERATIONS_RUNBOOK.md`, `ARCHITECTURE.md`, `BUSINESS_REQUIREMENTS.md`, `DECISIONS.md`, `SECURITY_AND_SECRETS.md`, `TESTING_GUIDE.md`, and others). These are useful historical/onboarding context but **contain numbers from past snapshots (e.g. Sep 20, 2026) that are now outdated** — see the cross-reference in Section 24-equivalent discussion under Section 15.

---

# 4. Amazon Integration Architecture

**Amazon instance configuration:** The `amazon.instance` model (`models/amazon_instance.py`, 137 KB — the largest and most central model file) holds per-seller configuration: Seller ID, Marketplace ID, region, FBA warehouse/location mapping, sync interval fields, and — per the database schema check performed today — credential fields including `client_secret`, `aws_secret_key`, `refresh_token`, and `ai_api_key` (none of these were queried for values; only their existence as columns was confirmed).

**Login With Amazon (LWA) / refresh token flow:** The architecture (per model structure and `amazon_api.py`) stores a long-lived LWA refresh token on the instance and exchanges it for short-lived access tokens at call time via the SP-API client wrapper in `amazon_api.py`. **Actual refresh-token exchange code was not read line-by-line for this report** (to avoid any risk of surfacing a credential-adjacent value) — this is stated as an **assumption based on standard SP-API architecture and the presence of a `refresh_token` column**, not a line-by-line verified code trace.

**Client ID / client secret usage:** Confirmed only as columns present on `amazon_instance` (not inspected for values or usage pattern in this pass).

**Seller ID / Marketplace ID / Region (confirmed from live data):** Instance id=6, seller `A2LMV58FMUN2ZD`, marketplace `ARBP9OOSHTCHU`, region `eu`.

**SP-API client structure:** `models/amazon_api.py` defines `class AmazonAPI` as a plain Python class (not an Odoo model) — a dedicated SP-API HTTP client wrapper, imported and used by the various job-processing models rather than embedding HTTP logic directly in each model. This is the single point of contact with Amazon's API surface.

**Request signing, rate-limit handling, retry handling, error handling:** Not independently re-verified line-by-line in this pass (would require deep reading of `amazon_api.py`'s ~91 KB of code, which was out of scope for the time available). What **is** confirmed structurally:
- `amazon_order_import_job.py` defines an `AmazonRateLimitDeferred` exception class, indicating rate-limit-aware job deferral exists.
- `amazon_operations.py` (`amazon.operation.control`) provides a generic retry/monitoring framework with crons `cron_amazon_dispatch_operational_retries` and `cron_amazon_detect_stuck_jobs`.
- `tests/test_phase24_diagnostic_safety.py` tests an API request-guard allow/deny mechanism (e.g., confirms GET-style read operations like orders/inventory lookups are allowed), suggesting a write-vs-read API guard exists somewhere in the call path.

**Sync logging:** `amazon.sync.log` model exists (`amazon_sync_log.py`) for audit trail of sync operations.

**API operation jobs / background processing:** The architecture is **two-tier**: "dispatcher" crons create job records (e.g. `amazon.order.import.job`, `amazon.order.status.sync.job`, `amazon.inbound.operation.job`, `amazon.phase7.job`), and separate "processor" crons pick up pending jobs and execute the actual API calls. This pattern repeats across orders, inbound operations, and phase7 (returns/removals/reimbursements) — it is the dominant architectural pattern in this module, confirmed by the cron table in Section 7 (pairs like "Process Order Import Jobs" / underlying job model, "Poll Inbound Operations" / `amazon.inbound.operation.job`, etc.).

**Idempotency and duplicate prevention:** Confirmed structurally via:
- `amazon.sale.order` is expected to key on `amazon_order_ref` (not re-verified for a DB unique constraint in this pass — **assumption based on test file naming**, e.g. `test_orders_import_hardening.py` covers "controlled import" hardening).
- `test_fba_hold_durability.py` — "Controlled-import FBA hold must survive normal sync/import/upsert paths," implying an explicit hold/dedupe mechanism for FBA stock events.
- The Cutover model (`amazon_fba_sale_stock_cutover.py`) exists specifically to prevent double-counting historical pre-go-live Amazon fulfillments against Odoo's opening stock (see also `test_fba_cutover_v2.py`, `test_fba_auto_cutover.py`).

**Caveat:** This section documents architecture *inferred from model/test/file structure*, which is a reliable but indirect signal. A developer continuing this project should read `amazon_api.py` directly before making any change to request signing, retry, or rate-limit behavior — do not assume this summary is a substitute for reading that file.

---

# 5. Implemented Amazon Features

## Orders

- **Models:** `amazon.sale.order`, `amazon.sale.order.line` (`amazon_sale_order.py`); extensions to `sale.order`/`sale.order.line` (`sale_order_inherit.py`); job queues `amazon.order.import.job` (`amazon_order_import_job.py`), `amazon.order.status.sync.job` (`amazon_order_status_sync_job.py`).
- **Main methods:** `cron_process_order_import_jobs()`, `cron_sync_order_statuses()`, `cron_import_orders()` (on `amazon.instance`); `amazon.import.order.by.id.wizard` for single-order manual import.
- **Crons:** `cron_amazon_process_order_import_jobs` (1 min, **active**), `cron_amazon_sync_order_statuses` (15 min, **active**), `cron_amazon_import_orders` (30 min, **inactive**), `cron_amazon_import_fbm_orders` (15 min, **inactive**), `cron_amazon_check_canceled` (2 h, **inactive**), `cron_amazon_update_fbm_status` (1 h, **inactive**).
- **UI:** Amazon > Orders menu (All Orders, Import Jobs, Status Sync Jobs, FBM/FBA split actions in `amazon_sale_order_view.xml` and related).
- **Status (confirmed from DB):** 43 `amazon_sale_order` rows exist; 11 import jobs, all `done`; 3,637 status-sync jobs, all `done`. Status sync (not the "Import All Orders" cron, which is inactive) is the primary live order-discovery mechanism.
- **FBA vs FBM handling:** FBM lifecycle is covered by dedicated tests (`test_fbm_lifecycle.py` — "FBM local lifecycle: import -> draft SO -> confirm -> native delivery -> invoice -> status sync... FBM must NOT create FBA stock events"), confirming the code explicitly distinguishes the two fulfillment channels. No FBM-specific crons are active today (`cron_amazon_import_fbm_orders` and `cron_amazon_update_fbm_status` both inactive) — **current live traffic is FBA only, by configuration, not by code limitation.**
- **Duplicate prevention:** Covered by `test_orders_import_hardening.py` and `test_fba_hold_durability.py` (see Section 4) — not independently re-verified via a DB constraint query in this pass.
- **Known limitations:** No evidence in this pass of FBM being exercised in production (0 active FBM crons); Egypt VAT/shipping/promotion handling is covered by `test_orders_vat_shipping.py` but production VAT configuration was not independently re-verified here.

## Products

- **Model:** `amazon.product` (`amazon_product.py`).
- **Confirmed (DB):** 19 `amazon_product` rows; **0 unmapped** (all have a non-null `odoo_product_id`).
- **Wizards:** `amazon.product.import.wizard`, `amazon.product.setup.wizard` for bulk CSV/Excel mapping.
- **Crons:** `cron_amazon_sync_products` (6 h, **inactive**), `cron_amazon_update_prices` (4 h, **inactive**), `cron_amazon_pull_prices` (6 h, **inactive**).
- **Status:** Product catalog is fully mapped today, but automated product sync and price push are both **not running** — current product data is effectively static unless manually re-imported via the wizards.
- **Variant handling:** Not independently verified in this pass (would require reading `amazon_product.py` in full, 116 KB).
- **Known limitation:** No automated detection of *new* Amazon SKUs appearing — relies on manual use of the import/setup wizard, since `cron_amazon_sync_products` is inactive.

## Inventory

- **Models:** `amazon.fba.sale.stock.event` (core depletion event, `amazon_fba_sale_stock.py`), `amazon.inventory.reconciliation.run`/`amazon.inventory.reconciliation` (`amazon_inventory_reconciliation.py`), `amazon.fba.sale.stock.cutover.baseline/run/shipment` (`amazon_fba_sale_stock_cutover.py`), legacy `amazon.fba.inventory.report(.line)`.
- **FBA location quantities (confirmed live, today):**

  | Location | ID | Odoo `complete_name` | Current quantity |
  |---|---|---|---|
  | Transit | 29 | Amazon FBA Transit | 1,571 |
  | Received/Staging | 30 | WH/Ionic/Amazon FBA Received / Staging | 0 |
  | **Sellable** | 31 | WH/Ionic/Amazon FBA Sellable | **963** |
  | Reserved | 32 | WH/Ionic/Amazon FBA Reserved | 48 |
  | Unsellable | 33 | WH/Ionic/Amazon FBA Unsellable | 8 |
  | Customer Returns | 34 | Amazon FBA Customer Returns | 0 |
  | Sold/Customers | 35 | Amazon FBA Sold / Customers | 55 |
  | Removal Transit | 36 | Amazon FBA Removal Transit | 0 |
  | Disposal | 37 | Amazon FBA Disposal / Inventory Loss | 0 |

  **Flag:** Older project documentation records an opening Sellable baseline of 944 units established on 2026-09-15. Today's live Sellable quantity is **963**, i.e. *higher* than that opening baseline despite 55 units having since been depleted to the Sold location. A simple opening-minus-depleted model (944 − 55 = 889) does **not** match the live value of 963. This is reported as a fact, not explained — see Section 10 (Risk) and Section 15 (Open Question). Do not assume this is "fine" without investigation; do not assume it is "broken" either — the cause (e.g. inbound receiving adding stock, a manual adjustment, a second cutover baseline event) was not determined in this pass.

- **Stock depletion logic:** The FBA sale-stock event model implements a delta-based depletion (`processed_fulfilled_qty` vs `amazon_cumulative_fulfilled_qty`, per model/field naming convention) paired with the Cutover baseline model to avoid double-depleting historical fulfillments that predate Odoo's opening stock snapshot. This pattern is extensively tested (`test_fba_sale_stock.py`, `test_fba_cutover_v2.py`, `test_fba_auto_cutover.py`) — confirmed from test file names/docstrings, not re-derived from a fresh line-by-line code read in this pass (prior sessions did verify the exact formula; not re-verified today — treat the precise arithmetic as **unconfirmed-in-this-pass**, though structurally consistent with what is observed).
- **Opening stock behavior:** Established once via a historical process (per older docs); **not something to re-run**.
- **Crons:** `cron_amazon_process_fba_sale_stock_events` (1 min, **active**), `cron_amazon_enqueue_inventory_audits` (daily, **active**), `cron_amazon_process_inventory_audits` (5 min, **active**), `cron_amazon_pull_stock` (4 h, compatibility cron, **inactive**), `cron_amazon_export_stock` (2 h, **inactive** — this is the dangerous write-to-Amazon cron).
- **Status (confirmed DB):** 55 FBA sale-stock events, all `state='done'`, zero `pending`/`failed`/`manual_review`. Last 5 inventory audit runs (`FBAAUDIT/00048`–`00052`) all `completed`, one per day for the last 5 days, on schedule.
- **Known limitation:** The Sellable-quantity discrepancy above is unresolved.

## FBA Inbound

- **Models:** `amazon.inbound.shipment(.line)` (`amazon_inbound_shipment.py`); extensions for shipping/transportation/dispatch in `amazon_inbound_shipping.py` (largest model file, 160 KB); receiving extensions in `amazon_inbound_receiving.py`; packing/box models in `amazon_fba_options.py`; `amazon.inbound.operation.job` (`amazon_inbound_operation_job.py`) for async Amazon operation polling.
- **Wizards:** `transportation_ready_to_ship_wizard.py` (Ready-to-Ship date collection before `generateTransportationOptions`), `schedule_appointment_wizard.py` (FC appointment slot scheduling).
- **Workflow stages covered by code/tests:** inbound plan creation (`test_fba_inbound_plan_phase2.py`), packing/placement options (`test_fba_packing_placement_phase3.py`), shipping/transportation including the Ready-to-Ship wizard (`test_fba_shipping_phase4.py`), receiving (`test_fba_receiving_phase5.py`), dispatch (`test_fba_dispatch.py`), FC appointment slots (`test_fba_appointment_slots.py`).
- **Crons:** `cron_amazon_process_inbound_operation_jobs` ("Poll Inbound Operations," 1 min, **active**), `cron_amazon_sync_inbound_receiving` ("Synchronize Inbound Receiving," 30 min, **active**).
- **Async Amazon operation polling / retry handling:** `amazon.inbound.operation.job` is the dedicated model for tracking long-running Amazon async operations (plan creation, packing confirmation, transportation generation, etc.) and polling them to completion; retry handling is shared with the generic `amazon.operation.control` framework.
- **Status mismatch handling:** `migrations/19.0.10.6.2/post-migrate.py` specifically performs Ready-to-Ship validation data cleanup and checks for in-flight `generate_transportation_options` jobs, indicating a known historical issue class around status mismatches during that transition — now migrated, not an open issue per se, but worth knowing the history exists.
- **Known limitation:** Inbound workflow correctness (plan→packing→transportation→shipping→receiving) was verified via test file existence/naming only in this pass, not via a fresh end-to-end functional trace against live data — there is no live inbound shipment currently in progress to inspect (not confirmed either way; no inbound-shipment-table query was run in this pass).

## Settlements and Finance

- **Models:** `amazon.settlement.report(.line)` (`amazon_settlement.py`), `amazon.payout`, `amazon.payout.allocation` (`amazon_payout.py`).
- **Confirmed live (today):** `amazon_settlement_report` = **0 rows**. `amazon_payout` = **0 rows**. All 15 settlement/accounting-related fields on `amazon_instance` id=6 (journal and account references, settlement cutoff date) are **NULL**. `settlement_accounting_strategy` = `settlement_based` (a default/chosen strategy value, but with no accounts configured it cannot yet post anything).
- **Crons:** `cron_amazon_import_settlement` (daily, **inactive**).
- **Accounting impact:** **None yet** — zero settlements imported, zero journal entries created, confirmed by direct query (`account_move` join via `amazon_settlement_report.account_move_id` returned 0).
- **Status:** **Implemented in code, not configured, not enabled.** This is the single largest piece of unfinished operational work in the project (see Section 16).

## Returns and Refunds

- **Model:** `amazon.return.report(.line)` (`amazon_return.py`).
- **Confirmed live:** `amazon_return_report` = **0 rows**.
- **Cron:** `cron_amazon_import_fba_customer_returns` (daily, **inactive**).
- **Status:** Implemented, not enabled, zero data.

## Removal Orders

- **Models:** `amazon.removal.order(.line)`, `amazon.removal.shipment` (`amazon_removal_order.py`).
- **Confirmed live:** `amazon_removal_order` = **0 rows**.
- **Cron:** `cron_amazon_import_removal` ("Refresh Removal Status," 4 h, **inactive**).
- **Status:** Implemented, not enabled, zero data.

## FBA Inventory Adjustments & Reimbursements (Phase 7)

- **Models:** `amazon.fba.inventory.adjustment`, `amazon.fba.reimbursement`, generic `amazon.phase7.job` (`amazon_phase7.py`).
- **Confirmed live:** `amazon_fba_inventory_adjustment` = **0 rows**. `amazon_fba_reimbursement` = **0 rows**. `amazon_phase7_job` = **43 rows**, all `operation_type='reimbursement_matching'`, all `state='done'` — i.e., the *matching* cron has run 43 times successfully but found **nothing to match** (consistent with 0 reimbursements imported, since the import crons for adjustments/reimbursements are inactive).
- **Crons:** `cron_amazon_import_fba_inventory_adjustments` (daily, **inactive**), `cron_amazon_import_fba_reimbursements` (daily, **inactive**), `cron_amazon_match_fba_reimbursements` (daily, **active**).
- **Status:** The matching job runs on a live schedule but is matching against an empty data set — functionally a no-op until the import crons are enabled. Not a bug; a direct consequence of only half the pipeline (matching, not importing) being turned on.

## AI Features

Document only what is actually implemented (per manifest summary and model files — confirmed present in code, **configuration status not independently re-verified via instance `ai_api_key` value** since that field was deliberately not queried as a credential field):

- **AI listing generation:** `amazon.ai.listing` model + `AmazonAIService.generate_listing()`.
- **AI product type detection:** `AmazonAIService.detect_product_type()`.
- **AI review analysis:** `amazon.review.analysis` model.
- **AI price optimization:** `amazon.ai.pricing` + `amazon.ai.pricing.wizard` + `AmazonAIService.optimize_price()`.
- **Profit calculator:** `amazon.profit.calculator`.
- **Competitor monitoring:** `amazon.competitor.monitor`.
- **Demand forecasting:** `amazon.demand.forecast` + `AmazonAIService.predict_inventory()`.
- **Product health:** `amazon.product.health` — cron `cron_amazon_health_scores` (daily, **inactive**).
- **Smart alerts:** `amazon.smart.alert` — crons `cron_amazon_smart_alerts` (4 h, **inactive**) and `cron_amazon_operational_alerts` ("Evaluate Operational Alerts," 15 min, **active** — but this is the *operational* alert evaluator from `amazon_operations.py`, distinct from the AI-driven smart-alert *scan* cron, which is inactive).
- **AI chat assistant:** `amazon.ai.chat` model + `ai_chat_controller.py` (3 JSON-RPC routes).
- **Supported providers:** `services/ai_service.py` supports OpenAI, Anthropic Claude, and Google Gemini (confirmed from class structure/docstring — exact provider selection/config mechanism not traced line-by-line in this pass).
- **Configuration/secrets handling:** An `ai_api_key` column exists on `amazon.instance`; its value was not queried (treated as a credential field). Whether it is currently populated is **unknown from this pass** — see Open Questions.
- **Status:** All of the above are implemented as models/services; **none have an active cron other than the operational-alerts evaluator**, and whether any AI provider key is actually configured is unconfirmed.

---

# 6. Database and Data Model

(Field lists below are representative, not exhaustive — `amazon_instance` alone has 135 columns. Credential columns are explicitly excluded.)

| Model | Purpose | Key fields observed | State field(s) | Key relationships | Cron dependency |
|---|---|---|---|---|---|
| `amazon.instance` | Seller connection + config | `seller_id`, `marketplace_id`, `region`, `active`, `company_id`, `settlement_accounting_strategy`, `settlement_accounting_cutoff_date`, 15 settlement journal/account fields (all NULL today), credential fields (excluded) | `active` (boolean) | `company_id` → `res.company`; hosts most cron methods | Almost all Amazon crons call a method on this model |
| `amazon.product` | Catalog/SKU mapping | `sku`, `odoo_product_id`, `parent_product_id`, `product_type`, `product_type_group` | — | `odoo_product_id` → `product.product` | `cron_amazon_sync_products` (inactive) |
| `amazon.sale.order` / `.line` | Mirror of Amazon orders | (order ref, status, instance link — exact field names not individually re-grepped this pass) | status field present (name not re-confirmed) | Links to native `sale.order` | `cron_amazon_process_order_import_jobs`, `cron_amazon_sync_order_statuses` |
| `amazon.order.import.job` | Import job queue | `state` | `state` (`done` confirmed; 11/11 done) | → `amazon.sale.order` created | `cron_amazon_process_order_import_jobs` |
| `amazon.order.status.sync.job` | Status sync job queue | `state` | `state` (`done`; 3,637/3,637 done) | → updates `amazon.sale.order` | `cron_amazon_sync_order_statuses` |
| `amazon.fba.sale.stock.event` | Core FBA depletion event | cumulative/processed fulfilled qty fields (per architecture in Section 5), `state` | `state` (`done`; 55/55 done, 0 other states) | → `stock.picking`/`stock.move` for Sellable→Sold transfers | `cron_amazon_process_fba_sale_stock_events` |
| `amazon.fba.inventory.report(.line)` | Legacy inventory report | — | — | — | none active |
| `amazon.inventory.reconciliation.run` / `.reconciliation` | Daily FBA audit | `name` (sequence `FBAAUDIT/xxxxx`), `state` | `state` (`completed` confirmed for last 5 runs) | — | `cron_amazon_enqueue_inventory_audits`, `cron_amazon_process_inventory_audits` |
| `amazon.inbound.shipment` / `.line` | FBA inbound plan | — | — | — | `cron_amazon_process_inbound_operation_jobs`, `cron_amazon_sync_inbound_receiving` |
| `amazon.inbound.operation.job` | Async inbound-operation tracking | — | likely `state` | → `amazon.inbound.shipment` | `cron_amazon_process_inbound_operation_jobs` |
| `amazon.outbound.order` / `.line` | MCF outbound orders | — | — | — | none confirmed active |
| `amazon.settlement.report` / `.line` | Settlement import | `account_move_id` (confirmed column exists, all currently unlinked — 0 settlements exist) | — | → `account.move` | `cron_amazon_import_settlement` (inactive) |
| `amazon.payout` / `.allocation` | Bank payout reconciliation | — | — | → `amazon.settlement.report` | none active (no payout-specific cron found) |
| `amazon.return.report` / `.line` | FBA return import | — | — | — | `cron_amazon_import_fba_customer_returns` (inactive) |
| `amazon.removal.order` / `.line`, `.shipment` | FBA removals | — | — | — | `cron_amazon_import_removal` (inactive) |
| `amazon.sync.log` | Sync audit trail | — | — | — | written by various jobs |
| `amazon.phase7.job` | Returns/adjustments/reimbursements job queue | `operation_type`, `state`, `cursor_index`, `batch_size`, `total_found/processed/failed`, `retry_count`/`max_retries`, `amazon_report_id`/`amazon_report_document_id`, `feed_id`/`feed_document_id`, `last_error_code`/`last_error_message` | `state` (`done`; 43/43 confirmed, all `reimbursement_matching`) | → `amazon.fba.reimbursement`/`amazon.fba.inventory.adjustment` | `cron_amazon_process_phase7_jobs`, `cron_amazon_match_fba_reimbursements` |
| `amazon.fba.inventory.adjustment` | Inventory adjustment records | — | — | — | `cron_amazon_import_fba_inventory_adjustments` (inactive; 0 rows) |
| `amazon.fba.reimbursement` | Reimbursement records | — | — | — | `cron_amazon_import_fba_reimbursements` (inactive; 0 rows) |
| AI models (`amazon.ai.*`, `amazon.demand.forecast`, etc.) | AI-assisted tooling | — | — | — | mostly inactive crons (health scores, smart-alert scan) |

**Caveat:** Several "Key fields observed" and "State field(s)" entries above are marked with exact confirmation only where a live query was run today (`amazon_phase7_job`, `amazon_product`, counts). Where a full column-by-column schema dump was not performed in this pass (e.g. `amazon_sale_order`, `amazon_inbound_shipment`), the model's *existence* and *row counts where queried* are confirmed, but the *exact field list* should be re-verified by a developer before writing code against it — do not trust field names in this table blindly for those rows; re-run `\d <table>` first.

---

# 7. Cron Jobs and Background Jobs

All data below is from a **live query against `ir_cron` joined to `ir_model_data` for module `sdlc_amazon_connector`**, run today — this is the authoritative current state, not the XML file defaults (which can differ from the live DB once an administrator has toggled a cron).

| Cron name | XML ID | DB id | Model | Method | Interval | Active | Read/Write | Amazon API impact | Odoo data impact | Risk | Known issue |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Amazon: Import All Orders (FBM + FBA) | `cron_amazon_import_orders` | 24 | `amazon.instance` | `cron_import_orders()` | 30 min | **Inactive** | Read | Reads orders | Creates orders | Low | Superseded by status-sync cron as primary discovery |
| Amazon: Process Order Import Jobs | `cron_amazon_process_order_import_jobs` | 25 | `amazon.order.import.job` | `cron_process_order_import_jobs()` | 1 min | **Active** | Read | Reads order detail | Creates `amazon.sale.order`/`sale.order` | Low | None observed (11/11 done) |
| Amazon: Sync Order Statuses | `cron_amazon_sync_order_statuses` | 26 | `amazon.order.status.sync.job` | `cron_sync_order_statuses()` | 15 min | **Active** | Read | Reads order status | Updates orders | Low | None observed (3637/3637 done) |
| Amazon: Process FBA Sale Stock Events | `cron_amazon_process_fba_sale_stock_events` | 27 | `amazon.fba.sale.stock.event` | `cron_process_fba_sale_stock_events()` | 1 min | **Active** | Internal (Odoo-only) | None | Creates stock moves | **Medium** (moves real stock) | Sellable-quantity discrepancy exists (Section 10) — not confirmed to originate here |
| Amazon: Sync Products | `cron_amazon_sync_products` | 28 | `amazon.instance` | `cron_sync_products()` | 6 h | **Inactive** | Read | Reads catalog | Updates product data | Low | New SKUs won't be auto-detected while inactive |
| Amazon: Update Product Prices | `cron_amazon_update_prices` | 29 | `amazon.instance` | `cron_update_prices()` | 4 h | **Inactive** | **Write** | **Writes prices to Amazon** | Reads Odoo prices | **High** | Intentionally disabled — no price policy approved |
| Amazon: Import Settlement Reports | `cron_amazon_import_settlement` | 30 | `amazon.instance` | `cron_import_settlement_reports()` | 1 day | **Inactive** | Read | Reads settlements | Would create settlement records | Low | Blocked on accounting configuration |
| Amazon: Check Canceled Orders | `cron_amazon_check_canceled` | 31 | `amazon.instance` | `cron_check_canceled_orders()` | 2 h | **Inactive** | Read | Reads order status | Updates orders | Low | Covered by status-sync cron |
| Amazon: Import FBM Orders | `cron_amazon_import_fbm_orders` | 32 | `amazon.instance` | `cron_import_fbm_orders()` | 15 min | **Inactive** | Read | Reads FBM orders | Creates orders | Low | FBA-only operation currently |
| Amazon: Export Stock Levels | `cron_amazon_export_stock` | 33 | `amazon.instance` | `cron_export_stock()` | 2 h | **Inactive** | **Write** | **Writes stock levels to Amazon** | Reads Odoo stock | **High** | Intentionally disabled — FBA stock is Amazon-managed |
| Amazon: Update FBM Order Status | `cron_amazon_update_fbm_status` | 34 | `amazon.instance` | `cron_update_fbm_order_status()` | 1 h | **Inactive** | Write (to Amazon, FBM only) | Writes FBM fulfillment status | None | Medium (if enabled) | FBA-only operation currently |
| Amazon: Poll Inbound Operations | `cron_amazon_process_inbound_operation_jobs` | 35 | `amazon.inbound.operation.job` | `cron_process_inbound_operation_jobs()` | 1 min | **Active** | Read (poll) | Polls async operation status | Updates job/shipment state | Low | None observed |
| Amazon: Synchronize Inbound Receiving | `cron_amazon_sync_inbound_receiving` | 36 | `amazon.fba.physical.shipment` | `cron_enqueue_receiving_sync()` | 30 min | **Active** | Read | Reads receiving evidence | Updates receiving records | Low | None observed |
| Amazon: Refresh Removal Status | `cron_amazon_import_removal` | 37 | `amazon.instance` | `cron_refresh_fba_removal_status()` | 4 h | **Inactive** | Read | Reads removal status | Updates removal records | Low | 0 removal orders exist, awaiting enablement decision |
| Amazon: Enqueue Inventory Audits (Compatibility) | `cron_amazon_pull_stock` | 38 | `amazon.instance` | `cron_pull_stock()` | 4 h | **Inactive** | Read | — | — | Low | Superseded by `cron_amazon_enqueue_inventory_audits` |
| Amazon: Enqueue Daily FBA Inventory Audits | `cron_amazon_enqueue_inventory_audits` | 39 | `amazon.inventory.reconciliation.run` | `cron_enqueue_inventory_audits()` | 1 day | **Active** | Internal | None | Creates audit run | Low | Running daily, all recent runs `completed` |
| Amazon: Process FBA Inventory Audits | `cron_amazon_process_inventory_audits` | 40 | `amazon.inventory.reconciliation.run` | `cron_process_inventory_audits()` | 5 min | **Active** | Read | Reads inventory summaries | Compares vs Odoo quants | Low | None observed |
| Amazon: Pull Prices | `cron_amazon_pull_prices` | 41 | `amazon.instance` | `cron_pull_prices()` | 6 h | **Inactive** | Read | Reads Amazon prices | — | Low | — |
| Amazon: Full Bidirectional Sync | `cron_amazon_full_sync` | 42 | `amazon.instance` | `cron_full_sync()` | 1 h | **Inactive** | **Read + Write** | **Bundles all read/write ops, including stock/price export** | Multiple | **High** | Must never be enabled for FBA per existing project guidance |
| Amazon: Master Auto-Sync Scheduler | `cron_amazon_master_scheduler` | 43 | `amazon.instance` | `cron_master_scheduler()` | 15 min | **Inactive** | Dispatches other sync types | Depends on `auto_sync_enabled` per instance | Multiple | **High** | Must stay off per existing project guidance |
| Amazon: Smart Alert Scan | `cron_amazon_smart_alerts` | 44 | `amazon.smart.alert` | `run_alert_scan()` | 4 h | **Inactive** | Internal (may call AI provider) | Possible AI API call | Creates alerts | Low | Optional feature, not enabled |
| Amazon: Calculate Product Health Scores | `cron_amazon_health_scores` | 45 | `amazon.product.health` | inline loop calling `calculate_all_health_scores(inst.id)` per instance | 1 day | **Inactive** | Internal | None | Updates health scores | Low | Optional feature, not enabled |
| Amazon: Check Connection Health | `cron_amazon_connection_health` | 46 | `amazon.instance` | `cron_run_health_checks()` | 15 min | **Active** | Read | Lightweight connectivity check | Updates health status | Low | None observed |
| Amazon: Refresh Operations Dashboard | `cron_amazon_refresh_operations_dashboard` | 47 | `amazon.operations.dashboard` | `cron_refresh_operations_dashboard()` | 15 min | **Active** | Internal | None | Refreshes dashboard stats | Low | None observed |
| Amazon: Detect Stuck Jobs | `cron_amazon_detect_stuck_jobs` | 48 | `amazon.operation.control` | `cron_detect_stuck_jobs()` | 10 min | **Active** | Internal | None | Flags stuck jobs | Low | None observed |
| Amazon: Dispatch Eligible Retries | `cron_amazon_dispatch_operational_retries` | 49 | `amazon.operation.control` | `cron_dispatch_operational_retries()` | 5 min | **Active** | Mixed (re-triggers failed ops) | Depends on underlying op | Retries jobs | Medium | None observed currently (no failed jobs to retry) |
| Amazon: Evaluate Operational Alerts | `cron_amazon_operational_alerts` | 50 | `amazon.smart.alert` | `cron_evaluate_operational_alerts()` | 15 min | **Active** | Internal | None | Creates alerts | Low | None observed |
| Amazon: Clean Successful Operational Logs | `cron_amazon_cleanup_operational_records` | 51 | `amazon.operation.control` | `cron_cleanup_operational_records()` | 1 day | **Active** | Internal | None | Deletes old log records | Low | None observed |
| Amazon: Process Removal Orders and FBA Events | `cron_amazon_process_phase7_jobs` | 52 | `amazon.phase7.job` | `cron_process_jobs()` | 5 min | **Active** | Mixed | Depends on job `operation_type` | Processes phase7 jobs | Medium | Currently only processing `reimbursement_matching` jobs (43 done, matching against 0 records) |
| Amazon: Import FBA Customer Returns | `cron_amazon_import_fba_customer_returns` | 53 | `amazon.instance` | `cron_import_fba_customer_returns()` | 1 day | **Inactive** | Read | Reads returns | Would create return records | Low | 0 returns exist, awaiting enablement |
| Amazon: Import FBA Inventory Adjustments | `cron_amazon_import_fba_inventory_adjustments` | 54 | `amazon.instance` | `cron_import_fba_inventory_adjustments()` | 1 day | **Inactive** | Read | Reads adjustments | Would create adjustment records | Low | 0 adjustments exist, awaiting enablement |
| Amazon: Import FBA Reimbursements | `cron_amazon_import_fba_reimbursements` | 55 | `amazon.instance` | `cron_import_fba_reimbursements()` | 1 day | **Inactive** | Read | Reads reimbursements | Would create reimbursement records | Low | 0 reimbursements exist, so matching (cron 56) has nothing to match |
| Amazon: Match FBA Reimbursements | `cron_amazon_match_fba_reimbursements` | 56 | `amazon.instance` | `cron_match_fba_reimbursements()` | 1 day | **Active** | Internal | None | Matches reimbursements to adjustments/returns | Low | Running but no-op (0 reimbursements imported) |

**Totals confirmed live today: 33 cron records, 16 active, 17 inactive.**

**Cron XML upgrade-safety issue (confirmed, resolved):** `data/cron.xml` sets `noupdate="1"` on all records specifically because earlier module upgrades could silently reset operator-set `active`/`interval_number`/`interval_type`/`priority` back to XML defaults. The `19.0.11.2.0` `pre-migrate.py` script retroactively flips `ir.model.data.noupdate` to `True` for existing DBs so this doesn't regress on upgrade from an older version. This is tested in `test_cron_upgrade_safety.py`. **This is a resolved historical issue, not a current open risk** — but any future module upgrade should still verify cron settings survive, as a regression test rather than blind trust.

---

# 8. Current Verified State

Everything in this section was freshly queried today unless explicitly marked otherwise.

| Item | Value | Freshly verified? |
|---|---|---|
| Manifest version | `19.0.11.2.0` | Yes |
| Installed module state | `installed`, `latest_version=19.0.11.2.0` (matches manifest, no pending upgrade) | Yes |
| Confirmed database inspected | `amazon_prod12sep` | Yes |
| Other databases present (not inspected) | `amazon_prod`, `test_cutover_v2`, `ahmed_elhosieny`, `amazon_prod12sep;` (anomalous) | Yes (existence only) |
| Confirmed Amazon instance | id=6, "Amazon Egypt Production" | Yes |
| Confirmed marketplace/seller | `ARBP9OOSHTCHU` / `A2LMV58FMUN2ZD`, region `eu` | Yes |
| Confirmed order count | 43 `amazon_sale_order` | Yes |
| Confirmed import job count/state | 11, all `done` | Yes |
| Confirmed status sync job count/state | 3,637, all `done` | Yes |
| Confirmed FBA sale-stock event count/state | 55, all `done`, 0 other states | Yes |
| Confirmed product mapping | 19 `amazon.product`, 0 unmapped | Yes |
| Confirmed settlement/payout/return/removal/adjustment/reimbursement counts | all **0** | Yes |
| Confirmed phase7 job count/state | 43, all `done`, all `reimbursement_matching` | Yes |
| Confirmed settlement accounting fields | all 15 NULL, strategy=`settlement_based` | Yes |
| Confirmed FBA location quantities | see Section 5 table | Yes |
| Confirmed cron state (33 records) | 16 active / 17 inactive | Yes |
| Confirmed last 5 inventory audits | `FBAAUDIT/00048`–`00052`, all `completed`, daily cadence | Yes |
| Test suite results (pass/fail counts) | **Not run in this pass** — no test execution was performed; this report only inspected test *file structure* (29 files, class names, docstrings) | **No — not freshly run.** Treat any pass/fail claims from older docs as unverified until the suite is actually executed. |
| Confirmed successful/failed live Amazon API calls today | Not independently queried — would require reading `amazon_sync_log` or `amazon_api_operation_metric` tables, which was not done in this pass | **No — not confirmed.** Only cron `lastcall` timestamps were checked, which show crons *ran* on schedule but not whether individual API calls inside them succeeded or failed. |
| Recent manual tests | None performed as part of this report (read-only inspection only, per task rules) | N/A |
| Production vs local/test evidence | `amazon_prod12sep` is the database actually containing the "Amazon Egypt Production" instance and real order/stock data; `amazon_prod` and `test_cutover_v2` exist but their content was not inspected — **do not assume which one Odoo actually serves at the HTTP layer**, since `dbfilter=.*` with no `db_name` means that is a request-routing/operational fact, not a config-file fact (see Open Questions) | Partially — DB content confirmed for `amazon_prod12sep` only |

---

# 9. Important Completed Changes

From `git log` on `main` (verified commit hashes/dates/messages; working tree is clean, no uncommitted changes, branch is up to date with `origin/main`).

| Commit | Date | Message | Files changed | Purpose | Deployment status |
|---|---|---|---|---|---|
| `e9a34e9` | 2026-10-18 | Add initial implementation of PostgreSQL Query Deluxe module... | 24 files | **Unrelated module**, out of scope | In repo on `main`; whether it's "deployed" (installed) wasn't checked — out of scope |
| `f91d1ce` | 2026-10-12 | Add company configuration transfer details to UAT documentation | 1 file (`PHASE24_LIVE_API_UAT.md`) | Docs only | On `main` |
| `aff4cb6` | 2026-10-11 | Add diagnostic safety tests and documentation for Phase 24 | 1 file | Docs | On `main` |
| `65428bd` | 2026-10-11 | Add diagnostic safety tests and documentation for Phase 24 | 3 files (incl. `tests/test_phase24_diagnostic_safety.py`) | Adds the API request-guard allow/deny test (Section 4) | On `main` |
| `2b3abad` | 2026-10-11 | Add Phase 22 staging isolation documentation and operational readiness details | 4 files (incl. `tests/test_phase23_uat.py`) | Docs + UAT test | On `main` |
| `79b2e3a` | 2026-10-10 | **Enhance cron configuration safety during module upgrades and add related tests** | 6 files (`__manifest__.py`, `data/cron.xml`, `migrations/19.0.11.2.0/pre-migrate.py`, `tests/test_cron_upgrade_safety.py`, `tests/test_orders_vat_shipping.py`) | **The cron `noupdate` upgrade-safety fix** (Section 7) | On `main`, confirmed live in DB today (33 crons all have correct active state preserved) |
| `36a9d1f` | 2026-10-10 | Bump version to 19.0.11.0.0 and enhance FBA order processing with controlled import holds and cutover management features | 15 files | Version bump + controlled-import hold logic | On `main` |
| `5d97bc0` | 2026-10-04 | Add import order by ID wizard and enhance order import functionality | 14 files | Adds `import_order_by_id_wizard.py` | On `main` |
| `b60c8c4` | 2026-10-02 | Add scheduling functionality for self-ship FC appointments | 7 files | Adds `schedule_appointment_wizard.py` | On `main` |
| `b1a8f52` | 2026-09-30 | **Add FC appointment slot generation functionality and related UI components** | 11 files, incl. `transportation_ready_to_ship_wizard_view.xml` and `wizard/transportation_ready_to_ship_wizard.py` | **This is the commit that introduces the Ready-to-Ship wizard** (no commit message literally contains the word "ready" — identified via the file-list, not a message grep) | On `main` |
| `19e46bc` | 2026-09-30 | Add bulk box creation wizard and support for item expiration dates in FBA | 6 files | Packing/box enhancements | On `main` |
| `b8d762c` | 2026-09-26 | (same title as above, different content) Add bulk box creation wizard and support for item expiration dates in FBA | 11 files | Packing/box enhancements | On `main` |
| `0751a48` | 2026-09-21 | hand off files (author: Mohamed01097) | 10 files | Documentation handoff | On `main` |
| `b9cd146` | 2026-09-15 | claude edits (author: Mohamed01097) | 10 files, adds `amazon_fba_sale_stock_cutover.py` (535 lines), `test_fba_cutover_v2.py` (938 lines) | **Introduces the Cutover V2 system** | On `main` |
| `e5b2520` | 2026-09-05 | Add FBA sale stock cutover and historical repair | 8 files | Earlier cutover work (v1, superseded by V2 above) | On `main` |
| `c3e2f08` | 2026-08-09 | Add transportation options management for Amazon FBA shipments | — | **Transportation flow introduction** | On `main` |
| `bd5da35` | 2026-08-08 | Add retry logic and error handling for Amazon inbound plan creation | — | **Inbound operation retry/error handling** | On `main` |
| `d8775ce` | 2026-08-09 | Refine inventory reconciliation process and update UI elements for clarity | — | Inventory reconciliation refinement | On `main` |
| `2f1d759` / `1b0860e` | 2026-08-01 | Add inventory reconciliation features and related configurations (two commits, same message, different hashes) | — | **Inventory reconciliation introduction** | On `main` |

**Searches that returned no results:** no commit message in the entire `main` history contains the literal words "ready" or "company_id" (case-insensitive). If a future assistant is asked to find an "instance visibility/company_id fix" commit, none was found by message search — it would need to be located by content/diff inspection, not assumed to exist.

**Authorship note:** the majority of history (through `b9cd146`) uses author `odoo16 <ezzat2616@gmail.com>`; two commits (`b9cd146`, `0751a48`) show author `Mohamed01097`; later commits (`b1a8f52` onward) revert to not being individually re-checked for author in this pass.

**Deployment status caveat:** "On `main`" above means the commit exists in the git history of the branch currently checked out on this server, which matches `origin/main` (clean working tree, zero divergence). Whether the *running Odoo process* has actually loaded the corresponding Python/XML changes (i.e., whether a module upgrade was run after each commit) was **not independently re-verified commit-by-commit** — only the overall module version in the DB (`19.0.11.2.0`) matching the manifest's current version was confirmed, which implies the code currently on disk has been loaded at least once since the last version bump.

---

# 10. Known Problems and Risks

| # | Issue | Symptom | Root cause | Evidence | Workaround | Recommended fix | Risk |
|---|---|---|---|---|---|---|---|
| 1 | **FBA Sellable quantity discrepancy** | Live Sellable quantity = 963, higher than the documented opening baseline of 944, despite 55 units already depleted to Sold | **Unknown — not determined in this pass** | Direct `stock_quant` sum query today vs. prior documentation's opening-baseline figure | None applied | Trace every stock move into/out of location 31 since the opening snapshot, reconcile against Amazon's actual current inventory via a fresh audit, before trusting Sellable for any new automation | **High** — could indicate either normal inbound-receiving behavior (benign) or a double-count/miscount (not benign); must be investigated, not assumed |
| 2 | **Anomalous database name** `amazon_prod12sep;` (literal semicolon) | A second, smaller (22 MB) database exists with a near-identical name | Likely an accidental `createdb` invocation where a trailing `;` was captured as part of the name | `psql -l` output today | None — not touched | Confirm with whoever created it whether it holds any needed data, then drop it deliberately (not as part of this read-only task) | Medium — confusing, could cause an operator to connect to the wrong DB by typo |
| 3 | **No fixed `db_name` in Odoo config** | `dbfilter=.*`, no `db_name` key | Server configured for multi-database serving rather than pinned to one DB | `/etc/odoo19.conf` read today | None | Decide and document which DB the public-facing Odoo URL actually serves; consider pinning `db_name` if only one DB should ever be reachable | Medium — operational ambiguity about what "production" means at the HTTP layer |
| 4 | **`workers = 0`** | Odoo runs single-process/threaded rather than prefork multi-worker | Configuration choice, possibly intentional for a smaller/dev-like deployment | `/etc/odoo19.conf` | None | Confirm with the team whether this is intentional for current load, or should be increased for production robustness | Low–Medium depending on actual traffic/load, not measured in this pass |
| 5 | **Undeclared external Python dependencies** (`requests`, `dateutil`) | Module manifest lists no `external_dependencies`; a fresh install could fail if these packages aren't already present | Manifest omission | `__manifest__.py` read today; imports confirmed in code | Packages happen to already be present in this environment | Add `external_dependencies: {'python': ['requests', 'python-dateutil']}` to the manifest | Low on this server (works today), Medium for portability/fresh deploys |
| 6 | **Dangerous write crons correctly disabled, but by configuration only, not by code removal** | `cron_amazon_export_stock`, `cron_amazon_update_prices`, `cron_amazon_full_sync`, `cron_amazon_master_scheduler` all inactive today | Intentional safety posture | `ir_cron` query today | Crons inactive | Any future module upgrade or manual toggle could re-enable these — now protected from upgrade resets by `noupdate` (Section 7), but **manual re-activation via UI is still possible by anyone with cron-edit access** | High if accidentally toggled — enforce via access rights review (Section 12), not just documentation |
| 7 | **Cron settings reset during upgrade** | Historical issue — upgrades used to reset `active`/`interval` to XML defaults | Pre-19.0.11.2.0, cron XML records were not `noupdate` | Confirmed resolved by commit `79b2e3a` and `migrations/19.0.11.2.0/pre-migrate.py`; tested by `test_cron_upgrade_safety.py` | N/A — fixed | Keep the regression test in the suite for every future upgrade | **Resolved**, listed for historical awareness only |
| 8 | **Zero settlements/returns/removals/reimbursements imported** | All financial/secondary-workflow tables empty | Crons intentionally inactive pending configuration/approval | DB counts today | None needed — expected state | Configure settlement accounting (Section 16) when ready | Not a bug — a scope/readiness gap |
| 9 | **Reimbursement matching cron running against empty data** | `cron_amazon_match_fba_reimbursements` executes daily, 43 successful no-op runs | Import crons for the data it would match against are inactive | `amazon_phase7_job` query today | None — harmless no-op | Either disable the matching cron until import is enabled, or leave as-is since it's a cheap no-op | Low |
| 10 | **Test suite not executed in this pass** | No pass/fail evidence gathered today | Out of scope for a read-only documentation task | N/A | N/A | Run the targeted test commands in Section 13 before any further development | Medium — unverified regression risk for any new change |
| 11 | **No live Amazon API success/failure log reviewed** | Cannot confirm whether recent API calls are succeeding cleanly beyond "cron ran on schedule" | Not inspected in this pass (would require reading `amazon_sync_log`/`amazon_api_operation_metric` content) | N/A | N/A | Query `amazon_sync_log` and `amazon_api_operation_metric` for recent entries before relying on "crons are active" as proof of healthy API traffic | Medium |
| 12 | **Unrelated module in same repo** (PostgreSQL Query Deluxe, latest commit) | Repo now contains two unrelated applications | Developer added a new, separate module in the same git history | `git log -1 --stat` today | None needed | Consider splitting into separate repos if these are genuinely unrelated products, to avoid confusing future contributors | Low (organizational, not functional) |
| 13 | **Static asset duplication** (`static/description/` vs `static/descriptionnn/`) | Dead/duplicate marketing assets in the module | Likely accidental folder rename/copy left behind | Directory listing today | None needed | Remove the unused `descriptionnn` folder if confirmed unreferenced | Low |

**Not confirmed either way in this pass (explicitly flagged, not claimed as risks or non-risks):** token expiration behavior, partial API response handling, multi-company access edge cases beyond the single confirmed `company_id=1` instance, database port mismatch (port 5432 confirmed, no mismatch observed), permissions/source-code protection beyond what's described in Section 12, and backup/restore integrity (backup *units exist* per prior docs, not independently re-verified today).

---

# 11. Production and Deployment Operations

All commands below are documented, not newly executed destructively in this pass (status/read commands were run; restart/upgrade commands were not run, per the read-only nature of this report).

```bash
# Odoo service
systemctl status odoo19 --no-pager
sudo systemctl restart odoo19
sudo systemctl stop odoo19
sudo systemctl start odoo19

# Odoo logs
tail -f /var/log/odoo19/odoo19.log
tail -200 /var/log/odoo19/odoo19.log
journalctl -u odoo19 -n 200 --no-pager
journalctl -u odoo19 -f

# PostgreSQL
systemctl status postgresql --no-pager
sudo systemctl restart postgresql
sudo -u postgres pg_lsclusters

# Nginx
systemctl status nginx --no-pager
sudo nginx -t
sudo systemctl reload nginx

# Repository update
cd /opt/odoo19/custom-addons/amazon-odoo
git status
git stash -u   # only if uncommitted work must be preserved first
git pull origin main
git stash pop  # only if something was stashed

# Module upgrade (stop service first to avoid partial-state issues)
sudo systemctl stop odoo19
/opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
  -c /etc/odoo19.conf \
  -u sdlc_amazon_connector \
  -d amazon_prod12sep \
  --stop-after-init
sudo systemctl start odoo19

# Clear Python bytecode cache if a stale .pyc is suspected
find /opt/odoo19/custom-addons/amazon-odoo -name "__pycache__" -exec rm -rf {} +

# Targeted tests (example — do not run against amazon_prod12sep)
/opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
  -c /etc/odoo19.conf \
  --test-enable \
  --test-tags sdlc_amazon_connector \
  -d test_cutover_v2 \
  --stop-after-init

# Cron inspection (read-only)
sudo -u odoo19 psql -d amazon_prod12sep -c \
  "SELECT id, active, interval_number, interval_type, cron_name FROM ir_cron WHERE id BETWEEN 24 AND 56 ORDER BY id;"

# Active ports
sudo ss -tlnp | grep -E '8069|5432|80|443'

# Backups (per existing docs — service/timer names not re-verified today)
systemctl status odoo19-backup.timer
systemctl list-timers | grep odoo

# Manual DB backup (read-only operation, safe)
sudo -u odoo19 pg_dump amazon_prod12sep > /opt/odoo19/backups/amazon_prod12sep_$(date +%Y%m%d_%H%M%S).sql
```

**Restoring a backup safely:** always restore into a **new, differently-named database** first (`createdb amazon_restore_test && pg_restore -d amazon_restore_test backup_file`), verify content, and only then consider pointing the Odoo config at it — never restore directly over `amazon_prod12sep` without a fresh pre-restore dump of the current state and explicit approval.

No secret values are included in any of the above.

---

# 12. Security and Access Control

| Item | Confirmed / documented |
|---|---|
| Linux user running Odoo | `odoo19` (confirmed via `systemctl status` showing the process, and via `sudo -u odoo19` pattern used for DB access) |
| Interactive shell user (this session) | `odoo-dev` |
| File ownership | Not independently re-verified via `ls -la` on the module directory in this pass — recommend a fresh check before assuming ownership matches the `odoo19` service user |
| Git safe.directory | Not re-verified in this pass; if git commands fail with an "unsafe repository" error for a given user, run `git config --global --add safe.directory /opt/odoo19/custom-addons/amazon-odoo` |
| SSH configuration | Not inspected in this pass |
| UFW rules | Not inspected in this pass |
| Fail2ban | Not inspected in this pass |
| PostgreSQL exposure | Confirmed listening on port 5432; whether it's bound to localhost only or network-exposed was **not re-verified today** (recommend `ss -tlnp | grep 5432` before assuming either way) |
| Odoo database manager exposure | `dbfilter=.*` with no `db_name` suggests the multi-database manager screen may be reachable; whether `list_db` is disabled in config was **not explicitly checked** — recommend verifying `list_db = False` is set if the database manager should not be publicly browsable |
| Secret storage | Credentials (`client_secret`, `aws_secret_key`, `refresh_token`, `ai_api_key`, `admin_passwd`, `db_password`) live in the `amazon_instance` table and `/etc/odoo19.conf` respectively — standard Odoo storage, not a dedicated secrets manager (e.g. Vault) as far as this inspection found |
| Backup protection | Not independently re-verified (file permissions on any backup directory not checked) |
| **Source-code protection limitation** | **Anyone with root access to this server can read the full module source code, the Odoo config file (including `admin_passwd`/`db_password`), and query the database directly for any stored credential value, regardless of Odoo-level user permissions or group restrictions.** Odoo's access control (groups, record rules, ACLs) only governs what application users can do *through the Odoo UI/API* — it provides no protection against anyone with OS-level root or direct database access. This is a structural limitation of the deployment model, not a bug to "fix" — it should simply be understood by anyone granted server access. |

---

# 13. Testing Strategy

- **Framework:** Odoo's `odoo.tests.common.TransactionCase`, standard for all 29 test files.
- **Test directory:** `sdlc_amazon_connector/tests/`
- **Naming convention:** `test_<feature_area>.py`, classes named `Test<FeatureArea>`.

**Full file list (confirmed today):** `test_cron_upgrade_safety.py`, `test_fba_appointment_slots.py`, `test_fba_auto_cutover.py`, `test_fba_customer_returns.py`, `test_fba_cutover_v2.py`, `test_fba_dispatch.py`, `test_fba_hold_durability.py`, `test_fba_inbound_plan_phase2.py`, `test_fba_packing_placement_phase3.py`, `test_fba_receiving_phase5.py`, `test_fba_reimbursements.py`, `test_fba_removal_disposal.py`, `test_fba_sale_stock.py`, `test_fba_shipping_phase4.py`, `test_fba_stock_structure.py`, `test_fbm_lifecycle.py`, `test_final_e2e.py`, `test_inventory_reconciliation_phase6.py`, `test_operations_phase65.py`, `test_order_import_cron_hardening.py`, `test_orders_api_v2026.py`, `test_orders_import_hardening.py`, `test_orders_vat_shipping.py`, `test_payout_clearing.py`, `test_peripheral_features.py`, `test_phase23_uat.py`, `test_phase24_diagnostic_safety.py`, `test_phase7_fba_events.py`, `test_settlement_accounting.py`, `test_settlement_payout.py`.

- **Targeted test command (single file):**
  ```bash
  /opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
    -c /etc/odoo19.conf --test-enable \
    --test-tags /sdlc_amazon_connector:TestFbaCutoverV2 \
    -d test_cutover_v2 --stop-after-init
  ```
- **Full module test command:**
  ```bash
  /opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
    -c /etc/odoo19.conf --test-enable \
    --test-tags sdlc_amazon_connector \
    -d test_cutover_v2 --stop-after-init
  ```
- **Required database setup:** Use `test_cutover_v2` or a freshly created database — **never run tests against `amazon_prod12sep` or `amazon_prod`.**
- **Mocked vs real API calls:** `test_fba_appointment_slots.py` includes a `_FakeResponse` helper mocking `requests.Response` — confirms at least some tests mock the HTTP layer rather than calling real Amazon endpoints. `test_final_e2e.py` is explicitly documented as "Rollback-only FBA and settlement scenario; no live Amazon calls." `test_peripheral_features.py` is documented as "Peripheral AI, VCS, rating, and MCF behavior with all providers mocked." No test file in this list was identified as intentionally calling the real/live Amazon API — all appear to be safe/mocked by design, but this was confirmed by docstring/naming, **not by reading every test body**.
- **Safe vs destructive:** All 29 tests use `TransactionCase`, which rolls back after each test by Odoo convention — inherently non-destructive to whatever database they run against, **provided they are run against a disposable test database, not production.**
- **Not performed in this pass:** actually executing the suite and recording pass/fail counts (see Section 8/10, Issue #10).

---

# 14. Current Business Workflow

| Step | Odoo Menu | Model | API calls | DB changes | Failure states | Prerequisites |
|---|---|---|---|---|---|---|
| 1. Configure Amazon instance | Amazon > Configuration > Instances | `amazon.instance` | None (config only) | Creates/updates instance record | Missing required fields | Odoo admin access |
| 2. Test connection | Instance form "Test Connection" | `amazon.instance` | Lightweight SP-API auth check | Updates connection-health field | Auth failure, network error | Valid LWA credentials on instance |
| 3. Configure marketplace and warehouse | Instance form (FBA Configuration tab) | `amazon.instance` | None | Sets `marketplace_id`, FBA location fields | Location not created yet | `stock.location` records must exist (9 FBA locations) |
| 4. Map products | Amazon > Catalog > Import/Map Products | `amazon.product`, wizard | Optional catalog read | Creates `amazon.product` rows linked to `product.product` | Unmapped SKU | Odoo products must already exist |
| 5. Import orders | Automatic (status sync) or manual via "Import Orders" button / import-by-ID wizard | `amazon.order.import.job`, `amazon.order.status.sync.job` | `getOrders`/`getOrder` | Creates job records | Rate limit, auth failure | Active instance, active crons 25/26 |
| 6. Process order jobs | Automatic (cron 25) | `amazon.order.import.job` | `getOrder` detail calls | Creates `amazon.sale.order`/`sale.order` | Job stuck/failed | Cron 25 active |
| 7. Synchronize order statuses | Automatic (cron 26) | `amazon.order.status.sync.job` | `getOrders` with `LastUpdatedAfter` | Updates order status | Job stuck/failed | Cron 26 active |
| 8. Create/update Odoo Sale Orders | Automatic, part of step 6 | `sale.order` | — | Creates/updates native SO | Missing product mapping | Product mapped (step 4) |
| 9. Process FBA inventory | Automatic (cron 27) | `amazon.fba.sale.stock.event` | None (internal) | Creates `stock.move`/`stock.picking` Sellable→Sold | `manual_review` state on anomaly | Cutover baseline present for pre-cutover orders |
| 10. Create inbound shipment | Amazon > FBA > Inbound Shipments | `amazon.inbound.shipment` | `createInboundPlan` | Creates shipment/plan record | Plan creation failure | Product mapped, FBA warehouse configured |
| 11. Generate packing | Inbound shipment form | `amazon.fba.packing.option/group` | `generatePackingOptions`/`setPackingInformation` | Creates packing records | Invalid box config | Shipment plan confirmed |
| 12. Generate transportation options | Inbound shipment form | `amazon.fba.transportation.option` | `generateTransportationOptions` | Creates option records | Missing Ready-to-Ship date | Packing confirmed |
| 13. Set ready-to-ship dates | **Transportation Ready-to-Ship wizard** | `amazon.fba.transportation.ready.to.ship.wizard(.line)` | None (collects input) | Writes `ready_to_ship_at` field | Date in the past / missing | Must run before step 12's API call succeeds |
| 14. Confirm transportation | Inbound shipment form | `amazon.fba.transportation.option` | `confirmTransportationOptions` | Updates transportation state | Confirmation failure, async poll needed | Option selected |
| 15. Confirm packing | Inbound shipment form | `amazon.fba.packing.option` | `confirmPackingOption` | Updates packing state | Confirmation failure | Packing generated |
| 16. Confirm shipment | Inbound shipment form | `amazon.inbound.shipment` | `confirmShipment` | Updates shipment state | Confirmation failure | Transportation + packing confirmed |
| 17. Receive inventory updates | Automatic (cron 36) | `amazon.inbound.shipment`, receiving extension models | Receiving evidence read | Updates stock locations (Transit→Received/Sellable) | Discrepancy detected → `amazon.fba.inventory.discrepancy` | Physical shipment dispatched |
| 18. Process settlements and returns | **Not currently enabled** — crons 30, 53 inactive | `amazon.settlement.report`, `amazon.return.report` | Would call settlement/returns report APIs | Would create records, draft journal entries | N/A — not running | Requires accounting configuration (Section 5) |

---

# 15. Open Questions

These must be answered before continuing development, in priority order:

1. **Which database is actually served to end users at the HTTP layer right now?** `/etc/odoo19.conf` has `dbfilter=.*` and no `db_name` — `amazon_prod12sep` was the database with live Amazon data, confirmed today, but the config file alone does not prove it is the one being served. Needs operational confirmation (e.g., check the actual URL/hostname routing, or ask the team directly).
2. **What explains the Sellable quantity being 963 today versus the documented 944 opening baseline minus 55 depleted (expected 889)?** This needs investigation before trusting FBA inventory numbers for any new automation (e.g., enabling stock export, which is currently and correctly disabled).
3. **What is `amazon_prod12sep;` (the semicolon-named database)?** Safe to drop, or does it hold anything needed?
4. **Is settlement accounting in scope for the next phase of work, and who (accountant/implementer) will configure the 15 NULL account/journal fields?**
5. **Are returns, removals, and inventory-adjustment/reimbursement imports approved to enable?** (Currently all inactive by design, not by missing code.)
6. **Is the FBM order pipeline intended to go live, or is FBA-only the permanent scope?** (Currently 0 active FBM crons.)
7. **Is any AI provider (`ai_api_key`) actually configured on the instance?** Not checked in this pass since it's a credential field.
8. **Is `workers = 0` intentional for the current/expected traffic level, or should it be increased?**
9. **Which tests, if any, were last actually executed and passed?** (This report did not run the suite — see Section 13.)
10. **Is the "PostgreSQL Query Deluxe" module (latest commit) meant to live in this same repository long-term, or should it be split out?**

---

# 16. Recommended Next Step

**Recommended single next step: Investigate and resolve the FBA Sellable-quantity discrepancy (Open Question #2 / Risk #1) before enabling any further automation or beginning settlement-accounting configuration.**

**Why this is next, not settlement configuration:** Settlement accounting is real, important, unfinished work — but it is purely additive and currently safely dormant (zero rows, all crons inactive). The Sellable-quantity discrepancy is different: it affects a number (**FBA Sellable = 963**) that the *already-active* `cron_amazon_process_fba_sale_stock_events` cron depends on every minute, and it is the exact kind of silent-drift issue that the Cutover system was built to prevent. Starting new work on top of an unexplained inventory number is higher risk than finishing a well-understood, currently-dormant accounting feature.

**Files to inspect (read-only first):**
- `sdlc_amazon_connector/models/amazon_fba_sale_stock.py` — re-read the exact depletion formula in full, line by line, to confirm current behavior matches documented intent.
- `sdlc_amazon_connector/models/amazon_fba_sale_stock_cutover.py` — confirm the Cutover Run's baseline total and activation state are unchanged since the original activation.
- `sdlc_amazon_connector/models/amazon_inventory_reconciliation.py` — check the 5 most recent `FBAAUDIT` run results in detail (not just their `state`) for any per-SKU mismatch already flagged by the system itself — this audit mechanism may have *already* detected and recorded the explanation.
- Database: `stock_move` records into/out of location 31 since the original opening-stock date, grouped by source document, to see what actually added the extra ~19+ units beyond the simple opening-minus-depleted arithmetic.

**Expected result:** A clear, evidence-based explanation for why Sellable = 963 (e.g., "X units were added via inbound receiving on date Y, which is expected and documented" vs. "there is a double-count bug in Z").

**Acceptance criteria:** The discrepancy is either (a) explained with a specific, cited stock-move/receiving event, and documented as expected behavior going forward, or (b) identified as a genuine defect with a specific root cause and a proposed code fix — not merely "re-run the audit and see if it still looks weird."

**What must not be changed while investigating:** Do not modify `amazon_fba_sale_stock_cutover_run`/`baseline` records. Do not re-seed opening inventory. Do not write to `stock.quant` directly. Do not call any Amazon write API. Do not toggle any currently-inactive cron. This is a read-only investigation until a specific, approved fix is identified.

---

# 17. PROMPT FOR THE NEXT AI CHAT

```
You are continuing work on an Amazon Egypt FBA connector for Odoo 19 Community.

PROJECT IDENTITY
- Module: sdlc_amazon_connector (custom Odoo module)
- Odoo: 19 Community, module version 19.0.11.2.0 (confirmed installed, matches manifest)
- Server: ionic-odoo-dev-01 (Ubuntu 24.04.4, Python 3.12.3, PostgreSQL 16.15 on port 5432)
- Repository: /opt/odoo19/custom-addons/amazon-odoo (git@github.com:Mohamed01097/amazon-odoo.git), branch main
- Odoo service: odoo19.service; config /etc/odoo19.conf (dbfilter=.*, NO fixed db_name — confirm which DB is actually served before assuming)
- Database inspected and confirmed to hold live data: amazon_prod12sep (231 MB). Other DBs exist (amazon_prod, test_cutover_v2, ahmed_elhosieny, and an anomalous "amazon_prod12sep;") but were not inspected for content.
- Amazon instance: id=6, "Amazon Egypt Production", seller A2LMV58FMUN2ZD, marketplace ARBP9OOSHTCHU, region eu.

CURRENT IMPLEMENTATION STATUS (confirmed via live DB query, not from memory or old docs)
- LIVE/ACTIVE: order import job processing, order status sync, FBA sale-stock event processing, inbound operation polling, inbound receiving sync, daily FBA inventory audits, operational health/monitoring crons (16 of 33 Amazon crons active).
- IMPLEMENTED BUT DORMANT (code exists, cron inactive, zero DB rows): settlements, payouts, returns, removals, FBA inventory adjustments, FBA reimbursements (only the "matching" cron is active and running as a no-op against zero data), product price push, stock export, full sync, master scheduler.
- 43 Amazon sale orders, 55 FBA sale-stock events (all done), 19 Amazon products (all mapped), 3,637 status-sync jobs (all done).
- All 15 settlement accounting fields on instance 6 are NULL — settlement accounting is unconfigured.

KNOWN ISSUE / CURRENT BLOCKER (read this first)
FBA Sellable location (id=31) currently holds 963 units. Older project documentation describes an opening baseline of 944 units with 55 units since depleted to the Sold location, which would predict 889 — but the live number is 963, not 889. This discrepancy has NOT been explained or resolved. Before doing any new work, investigate this using:
- sdlc_amazon_connector/models/amazon_fba_sale_stock.py (depletion logic)
- sdlc_amazon_connector/models/amazon_fba_sale_stock_cutover.py (baseline system)
- sdlc_amazon_connector/models/amazon_inventory_reconciliation.py (daily audits — check the 5 most recent FBAAUDIT runs in DETAIL, not just state)
- Direct stock_move query against location_id=31 since the opening-stock date.
Do not assume it's a bug or assume it's fine — determine the actual cause with evidence.

RELEVANT FILES
- models/amazon_instance.py (config, hosts most cron methods, 135 DB columns)
- models/amazon_fba_sale_stock.py (core depletion event model)
- models/amazon_fba_sale_stock_cutover.py (cutover baseline system)
- models/amazon_inventory_reconciliation.py (daily audits)
- models/amazon_inbound_shipping.py (transportation/Ready-to-Ship, largest file at 160KB)
- wizard/transportation_ready_to_ship_wizard.py
- data/cron.xml (33 cron definitions, all noupdate="1")
- migrations/19.0.11.2.0/pre-migrate.py (cron upgrade-safety protection)

RELEVANT MODELS
amazon.instance, amazon.product, amazon.sale.order(.line), amazon.order.import.job, amazon.order.status.sync.job, amazon.fba.sale.stock.event, amazon.fba.sale.stock.cutover.run/baseline/shipment, amazon.inventory.reconciliation.run, amazon.inbound.shipment(.line), amazon.inbound.operation.job, amazon.settlement.report(.line) [0 rows], amazon.payout [0 rows], amazon.return.report [0 rows], amazon.removal.order [0 rows], amazon.phase7.job [43 rows, all reimbursement_matching/done], amazon.fba.reimbursement [0 rows], amazon.fba.inventory.adjustment [0 rows].

RECOMMENDED NEXT TASK
Investigate the Sellable-quantity discrepancy above (read-only first). Only after it is explained or fixed with evidence should settlement-accounting configuration (15 NULL fields on instance 6, used by crons 30/53/54/55 which are all currently inactive) be the next major feature task.

TESTING REQUIREMENTS
- 29 test files exist under sdlc_amazon_connector/tests/, all TransactionCase-based. The suite was NOT executed as part of the handoff that produced this prompt — run it yourself before trusting "tests pass" as a starting assumption.
- NEVER run tests against amazon_prod12sep or amazon_prod. Use test_cutover_v2 or a fresh database.
- Command: /opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin -c /etc/odoo19.conf --test-enable --test-tags sdlc_amazon_connector -d test_cutover_v2 --stop-after-init

SECURITY CONSTRAINTS
- Never print or expose: client_secret, aws_secret_key, refresh_token, ai_api_key (columns on amazon_instance), admin_passwd, db_password (in /etc/odoo19.conf). Redact as [REDACTED] if ever surfaced.
- Never call Amazon write APIs (price push, stock export, full sync, master scheduler — crons 29/33/42/43, all correctly inactive) without explicit human approval.
- Never modify the Cutover baseline/run records, never re-seed opening stock, never write directly to stock.quant.
- Never enable a currently-inactive cron without understanding why it was disabled first.

INSTRUCTIONS
- Do NOT modify the unrelated "PostgreSQL Query Deluxe" module that was added to this same repository in the latest commit (e9a34e9) — it is out of scope.
- Do NOT invent or assume facts not confirmed by direct inspection — always re-verify current database/code state before acting, since prior documentation in docs/ contains outdated snapshots (e.g. an older "Sellable=889" figure that is now known to be wrong — today's live figure is 963, and even that may change by the time you read this, so re-verify again).
- Inspect current code and current database state before making any change.
- Confirm the current git status and working-tree cleanliness before starting.
```
