# Claude Code Instructions

This repository contains an Odoo 19 Community Amazon Egypt FBA connector. Claude Code must treat this as a production-adjacent ERP integration and must read the project documentation before making changes.

## Required Reading Before Changes

Read these files before editing code or data files:

- `docs/PROJECT_CONTEXT.md`
- `docs/ARCHITECTURE.md`
- `docs/BUSINESS_REQUIREMENTS.md`
- `docs/DEVELOPMENT_WORKFLOW.md`
- `docs/TESTING_GUIDE.md`
- `docs/SECURITY_AND_SECRETS.md`
- `docs/CURRENT_STATUS.md`
- `docs/DECISIONS.md`
- Existing handoff files under `docs/` and `sdlc_amazon_connector/docs/` when they are relevant to the requested phase.

## Operating Rules

- Confirm the current phase and exact scope before making changes.
- Run `git status` before changes and preserve all user changes.
- Inspect only directly related files. Avoid broad scans of Odoo core.
- Do not copy or depend on Odoo Enterprise code.
- Extend the existing implementation using Odoo 19 Community-compatible patterns.
- Use the Odoo ORM and standard stock/accounting APIs.
- Never write directly to `stock.quant`.
- Never run `git reset`.
- Never run destructive checkout or clean commands.
- Never delete files without explicit approval.
- Never create commits or push changes unless explicitly requested.
- Never modify production data.
- Never call live Amazon APIs unless the user explicitly authorizes that live validation.
- Never export stock, export prices, import settlements, post accounting, or reconcile payouts during development validation.
- Never print passwords, tokens, private keys, AWS keys, LWA credentials, database credentials, or API secrets.
- Never claim unverified functionality as complete.

## Development Method

Implement the smallest safe change that satisfies the current phase. Do not combine unrelated features into one phase. Preserve backward compatibility, idempotency, multi-company behavior, access rights, external Amazon identifiers, asynchronous operation IDs, and retry state.

Every phase must define:

- One clear objective
- In-scope items
- Out-of-scope items
- Files to inspect
- Files expected to change
- Expected behavior
- Focused tests
- Regression checks
- Known limitations

## Verification

Run focused tests proportional to the change. Prefer mocked/local validation for Amazon interactions. Do not call live Amazon or mutate live stock/accounting/payout data as a substitute for tests.

Before reporting completion:

- Review the final diff.
- Confirm no unrelated files changed.
- Confirm no secrets were exposed.
- Report files changed.
- Report tests executed and their result.
- Report limitations, unverified behavior, and remaining risks.

