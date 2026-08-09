# Amazon Settlement V2 Integration

Review date: 2026-08-10

## Official Amazon sources

- [Settlement report types](https://developer-docs.amazon.com/sp-api/docs/report-type-values-settlement)
- [Retrieve automatically generated reports](https://developer-docs.amazon.com/sp-api/docs/retrieve-automatically-generated-reports)
- [Reports API `getReports`](https://developer-docs.amazon.com/sp-api/reference/getreports)
- [Reports API](https://developer-docs.amazon.com/sp-api/docs/reports-api)
- [Finances API](https://developer-docs.amazon.com/sp-api/docs/finances-api)
- [Finances API `listTransactions`](https://developer-docs.amazon.com/sp-api/reference/listtransactions)
- [Settlement report removal announcement](https://developer-docs.amazon.com/sp-api/changelog/update-removal-of-xml-settlement-report-and-flat-file-settlement-report-date-changed-to-november-11-2026)

## Verified contract

The current source is `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2`.
It requires the Finance and Accounting role, is available to Seller Central
sellers, and is a tab-delimited flat file. Amazon generates settlement reports
automatically: they cannot be requested or scheduled, so the connector locates
completed documents with Reports API v2021-06-30 `getReports`.

The body fields currently documented by Amazon are:

`settlement-id`, `settlement-start-date`, `settlement-end-date`,
`deposit-date`, `total-amount`, `currency`, `transaction-type`, `order-id`,
`merchant-order-id`, `adjustment-id`, `shipment-id`, `marketplace-name`,
`amount-type`, `amount-description`, `amount`, `fulfillment-id`, `posted-date`,
`posted-date-time`, `order-item-code`, `merchant-order-item-id`,
`merchant-adjustment-item-id`, `sku`, `quantity-purchased`, and `promotion-id`.

Amazon deliberately condenses price and fee dimensions into `amount-type`,
`amount-description`, and signed `amount`. Local numeric formats are possible,
including comma decimals. The importer preserves all raw Amazon dimensions and
does not use a closed enum for Amazon descriptions.

`getReports` accepts up to ten report types, page sizes from 1 through 100,
and created-time bounds. Its default `createdSince` is 90 days before the
request, Amazon retains only the most recent 90 days of report metadata, and
continuation calls contain only the returned `nextToken`. This integration
uses a seven-day overlap bounded to that 90-day window. Body settlement IDs,
not Reports API report IDs, are the business keys.

The V2 report availability is Seller Central-wide and the November 2026
announcement says the migration applies to all marketplaces. Amazon Egypt is
therefore covered for an authorized Seller Central seller; currency is still
read per settlement and is never assumed to be EGP.

## Deprecation and Finances API boundary

Amazon will remove `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE` and
`GET_V2_SETTLEMENT_REPORT_DATA_XML` on November 11, 2026, in all marketplaces.
The connector does not create new logic around either deprecated type.

Finances API v2024-06-19 is current. `listTransactions` can return up to 500
transactions per page, warns that events can lag 48 hours, supports targeted
order or financial-event-group filtering, and uses a maximum 180-day time
window. It requires Finance and Accounting. It is intentionally not imported
as a second ledger here; it remains an optional future mismatch diagnostic.

## Safety boundary

Settlement import stores and reconciles Amazon financial data only. It creates
no journal entry, invoice, vendor bill, payment, bank record, or stock move.
Malformed or currency-inconsistent documents remain incomplete, and no
balancing line is fabricated.
