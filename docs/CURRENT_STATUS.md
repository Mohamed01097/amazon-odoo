# Current Status

Review basis for this document:

- `git status`
- `README.md`
- `sdlc_amazon_connector/__manifest__.py`
- `sdlc_amazon_connector/models/__init__.py`
- `sdlc_amazon_connector/data/cron.xml`
- Existing root and module documentation under `docs/` and `sdlc_amazon_connector/docs/`
- Focused source searches for model, cron, retry, identifier, and test evidence

No Python code, XML, manifests, security files, dependencies, database data, live Amazon APIs, stock exports, price exports, settlement imports, migrations, commits, or pushes were changed in this documentation task.

## Verified Existing Functionality

- Main module exists at `sdlc_amazon_connector/`.
- Manifest version is `19.0.10.4.0`.
- Manifest declares Odoo Community modules only among dependencies: `sale_management`, `stock`, `contacts`, `account`, `mail`, `purchase`, and `delivery`.
- Source files exist for Amazon API, instance configuration, products, orders, order import jobs, order status jobs, settlements, payouts, returns, removals, inbound shipments, inbound operation jobs, inbound receiving, inventory reconciliation, FBA inventory, sync logs, operation controls, and AI features.
- Cron XML defines scheduled actions for order jobs, status sync, FBA sale stock events, product sync, price update, settlement import, cancellation checks, stock export, inbound polling, receiving, removal import, inventory audits, health, operations, retries, alerts, Phase 7 jobs, returns, adjustments, reimbursements, and reimbursement matching.
- Tests exist for FBA stock structure, dispatch, sale stock, inbound phases, receiving, inventory reconciliation, operations, Orders API behavior, returns, removals, reimbursements, settlements, payout clearing, and a mocked end-to-end flow.
- Source and existing docs show unique constraints or deterministic identities for several areas, including products by SKU/instance, Amazon orders by order/instance, inbound operations, physical shipments, returns, removals, reimbursements, settlements, settlement lines, and payouts.
- Existing docs state that current FBA stock changes should use standard Odoo pickings/moves and not direct `stock.quant` writes in the newer architecture.
- Existing docs state that Phase 7 API behavior was reviewed against official Amazon documentation as of August 2026.

## Partially Implemented Functionality

The following are present in code/docs but require staging/live validation, configuration, or business approval before production use:

- Amazon Egypt FBA setup.
- Product sync and SKU mapping.
- Order import and status sync.
- FBA inbound planning, packing, placement, shipping, labels, tracking, dispatch, and receiving.
- Inventory audits and reviewed reconciliation.
- FBA sale-stock event processing.
- Returns, removals, inventory adjustments, reimbursements, and settlement jobs.
- Settlement accounting and payout clearing.
- AI listing, pricing, forecast, review, alert, health, and chat features.

## Planned Functionality

Planned or required by project scope but not verified as production-ready in this documentation task:

- Production cron enablement after cutover approval.
- Live Amazon validation for Egypt account permissions, reports, feeds, rate limits, and marketplace behavior.
- Accountant-approved settlement/account/tax configuration.
- Production payout reconciliation process.
- Production-safe price push and stock export policies.
- Staging workshop for the next accounting/tax go-live gate.

## Missing Tests

Not verified:

- Whether the complete local test suite passes in this environment.
- Whether current tests cover every live Amazon report/feed edge case.
- Whether performance at 50 orders/day and larger history windows has been measured.
- Whether multi-company record rules are complete for every model.
- Whether all AI provider paths are covered by tests.

## Requires Live Validation

- Amazon Egypt credentials and roles.
- Marketplace participation and region configuration.
- Current Orders API behavior.
- Current FBA Inbound API behavior.
- Current Reports API behavior and report availability for Egypt.
- Current Feeds API behavior and processing reports.
- Current rate limits and throttling headers.
- Live settlement file structure from the target seller account.
- Live payout/bank reconciliation procedure in the client accounting setup.

## Open Risks

- Existing documentation conflicts exist between older root handoff/gap-analysis documents and newer `docs/` handoff/business-flow documents.
- Some crons are active in XML while production readiness still depends on configuration and cutover decisions.
- Price and stock export are live Amazon write areas and must remain disabled/manual until explicitly approved and feed-result handling is verified.
- Accounting strategy and Egypt tax policy require accountant approval.
- Partner creation/matching strategy requires client approval.
- Report/feed/API behavior may have changed after the last documented Amazon review dates and must be checked against official Amazon documentation before related implementation.

