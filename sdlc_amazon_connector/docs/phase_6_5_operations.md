# Phase 6.5 Operations and Amazon Documentation Review

Review date: 2026-08-01

## Official sources reviewed

- SP-API Release Notes (latest visible entry: 2026-05-27)
- Fulfillment Inbound API v2024-03-20 reference, use-case guide, migration guide,
  FAQ, and rate-limit table
- Usage Plans and Rate Limits
- Optimize Rate Limits for Application Workloads
- Logging and Monitoring for Amazon SP-API Applications
- SP-API Health Dashboard announcement and official dashboard
- Notifications API notification-type catalogue
- Sellers API v1 `getMarketplaceParticipations`

## Endpoint and deprecation result

Fulfillment Inbound v2024-03-20 remains the current inbound version. The
connector's plan, packing, placement, transportation, tracking, and shipment
read operations use that version. Amazon's updated migration guide states that
the v0 read operations `getShipments`, `getShipmentItemsByShipmentId`,
`getShipmentItems`, `getLabels`, `getBillOfLading`, and `getPrepInstructions`
are preserved. Phase 5 uses only preserved shipment/item reads; it does not use
the removed v0 create or transportation operations.

The removed v0 operations include `createInboundShipmentPlan`,
`updateInboundShipment`, `createInboundShipment`, `getPreorderInfo`,
`confirmPreorder`, `getTransportDetails`, `putTransportDetails`,
`voidTransport`, `estimateTransport`, and `confirmTransport`. None is used by
the active inbound workflow.

Two out-of-scope legacy artifacts were identified and left inactive rather
than silently redesigned in this observability phase:

- The source still defines compatibility builders/constants for the removed
  XML listing feed types `POST_PRODUCT_PRICING_DATA` and
  `POST_INVENTORY_AVAILABILITY_DATA`. There is no active call site for those
  constants; active listing export uses `JSON_LISTINGS_FEED`.
- The settlement workflow now discovers Amazon-generated
  `GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE_V2` documents through Reports API
  `getReports`. The settlement report remains the payout source of truth;
  Finances API v2024-06-19 `listTransactions` is reserved for targeted future
  diagnostics and does not create a second financial ledger.

Amazon documents dynamic as well as standard usage plans. The connector must
therefore observe `x-amzn-RateLimit-Limit` when it is present and must not
assume one global rate. HTTP 429 is retryable. Retry is bounded, honors
`Retry-After`, and otherwise uses capped exponential backoff with jitter.
Per-operation 1-hour and 24-hour throttle counts, 24-hour throttle rate,
average retry delay, and the latest throttle time are stored by the scheduled
dashboard aggregation. Three or more throttles for one operation in an hour is
flagged as repeated throttling.

## Notification analysis

| Polling workflow | Official notification | Decision |
|---|---|---|
| FBA inventory reconciliation | `FBA_INVENTORY_AVAILABILITY_CHANGES` | Supported and potentially useful, but it requires an Amazon SQS destination, message verification, dead-letter handling, and notification-id idempotency. No such destination infrastructure exists in this module, so polling remains enabled. |
| Amazon order status | `ORDER_CHANGE` | Supported for important order changes. It can reduce polling after destination infrastructure is deployed; polling remains the fallback. |
| FBA inbound plan/packing/placement asynchronous status | None in the current notification catalogue | Continue `getInboundOperationStatus` polling. |
| FBA inbound shipment receiving/status | No inbound-shipment status notification is listed | Continue the documented shipment and shipment-item read polling. |
| Reports and feeds | `REPORT_PROCESSING_FINISHED` / `FEED_PROCESSING_FINISHED` | Applicable only if those business workflows later deploy notification infrastructure. |

No Notifications API subscription is created in Phase 6.5. This is deliberate:
there is no verified SQS/EventBridge destination, consumer verification,
dead-letter queue, or idempotent notification store in the current deployment.

## Health-check contract

The lightweight health check refreshes the Login with Amazon access token and
calls the read-only Sellers API v1 `getMarketplaceParticipations`. It does not
import products, orders, reports, shipments, or inventory and cannot create an
Amazon business operation.

The connector never scrapes the SP-API Health Dashboard. It exposes the
official `https://sellercentral.amazon.com/sp-api-status` link and classifies a
possible Amazon incident only as an inference when authorization is healthy
and multiple unrelated operations return recent 5xx responses.

## Operational safety

- Retry Center resumes the same durable source job; it never creates a second
  business job.
- An inbound POST workflow is retry-safe only when its Amazon `operationId` is
  already stored. Read/refresh jobs are resumable.
- Permanent validation, data, configuration, and authorization failures are
  routed to manual review. They are never automatically retried.
- Monitoring writes only logs, health metadata, retry scheduling, alerts, and
  activities. Dashboard page loads make no Amazon calls.
- Phase 6.5 contains no stock, picking, order, inbound-plan, packing,
  placement, shipment, receiving, accounting, or quantity mutation.
