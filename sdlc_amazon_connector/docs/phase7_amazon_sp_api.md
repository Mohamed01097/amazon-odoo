# Phase 7 Amazon SP-API contract

Reviewed: **2026-08-09**. Amazon behavior in Phase 7 is based only on the
official Amazon Selling Partner API documentation linked below.

## Official sources reviewed

- [SP-API release notes](https://developer-docs.amazon.com/sp-api/docs/sp-api-release-notes)
- [Fulfillment by Amazon report type values](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/report-type-values-fba)
- [Fulfillment by Amazon feed type values](https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/fulfillment-by-amazon-feed-type-values)
- [Reports API](https://developer-docs.amazon.com/sp-api/docs/reports-api)
- [Verify report processing](https://developer-docs.amazon.com/sp-api/docs/verify-that-report-processing-is-complete)
- [Feeds API](https://developer-docs.amazon.com/sp-api/docs/feeds-api)
- [Submit a feed](https://developer-docs.amazon.com/sp-api/docs/submit-a-feed)
- [Feeds API rate limits](https://developer-docs.amazon.com/sp-api/docs/feeds-api-rate-limits)
- [Retirement of legacy FBA inventory reports](https://developer-docs.amazon.com/sp-api/lang-es_ES/changelog/deprecation-notice-suppressed-listings-reports-and-fba-inventory-reports)

The latest release-note entry visible at review time was dated 2026-08-05.
The relevant recent platform change is the 2026-04-01 addition of the
`enableContentEncodingUrlHeader` option to report/feed document operations.
No later release note replaced the Phase 7 reports or removal feed below.

## Reports used

### `GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA`

- FBA sellers; requested report; daily data; tab-delimited flat file.
- Roles: **Amazon Fulfillment** or **Pricing**.
- Amazon does not list a marketplace restriction for this report (unlike
  neighboring reports that explicitly list regions). Egypt is therefore not
  separately named; unsupported-marketplace responses remain permanent
  configuration errors rather than retryable failures.
- Official columns consumed: `return-date`, `order-id`, `sku`, `asin`,
  `fnsku`, `product-name`, `quantity`, `fulfillment-center-id`,
  `detailed-disposition`, `reason`, `status`, `license-plate-number`, and
  `customer-comments`.
- The report does not expose a customer-return event ID or order-item ID.
  Connector idempotency therefore hashes documented stable columns and never
  uses row position. An exact duplicate physical event with every documented
  identity component equal cannot be distinguished by this source.
- The report is physical/logistical evidence, not a refund source and not an
  inventory-adjustment command. Return rows never create stock movements;
  reviewed FBA Inventory Reconciliation remains the inventory source of truth.

### `GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA`

- FBA sellers; requested report; near-real-time data; tab-delimited flat file.
- Roles: **Amazon Fulfillment** or **Pricing**.
- Official columns consumed: `request-date`, `order-id`, `order-type`,
  `service-speed`, `order-status`, `last-updated-date`, `sku`, `fnsku`,
  `disposition`, `requested-quantity`, `cancelled-quantity`,
  `disposed-quantity`, `shipped-quantity`, `in-process-quantity`,
  `removal-fee`, and `currency`.

### `GET_FBA_FULFILLMENT_REMOVAL_SHIPMENT_DETAIL_DATA`

- FBA sellers; requested report; near-real-time data; tab-delimited flat file.
- Roles: **Amazon Fulfillment** or **Pricing**.
- Official columns consumed: `request-date`, `order-id`, `shipment-date`,
  `sku`, `fnsku`, `disposition`, `shipped-quantity`, `carrier`,
  `tracking-number`, and `removal-order-type`.
- Amazon documents that cancelled/disposed units are not shipment rows. Their
  quantities come from the removal-order detail report.

`GET_FBA_RECOMMENDED_REMOVAL_DATA` is explicitly not used for removal status.
It contains recommendations, not the authoritative removal lifecycle.

### `GET_LEDGER_DETAIL_VIEW_DATA`

- FBA sellers; requested report; tab-delimited flat file.
- Role: **Amazon Fulfillment**.
- The request supplies `reportOptions: {"eventType": "Adjustments"}`.
- Official columns consumed: `Date`, `FNSKU`, `ASIN`, `MSKU`, `Title`,
  `EventType`, `ReferenceID`, `Quantity`, `FulfillmentCenter`, `Disposition`,
  `Reason`, `Country`, `ReconciledQuantity`, and `UnreconciledQuantity`.
- Ledger detail is available for the preceding **18 months**.
- It replaces `GET_FBA_FULFILLMENT_INVENTORY_ADJUSTMENTS_DATA`, which Amazon
  retired for all marketplaces on 2023-01-31. The legacy
  `GET_FBA_INVENTORY_ADJUSTMENTS_DATA` value previously present in this module
  was not a supported current report type.

### `GET_FBA_REIMBURSEMENTS_DATA`

- FBA sellers; requested report; daily data; tab-delimited flat file.
- Roles: **Amazon Fulfillment** or **Pricing**.
- Official columns consumed: `approval-date`, `reimbursement-id`, `case-id`,
  `amazon-order-id`, `reason`, `sku`, `fnsku`, `asin`, `product-name`,
  `condition`, `currency-unit`, `amount-per-unit`, `amount-total`,
  `quantity-reimbursed-cash`, `quantity-reimbursed-inventory`,
  `quantity-reimbursed-total`, `original-reimbursement-id`, and
  `original-reimbursement-type`.

## Feed used

### `POST_FLAT_FILE_FBA_CREATE_REMOVAL`

- FBA sellers; role: **Amazon Fulfillment**.
- Submitted through Feeds API `v2021-06-30` as
  `text/tab-separated-values; charset=UTF-8`.
- Official fields emitted: `MerchantRemovalOrderID`, `RemovalDisposition`,
  `MerchantSKU`, `SellableQuantity`, `UnsellableQuantity`, `AddressName`,
  `AddressFieldOne`, `AddressFieldTwo`, `AddressFieldThree`, `AddressCity`,
  `AddressCountryCode`, `AddressStateOrRegion`, `AddressPostalCode`,
  `ContactPhoneNumber`, and `ShippingNotes`.
- `RemovalDisposition` is `Return` or `Disposal`. Addresses are emitted only
  for Return, use configured partner data, and use ISO country codes.

The connector creates an input feed document, uploads it, creates the feed,
stores both IDs, and polls `getFeed`. `IN_QUEUE` and `IN_PROGRESS` are
non-terminal. `DONE`, `CANCELLED`, and `FATAL` are terminal, but `DONE` alone
does not establish row acceptance: the processing report is downloaded and
checked for Error/Fatal results before the removal order becomes Submitted.
Feed metadata is retained by Amazon for 28 days; presigned document URLs are
short lived (five minutes), so each is consumed immediately in its job turn.

## Reports API processing, availability, and limits

Reports use `createReport`, durable polling with `getReport`,
`getReportDocument`, and immediate download of the presigned URL. `IN_QUEUE`
and `IN_PROGRESS` are non-terminal; `DONE`, `CANCELLED`, and `FATAL` are
terminal. Amazon documents that `CANCELLED` can mean no data, which the
connector treats as an empty successful interval. Report metadata is retained
for 90 days. Generated report-document retention varies by report type under
Amazon's policy effective 2024-05-30, so documents are downloaded immediately;
the FBA report-type page does not publish a separate customer-return history
limit.

Amazon does not publish a report-specific maximum request interval for the
customer-return, removal-detail, removal-shipment, or reimbursement reports.
The connector therefore uses conservative 30-day windows with a two-day
overlap; this is an implementation choice, not an invented Amazon limit.
Idempotent event keys make the overlap safe. Ledger detail is additionally
bounded by Amazon's official 18-month availability.

At review time, Amazon documented a maximum creation frequency of once per 30
minutes for near-real-time FBA reports and once per four hours for daily FBA
reports. Default importing crons are disabled until configured; the durable
job processor and reimbursement matcher are enabled.

Relevant default usage plans reviewed (the response
`x-amzn-RateLimit-Limit` remains authoritative):

- Reports: `createReport` 0.0167 requests/second, burst 15;
  `getReport` 2/second, burst 15; `getReportDocument` 0.0167/second, burst 15.
- Feeds: `createFeed` 0.0083/second, burst 15;
  `createFeedDocument` 0.5/second, burst 15; `getFeed` 2/second, burst 15;
  `getFeeds` and `getFeedDocument` 0.0222/second, burst 10.

The shared API transport honors HTTP 429 `Retry-After`, captures Amazon
request/rate-limit headers, and uses exponential backoff. Report contents are
flat files and have no within-document pagination. Listing report/feed
metadata uses `nextToken` where applicable; Phase 7 creates one report per
persisted date window.

## Deliberate operational boundaries

- No undocumented REST endpoint is used for returns, removals, adjustments,
  or reimbursements.
- Unknown statuses, reason codes, and dispositions are stored as raw strings
  and sent to manual review.
- Customer-return rows are always inventory-audit only; the legacy return event
  movement option is disabled. Adjustment policies remain separate.
- Customer warehouse receipts are never validated from Amazon shipment data.
- No `account.move`, payment, journal entry, fee, payout, or bank
  reconciliation is created in Phase 7.
- Connector code never creates or writes `stock.quant`; all stock effects use
  standard Odoo pickings and moves.
