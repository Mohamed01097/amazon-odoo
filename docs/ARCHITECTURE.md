# Architecture

## High-Level Architecture

```text
Amazon Seller Central / SP-API
        |
        | read reports, orders, inventory, status
        | controlled writes for feeds/inbound/removal/price where explicitly approved
        v
sdlc_amazon_connector
        |
        | Odoo ORM models, jobs, logs, stock pickings, accounting drafts
        v
Odoo 19 Community
```

The connector must use Odoo 19 Community-compatible extension patterns. Do not copy, inspect for cloning, or depend on Odoo Enterprise Amazon connector code.

## Main Module Areas

Verified module folders include:

- `models/`: Amazon instances, API client, products, orders, settlements, payouts, returns, removals, inbound FBA, FBA inventory, sync logs, operation controls, AI features, and Odoo model extensions.
- `wizard/`: product import and setup wizards.
- `controllers/`: dashboard and AI chat controllers.
- `views/`: Odoo backend views and menus.
- `data/`: scheduled actions and sequences.
- `security/`: access definitions.
- `tests/`: Odoo transaction tests for FBA stock structure, inbound phases, receiving, reconciliation, operations, orders API, settlements, payouts, returns, removals, reimbursements, and end-to-end mocked flows.
- `static/`: dashboard assets and module description assets.
- `migrations/`: versioned post-migration scripts.

## Important Models

Verified from source filenames and imports:

- `amazon.instance`
- `amazon.api`
- `amazon.product`
- `amazon.sale.order`
- `amazon.sale.order.line`
- `amazon.order.import.job`
- `amazon.order.status.sync.job`
- `amazon.fba.sale.stock.event`
- `amazon.inbound.shipment`
- `amazon.inbound.operation.job`
- `amazon.inventory.reconciliation.run`
- `amazon.fba.inventory.report`
- `amazon.return.report`
- `amazon.removal.order`
- `amazon.settlement.report`
- `amazon.settlement.report.line`
- `amazon.payout`
- `amazon.sync.log`
- `amazon.phase7.job`

## FBA Stock Flow

The intended FBA stock flow is:

```text
Odoo Warehouse
-> Amazon Transit
-> Amazon Receiving
-> Amazon Sellable / Reserved / Unsellable
```

Stock must be changed through standard Odoo stock models. Use stock pickings, stock moves, and normal validation flows. Do not create, update, or correct stock by writing directly to `stock.quant`.

FBA sale depletion must not reduce `WH/Stock`. Amazon-fulfilled order evidence should reduce Amazon FBA Sellable through one idempotent owner and move stock to an Amazon FBA Sold / Customers location or equivalent standard Odoo customer flow.

## Product Flow

```text
Amazon listing
-> amazon.product keyed by instance + seller SKU
-> product.product linked by SKU / Internal Reference
```

SKU mapping must be deterministic. Duplicate Odoo product references or missing Amazon mappings must stop automatic processing and require review.

## Orders

Order import must preserve Amazon order IDs and item IDs. Repeated imports over an overlapping time window must update the same connector records and must not create duplicate Odoo sale orders or duplicate stock events.

For FBA orders, Amazon fulfillment evidence is the stock event source. For FBM workflows, any shipment confirmation to Amazon must be feed-result aware and idempotent before unattended production use.

## Settlements and Accounting

Settlement import must preserve Amazon report IDs, settlement IDs, row keys, categories, signed amounts, order links, reimbursement links, currency, and raw evidence needed for audit.

Accounting writes must be manual, draft-first, and reviewed. The connector must not post entries, reconcile payouts, or write off differences automatically during development validation.

## Scheduled Jobs and Retries

The module defines multiple crons for order import, order status sync, FBA sale stock events, inbound operation polling, receiving, inventory audits, operations, alerts, Phase 7 jobs, returns, adjustments, reimbursements, and settlements.

Job workers must:

- Persist cursors, external IDs, feed IDs, report IDs, operation IDs, retry counts, and `next_run_at`.
- Use row locking where concurrent workers may process the same logical work.
- Treat unknown outcomes after Amazon writes as manual review unless a durable Amazon ID proves the write can be resumed.
- Poll asynchronous feeds, reports, and inbound operations to terminal status where applicable.
- Respect Amazon rate limits and retry only bounded, classified retryable failures.

