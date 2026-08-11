# Amazon payout and clearing reconciliation

Checked against current official Amazon documentation on 2026-08-11:

- [Settlement Reports](https://developer-docs.amazon.com/sp-api/docs/report-type-values-settlement): `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2` is an automatically generated Seller Central TSV report available with the Finance and Accounting role. Its header provides `settlement-id`, settlement dates, `deposit-date`, `total-amount`, and currency. The report is the expected settlement/payout source; `deposit-date` is not evidence that the seller's bank credited the funds.
- [Finances API](https://developer-docs.amazon.com/sp-api/docs/finances-api) and [listTransactions](https://developer-docs.amazon.com/sp-api/reference/listtransactions): v2024-06-19 exposes released/deferred seller financial transactions and related identifiers including `FINANCIAL_EVENT_GROUP_ID`. These identifiers can aid investigation, but the API is not imported as a second ledger by this feature.
- [Retrieve the amount and status of a payment](https://developer-docs.amazon.com/sp-api/docs/retrieve-amount-status-payment): legacy financial-event-group status can indicate that Amazon successfully sent a payment. It does not establish that the seller's bank physically credited it.
- [SP-API announcements](https://developer-docs.amazon.com/sp-api/changelog): the older XML/original flat settlement report types are scheduled for removal on 2026-11-11; new work remains on the V2 flat-file report. Finances v0 financial-event listing operations are being replaced by v2024-06-19 transaction operations.

## Implemented evidence policy

An actual receipt requires either an existing posted Odoo bank statement transaction or explicit manual confirmation with an actual bank receipt reference. Manual confirmation creates a draft bank-journal entry only. Amazon API dates/statuses never create or post a bank receipt.

Amazon Clearing reconciliation uses relationally linked settlement and receipt move lines and Odoo's standard `account.move.line.reconcile()` API. It never writes accounting residuals directly, creates a write-off, or uses SQL. Same-company, same-currency clearing is supported; cross-currency receipt reconciliation is deliberately blocked until a reviewed exchange-rate workflow exists.
