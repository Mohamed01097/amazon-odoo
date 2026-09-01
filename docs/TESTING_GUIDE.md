# Testing Guide

## Testing Policy

Use focused testing only. Tests must be proportional to the changed behavior and must avoid live Amazon writes, production stock changes, production accounting writes, production payout reconciliation, and production settlement mutation during development validation.

Do not claim production readiness without staging and live validation evidence.

## Status Terminology

Use these exact terms:

- Implemented
- Partially implemented
- Source-code validated
- Locally tested
- Mock-tested
- Requires staging validation
- Requires live Amazon validation
- Not implemented

Avoid vague terms such as ready, done, complete, or production safe unless the validation evidence is stated.

## What to Test Per Change

Where relevant, test:

- Successful flow
- Duplicate execution
- Retry after failure
- Partial API failure
- Empty response
- Invalid response
- Missing mapping
- Cron re-execution
- Permissions
- Existing records
- Currency and rounding
- Large data volume

## Amazon Integration Tests

Prefer mocked Amazon API tests for development:

- Mock access tokens.
- Mock report creation, polling, and document download.
- Mock feed creation, upload, polling, and processing results.
- Mock paginated responses and duplicate tokens.
- Mock 429 responses with `Retry-After`.
- Mock 5xx and network failures.
- Confirm raw Amazon identifiers are persisted.
- Confirm retries resume durable work and do not create duplicate business records.

Live Amazon validation requires explicit authorization and must not be mixed into normal development tests.

## FBA Stock Tests

For stock-affecting changes, verify:

- No stock movement before physical dispatch.
- Dispatch uses a standard Odoo picking and stock moves.
- Receiving applies cumulative deltas only once.
- Inventory reconciliation does not blindly overwrite quantities.
- FBA sale depletion does not reduce `WH/Stock`.
- Repeated order/status import does not duplicate sale stock movement.
- Return evidence does not automatically increase `WH/Stock`.
- Removal shipment and removal receipt flows are separated.
- No code creates or writes `stock.quant` directly.

## Financial Tests

For settlement/accounting/payout changes, verify:

- Duplicate settlement import updates or reuses existing records.
- Signed amounts are preserved.
- Calculated net matches reported net before accounting is allowed.
- Unknown categories block accounting.
- Missing mappings block accounting.
- Currency precision and rounding are correct.
- One settlement creates at most one draft move.
- No move is posted automatically.
- Payout reconciliation uses trusted bank evidence.
- No automatic write-off is created.
- Cross-company and cross-currency cases are blocked or explicitly reviewed.

## Existing Test Areas

Verified test files exist for:

- FBA stock structure
- FBA dispatch
- FBA sale stock
- FBA inbound phases
- FBA shipping
- FBA receiving
- Inventory reconciliation
- Operations and retry controls
- Orders API behavior
- Phase 7 FBA events
- Customer returns
- Removal/disposal
- Reimbursements
- Settlement accounting
- Settlement payout
- Payout clearing
- Final mocked end-to-end flow

Not verified in this documentation task: whether the full suite currently passes in this local environment.

