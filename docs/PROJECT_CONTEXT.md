# Project Context

## Project Summary

This repository contains the `sdlc_amazon_connector` module, an Odoo 19 Community connector for Amazon Selling Partner API workflows. The target rollout context is Amazon Egypt with FBA fulfillment.

Business context supplied for this project:

- Marketplace: Amazon Egypt
- Fulfillment model: FBA
- Initial catalog: approximately 15 SKUs
- Expected order volume: approximately 50 orders per day
- Initial warehouse setup: one customer warehouse
- Odoo is the master system for stock and approved selling prices.
- Amazon is the source for Amazon marketplace identifiers, FBA fulfillment evidence, FBA inventory disposition evidence, returns, removals, reimbursements, settlements, fees, commissions, refunds, expenses, and payout evidence.
- The system starts fresh after the agreed settlement cutover point.
- Settlement import and reconciliation are required.

## Main Module

Module path:

```text
sdlc_amazon_connector/
```

Verified from `sdlc_amazon_connector/__manifest__.py`:

- Module name: `Odoo Amazon Connector`
- Version: `19.0.10.4.0`
- License: `OPL-1`
- Declared dependencies: `sale_management`, `stock`, `contacts`, `account`, `mail`, `purchase`, `delivery`
- Assets include an Amazon dashboard JavaScript/XML frontend.

## Scope

The connector scope includes:

- Odoo 19 Community compatibility
- Amazon SP-API integration
- Amazon Egypt marketplace setup
- FBA workflow
- Product and SKU synchronization
- Order import
- Cancellation and removal checks
- Inventory synchronization
- Stock export
- Manual stock pull for reconciliation
- Price export
- Manual price pull for reconciliation
- FBA inventory
- Settlement import
- Fees, commissions, refunds, and expenses
- Returns
- Sync logs
- Scheduled jobs
- Retry handling
- AI pricing and analytics

## Source of Truth

- Odoo is the master for customer warehouse stock, product cost, UoM, and approved selling prices.
- Amazon is the source for Amazon order status, fulfillment evidence, FBA sellable/reserved/unsellable evidence, returns/removals/reimbursements, settlement financial lines, Amazon identifiers, feed IDs, report IDs, and operation IDs.
- Bank evidence in Odoo is the source for actual cash receipt. Amazon deposit dates alone are not proof that money reached the bank.

## Documentation Map

Root-level project documentation:

- `docs/PROJECT_CONTEXT.md`
- `docs/ARCHITECTURE.md`
- `docs/BUSINESS_REQUIREMENTS.md`
- `docs/DEVELOPMENT_WORKFLOW.md`
- `docs/TESTING_GUIDE.md`
- `docs/AMAZON_API_GUIDELINES.md`
- `docs/CURRENT_STATUS.md`
- `docs/DECISIONS.md`
- `docs/SECURITY_AND_SECRETS.md`
- `docs/CHANGELOG.md`

Existing detailed handoff and review documents:

- `docs/AMAZON_FBA_IMPLEMENTER_HANDOFF.md`
- `docs/AMAZON_FBA_BUSINESS_FLOW_SUMMARY.md`
- `sdlc_amazon_connector/docs/phase7_amazon_sp_api.md`
- `sdlc_amazon_connector/docs/phase_6_5_operations.md`
- `sdlc_amazon_connector/docs/settlement_v2_integration.md`
- `sdlc_amazon_connector/docs/settlement_accounting_posting.md`
- `sdlc_amazon_connector/docs/payout_clearing_reconciliation.md`
- `sdlc_amazon_connector/docs/inventory_fulfillment_gap_analysis.md`

When documents disagree, prefer the newer dated source only after verifying the related source code. Do not silently treat older gap analysis as current truth.

