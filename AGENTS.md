# Repository Instructions for Coding Agents

This repository is an Odoo 19 Community Amazon Egypt FBA connector. These instructions apply to Codex and any other AI coding agent working in this repository.

## Read First

Before making changes, read:

- `docs/PROJECT_CONTEXT.md`
- `docs/ARCHITECTURE.md`
- `docs/BUSINESS_REQUIREMENTS.md`
- `docs/DEVELOPMENT_WORKFLOW.md`
- `docs/TESTING_GUIDE.md`
- `docs/SECURITY_AND_SECRETS.md`
- `docs/CURRENT_STATUS.md`
- `docs/DECISIONS.md`

Also read existing handoff or phase documents when they overlap the task:

- `docs/AMAZON_FBA_IMPLEMENTER_HANDOFF.md`
- `docs/AMAZON_FBA_BUSINESS_FLOW_SUMMARY.md`
- `sdlc_amazon_connector/docs/`

## Non-Negotiable Rules

- Confirm the current phase and scope before changing files.
- Run `git status` before changes.
- Preserve user changes and unrelated dirty worktree state.
- Avoid unrelated modifications.
- Implement the smallest safe change.
- Run focused tests.
- Review the final diff.
- Report files changed, tests executed, limitations, and risks.
- Never expose secrets.
- Never claim unverified functionality as complete.

## Technical Boundaries

- Do not copy or depend on Odoo Enterprise code.
- Use Odoo 19 Community-compatible patterns.
- Use the Odoo ORM.
- Never write directly to `stock.quant`.
- Stock changes must use standard Odoo stock operations, pickings, and stock moves.
- Preserve existing records and backward compatibility.
- Respect multi-company and access-rights behavior.
- Preserve idempotency.
- Prevent duplicate imports and exports.
- Persist Amazon external IDs and asynchronous operation IDs.
- Track asynchronous job status and poll feed/report results where applicable.
- Respect Amazon rate limits and `Retry-After`.
- Handle retries with bounded exponential backoff.
- Never log or document credentials.
- Do not perform live stock, accounting, payout, or settlement writes during development validation.

## Git and File Safety

- Never run `git reset`.
- Never run destructive checkout, clean, or deletion commands unless the user explicitly requests the exact action.
- Never delete files without explicit approval.
- Never create commits or push changes unless explicitly requested.
- Do not install dependencies unless explicitly requested.
- Do not run migrations unless explicitly requested.
- Preserve existing documentation. If a requested doc already exists, improve it carefully and keep useful content.

## Amazon Validation Rule

Amazon API behavior must be verified against the latest official Amazon Selling Partner API documentation and release notes whenever relevant behavior may have changed. Use official Amazon sources only for API-specific requirements.

