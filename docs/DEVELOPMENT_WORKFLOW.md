# Development Workflow

## Required Start of Work

Every development task must start with:

1. Run `git status`.
2. Confirm current phase and scope.
3. Read the relevant project docs.
4. Inspect only directly related files.
5. Identify files expected to change.
6. Preserve all existing user changes.

Do not inspect broad areas of Odoo core unless a specific Odoo API behavior is directly in scope.

## Phase-Based Workflow

All work must be divided into small phases. Do not combine unrelated features into a single implementation phase.

Each phase must include:

- One clear objective
- In-scope items
- Out-of-scope items
- Files to inspect
- Files expected to change
- Expected behavior
- Focused tests
- Regression checks
- Known limitations

## Phase Template

```md
## Phase Name

Objective:

In scope:

Out of scope:

Files to inspect:

Files expected to change:

Expected behavior:

Focused tests:

Regression checks:

Known limitations:
```

## Implementation Rules

- Extend the existing implementation.
- Use Odoo 19 Community-compatible patterns.
- Use the Odoo ORM.
- Use standard Odoo stock operations and stock moves for inventory effects.
- Use standard Odoo accounting APIs for accounting effects.
- Preserve existing records and backward compatibility.
- Respect multi-company and access-rights behavior.
- Preserve idempotency.
- Prevent duplicate imports and exports.
- Persist Amazon external IDs and asynchronous operation IDs.
- Track asynchronous job status.
- Poll feed/report results where applicable.
- Respect Amazon rate limits.
- Handle retries with bounded exponential backoff.
- Never expose credentials in logs, screenshots, documentation, prompts, or issue descriptions.
- Do not perform live stock, accounting, payout, or settlement writes during development validation.

## Forbidden Actions Without Explicit Approval

- `git reset`
- Destructive checkout or clean commands
- File deletion
- Commits
- Pushes
- Dependency installation
- Migrations
- Live Amazon API calls
- Stock export
- Price export
- Settlement import
- Accounting posting
- Payout reconciliation
- Production data edits

## Code Review Checklist

Before completing a code phase, check:

- Duplicate records
- Idempotency
- Retry behavior
- Cron reprocessing
- Race conditions
- Partial API failures
- Pagination
- Rate limits
- Missing mappings
- Incorrect states
- Currency and rounding
- Timezone handling
- Multi-company access
- Access rights and record rules
- Transaction boundaries
- Performance
- Logging and auditability
- Silent exception handling
- Amazon identifiers
- Asynchronous operations

## Final Report Requirements

Report:

- Files changed
- Tests executed
- Result of final diff review
- Known limitations
- Risks
- Required live/staging validation
- Any functionality that remains unverified

Never describe untested or unverified functionality as production ready.

