# Amazon settlement accounting posting

## Detected accounting architecture

The connector is invoice-capable. `amazon.sale.order` links to Odoo sales
orders and invoices, and the instance settings can explicitly create and post
customer invoices when Amazon reports an order as shipped. Settlement
accounting therefore uses an invoice-aware hybrid strategy:

- financial components already recognized by a posted customer invoice or
  credit note are applied to that document's receivable account;
- components without a posted customer document use the configured settlement
  category account;
- every component is offset through Amazon Clearing, never Bank;
- the move remains draft for accounting review.

Draft customer documents, ambiguous receivable data, missing order links on
order sales/refunds, unknown categories, missing mappings, bank/cash mappings,
cross-company mappings, incomplete settlements, and payout differences block
entry creation.

## Entry direction

Amazon report signs are preserved. A positive settlement component credits its
category or receivable account and debits Amazon Clearing. A negative component
debits its category or receivable account and credits Amazon Clearing. The net
Amazon Clearing balance must equal Amazon's reported settlement payout using
the settlement currency precision.

The move date is the Amazon deposit date, falling back to settlement end date.
The same settlement cannot create a second move. No method calls
`account.move.action_post()` and no bank payout or bank reconciliation is
implemented in this phase.
