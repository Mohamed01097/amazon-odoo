# Decisions

This file records project-level decisions and guardrails. Add new decisions here when a phase makes an architectural or business-policy choice.

## Accepted Decisions

### Odoo Community Only

Use Odoo 19 Community-compatible patterns. Do not copy, port, or depend on Odoo Enterprise Amazon connector code.

### Odoo Is Stock and Price Master

Odoo is the master system for customer warehouse stock and approved selling prices. Amazon remains the evidence source for Amazon-side FBA states, fulfillment, reports, fees, reimbursements, settlements, and payout facts.

### FBA Stock Must Use Standard Odoo Stock Operations

Stock movement must use standard Odoo stock models, pickings, moves, and validations. Direct writes to `stock.quant` are forbidden.

### FBA Flow

The intended operational flow is:

```text
Odoo Warehouse
-> Amazon Transit
-> Amazon Receiving
-> Amazon Sellable / Reserved / Unsellable
```

Planning and Amazon option selection do not change stock. Physical dispatch and receiving evidence must be represented through standard Odoo stock records.

### Settlement Cutover Required

The system starts fresh after the agreed settlement cutover point. Legacy periods must not be rebooked by the connector.

### Draft-First Accounting

Settlement accounting entries must be draft-first and manually reviewed. The connector must not post accounting entries automatically during development validation.

### Bank Evidence Required for Payouts

Amazon settlement/deposit dates are not bank receipt proof. Payout clearing requires trusted Odoo bank evidence or an explicitly approved manual receipt reference.

### Official Amazon Sources Only

API-specific behavior must be verified against official Amazon SP-API documentation and release notes whenever relevant behavior may have changed.

### Phase-Based Delivery

All implementation work must be split into small phases. A phase must have one clear objective and must not combine unrelated features.

### FBA Sale Stock Cutover V2

Accepted 2026-09-15. The connector supports two cutover strategies:

- **V1 (purchase-date suppression):** Orders before `fba_sale_stock_cutover_at` are marked historical with `processed = cumulative`. No stock move. Simple but assumes all pre-cutover fulfillment is already accounted for.
- **V2 (historical fulfillment evidence baseline):** Uses `GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL` to compute B (units fulfilled before cutover) per order item. Delta formula: `D = max(0, C - B - P)`.

V2 hardening rules (non-negotiable):

1. B=0 is only proven safe when the order's purchase_date is within `[history_start_at, cutover_at)` and all report windows succeeded. Orders outside coverage get `manual_review` with `CUTOVER_BASELINE_OUTSIDE_COVERAGE`.
2. B > C is a data inconsistency — `manual_review` with `CUTOVER_BASELINE_EXCEEDS_CUMULATIVE`, never clamped.
3. Baselines are immutable after the run leaves `building` state.
4. `action_activate` only sets the cutover date on the instance. It does not seed inventory, import orders, enable crons, create stock moves, or call Amazon write APIs.
5. Shipment evidence is deduplicated by `shipment_item_id` (SQL UNIQUE per run).
6. No `env.cr.commit()` anywhere in the module.

## Pending Decisions

- Final settlement accounting strategy for Egypt go-live.
- Egypt VAT/tax handling and account mapping.
- Order import start date.
- Settlement cutover date.
- Partner matching/creation policy.
- Which crons to enable for the first production run.
- Whether price export can become scheduled after feed-result lifecycle validation.
- Whether any stock export is relevant for this FBA-first rollout.
- Whether adjustment events remain informational or can trigger reviewed stock moves.
- Whether notification infrastructure will be added later for report/feed/order/inventory events.

