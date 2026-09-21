# AI Continuation Prompt

Copy and paste everything below the line into a new Claude or ChatGPT session.

---

## CONTEXT PROMPT — Amazon Egypt FBA Odoo 19 Integration

You are continuing work on an Amazon Egypt FBA connector for Odoo 19 Community.

### Project Identity

- **Module:** `sdlc_amazon_connector` (custom Odoo module)
- **Odoo Version:** 19 Community (19.0.0 final)
- **Server:** `ionic-odoo-dev-01` (Ubuntu 24.04, Python 3.12)
- **Repository:** `/opt/odoo19/custom-addons/amazon-odoo` (git@github.com:Mohamed01097/amazon-odoo.git)
- **Production Database:** `amazon_prod12sep` (PostgreSQL 16)
- **Module Version:** 19.0.10.6.1
- **Odoo Config:** `/etc/odoo19.conf`
- **Odoo Service:** `odoo19.service` (systemd)
- **Odoo Venv:** `/opt/odoo19/venv/`
- **Logs:** `/var/log/odoo19/odoo19.log`
- **Amazon Instance:** id=6, Amazon Egypt Production, Marketplace ARBP9OOSHTCHU, Seller A2LMV58FMUN2ZD, EU region

### What Has Already Been Completed

1. **Full Amazon SP-API integration** — orders, products, inventory, settlements, returns, removals, reimbursements, inbound FBA, AI features
2. **FBA opening stock seeded** — 944 Sellable, 48 Reserved, 8 Unsellable, 1571 Transit from FBAAUDIT/00007 (2026-09-15 21:02:11 UTC)
3. **Cutover V2 activated** — Run 3 with 15,269 baselines covering 13,321 orders, total B=16,860. Prevents double-depletion of pre-cutover orders
4. **Live order pipeline** — Order Status Sync (cron 26) runs every 15 min, Event Processor (cron 27) every 1 min
5. **43 orders imported, 55 events processed** (all `done`), 889 Sellable as of 2026-09-20
6. **Reconciliation verified:** 944 − 55 = 889 MATCHES actual quant
7. **15 active crons, 18 inactive** (dangerous ones intentionally disabled)
8. **Implementer handoff documentation** created in English and Arabic

### What Has NOT Been Done

1. **Settlement Accounting** — ALL 15 account fields are NULL. No settlements imported. No journal entries created. Blocked on accountant.
2. **Egyptian Tax Configuration** — not configured
3. **Returns/Adjustments/Reimbursements import** — crons disabled, awaiting client approval
4. **Price Push / Stock Push** — disabled, must NEVER be enabled without explicit approval
5. **Product sync automation** — not decided

### Critical Invariants — DO NOT CHANGE

1. `auto_sync_enabled` MUST stay **False** on instance 6
2. `stock_push_interval` MUST stay **disabled**
3. `price_push_interval` MUST stay **disabled**
4. Crons 29 (prices), 33 (stock export), 42 (full sync), 43 (master scheduler) MUST stay **inactive**
5. Cutover Run 3 — do NOT rebuild, delete baselines, or change cutover_at timestamp
6. Opening stock (FBAAUDIT/00007) — do NOT re-seed
7. FBA location IDs 29-37 — do NOT change
8. API credentials — do NOT expose or modify without developer approval

### Cutover V2 — How It Works

The Cutover V2 system prevents double-depletion of historical orders. When a pre-cutover order is imported:

1. The system looks up B (baseline fulfilled quantity) for the order item
2. P (processed_fulfilled_qty) is initialized to B at event creation
3. During processing: D = C - P (where C is Amazon's cumulative fulfilled qty)
4. After processing: P = P + D
5. B is absorbed into P at creation — it is NEVER subtracted separately during processing

**The formula is D = C - P, NOT D = C - B - P.** B is used exactly once at initialization.

Source code proof: `amazon_fba_sale_stock.py` line 317: `values['processed_fulfilled_qty'] = baseline` and line 795: `delta = self.amazon_cumulative_fulfilled_qty - self.processed_fulfilled_qty`

### Documents To Read FIRST

Before making any modification, read these files in order:

1. `CLAUDE.md` — operating rules and safety constraints
2. `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md` — verified production state
3. `docs/IMPLEMENTER_HANDOFF.md` — complete functional guide (25 sections)
4. `docs/CURRENT_STATUS.md` — feature status overview
5. `docs/ACCOUNT_MIGRATION_MASTER_HANDOFF.md` — full technical handoff
6. `docs/OPERATIONS_RUNBOOK.md` — server operations and troubleshooting
7. `docs/DECISIONS.md` — architectural decisions

### Operating Rules

1. **Before making any modification, perform a read-only verification of the current production state.** Query the database to confirm current values. Do not rely on documentation alone.
2. **Never call Amazon write APIs** (stock push, price push, full sync) without explicit user approval
3. **Never modify stock** (no stock adjustments, no quant writes, no cutover changes) without explicit approval
4. **Never enable dangerous crons** (29, 33, 42, 43) — these write to Amazon
5. **Never modify Cutover V2** (Run 3, baselines, timestamps)
6. **Never expose secrets** (tokens, keys, passwords)
7. **Use small scoped changes** — one objective per phase
8. **Test before production** — use mocked/local validation
9. **Document every production action** — what was done, why, by whom
10. **Preserve backward compatibility** and idempotency
11. **Check git status before changes** — preserve uncommitted work

### Database Access

```bash
# Read-only queries on production:
sudo -u odoo19 psql -d amazon_prod12sep -c "SELECT ..."

# Check Odoo service:
systemctl status odoo19

# View logs:
tail -f /var/log/odoo19/odoo19.log

# Git state:
cd /opt/odoo19/custom-addons/amazon-odoo && git status
```

### Next Task

The primary remaining deliverable is **Settlement Accounting Configuration**. The implementer and accountant must:

1. Configure all 15+ account/journal fields on instance 6
2. Set the settlement cutoff date
3. Configure Egyptian tax treatment
4. Import one test settlement
5. Create and review a draft journal entry
6. Iterate until correct
7. Only then consider enabling settlement import automation

See `docs/IMPLEMENTER_HANDOFF.md` Sections 14-15 for the complete procedure.
