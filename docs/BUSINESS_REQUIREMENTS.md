# Business Requirements

## Target Rollout

- Odoo 19 Community
- Amazon Egypt marketplace
- FBA fulfillment
- Approximately 15 initial SKUs
- Approximately 50 orders per day
- One warehouse at initial setup
- Odoo is master for stock and selling prices.
- Fresh operational start after the agreed settlement cutover point.

## Functional Requirements

The connector must support:

- Amazon SP-API authentication and marketplace setup.
- Product and SKU synchronization.
- Mapping Amazon SKUs to Odoo products.
- Order import with duplicate prevention.
- Cancellation and removal checks.
- FBA inventory synchronization.
- Manual stock pull for reconciliation.
- Controlled stock export only where the fulfillment model supports it.
- Controlled price export from Odoo to Amazon.
- Manual price pull for reconciliation.
- FBA inbound planning, packing, placement, shipping, labels, tracking, dispatch, receiving, and inventory disposition review.
- Settlement import and reconciliation.
- Fees, commissions, refunds, FBA fees, reimbursements, expenses, adjustments, shipping, tax, promotions, and other settlement components.
- Returns and customer-return evidence.
- Removal orders and removal shipments.
- Sync logs, auditability, scheduled jobs, and retry handling.
- AI pricing, listing, forecast, review, alert, and analytics features where they do not override operational controls.

## Required FBA Stock Principles

The intended flow is:

```text
Odoo Warehouse
-> Amazon Transit
-> Amazon Receiving
-> Amazon Sellable / Reserved / Unsellable
```

Rules:

- Planning, packing, placement, transportation selection, label generation, and tracking entry do not change stock.
- Physical dispatch changes stock only through a standard Odoo picking from the source warehouse to Amazon Transit.
- Amazon receiving evidence moves only the positive unprocessed delta from Transit to Amazon Receiving/Staging.
- Amazon inventory disposition is reviewed before moving stock from Receiving/Staging into Sellable, Reserved, or Unsellable.
- FBA sale fulfillment must reduce Amazon Sellable, not the customer warehouse.
- Returns do not automatically increase customer warehouse stock.
- Removal shipment evidence may move reviewed FBA stock to Removal Transit.
- Customer warehouse stock increases from removals only after a standard Odoo receipt is validated from physically counted goods.
- Never write directly to `stock.quant`.

## Financial Requirements

- Settlement import must be idempotent.
- Settlement IDs and line identities must be persisted.
- Amazon reported net must be compared with locally calculated signed net.
- Unknown categories, parse failures, missing mappings, currency mismatch, and ambiguous order links must block accounting creation.
- Accounting entries must be draft-first and manually reviewed.
- Posting must remain a standard Odoo accounting action.
- Payout reconciliation must use actual bank evidence, not only Amazon settlement dates.
- No duplicate revenue, refund, reimbursement, fee, or clearing recognition is allowed across settlement-based and invoice-aware strategies.

## Business Decision Gates

These require explicit client/accountant/operations approval before production use:

- Settlement cutover date.
- Order import start date.
- Product/SKU mapping policy.
- Partner creation/matching policy.
- Price push policy.
- Stock export policy.
- FBA adjustment stock policy.
- Settlement-based versus invoice-aware accounting strategy.
- Egypt tax treatment, tax-inclusive behavior, and account mapping.
- Production cron enablement.
- Live Amazon write validation.

