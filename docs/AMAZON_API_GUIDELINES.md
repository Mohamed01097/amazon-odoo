# Amazon API Guidelines

## Official Documentation Rule

Amazon API behavior must be verified against the latest official Amazon Selling Partner API documentation and release notes whenever relevant API behavior may have changed.

Use official Amazon sources only for API-specific requirements, including:

- SP-API reference pages
- SP-API release notes
- SP-API report type pages
- SP-API feed type pages
- SP-API usage plan and rate-limit pages
- Official migration/deprecation notices

Do not rely on blog posts, third-party connector behavior, forum snippets, or copied Enterprise connector logic for API requirements.

## Amazon Egypt

The project target is Amazon Egypt. Marketplace, region, role, report, feed, and endpoint behavior must be verified against current official SP-API documentation before implementation or live validation. If a report/feed/API page does not explicitly mention Egypt, handle unsupported-marketplace responses as configuration or authorization errors and document the evidence.

## Authentication and Secrets

Use Login With Amazon and AWS signing according to official SP-API requirements. Never log:

- LWA credentials
- Refresh tokens
- AWS access keys
- AWS secret keys
- Client secrets
- Access tokens
- Authorization headers
- Request signatures

## Rate Limits and Retries

- Respect operation-specific usage plans.
- Use `x-amzn-RateLimit-Limit` when Amazon returns it.
- Honor `Retry-After` on HTTP 429.
- Use bounded exponential backoff for retryable 429, 5xx, timeout, and network failures.
- Do not retry permanent authorization, role, validation, schema, missing mapping, or business-rule failures as transient failures.
- Track retry count, last error, next run date, and request IDs.

## Pagination and Asynchronous Work

Persist and process:

- `nextToken` / pagination tokens
- Report IDs
- Report document IDs
- Feed document IDs
- Feed IDs
- Inbound operation IDs
- Amazon request IDs where useful for support and audit

Poll asynchronous work until terminal state where applicable. Terminal state does not always mean business acceptance; for example, feed completion must still inspect the processing result when Amazon provides one.

## Idempotency

Every imported Amazon object must have a stable key. Use Amazon IDs when available and deterministic composite keys when reports do not provide event IDs.

Repeated imports, overlapping report windows, cron reprocessing, worker restart, pagination continuation, and retry after failure must not duplicate:

- Products
- Orders
- Order lines
- Stock events
- Inbound operations
- Physical shipments
- Inventory audit lines
- Returns
- Removal orders
- Removal shipments
- Reimbursements
- Settlements
- Settlement lines
- Payout allocations

## Development Validation Boundary

During development validation:

- Do not call live Amazon APIs without explicit authorization.
- Do not submit live feeds.
- Do not export live stock.
- Do not export live prices.
- Do not request or import live settlement data unless explicitly authorized.
- Do not use production credentials in AI prompts, logs, screenshots, or tickets.

