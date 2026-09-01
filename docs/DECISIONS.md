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

