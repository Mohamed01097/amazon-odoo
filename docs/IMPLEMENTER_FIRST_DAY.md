# Implementer First Day Guide

You are seeing this Amazon Egypt FBA integration for the first time. This guide gets you oriented in one day.

---

## Before You Start

**Read these rules. They protect live production data:**

1. DO NOT enable any additional automation today
2. DO NOT click "Export Stock", "Push Prices", or "Full Sync" buttons
3. DO NOT change any interval fields on the instance form
4. DO NOT modify Cutover V2, baselines, or cutover timestamps
5. DO NOT re-seed opening inventory

---

## Hour 1 — Understand the Integration

Read `docs/IMPLEMENTER_HANDOFF.md` Section 1 (Executive Overview).

Key facts:
- Client sells IONIC car care products on Amazon Egypt using FBA
- Amazon stores and ships products from its Egyptian warehouse
- Odoo tracks orders, inventory, and will eventually track settlements
- The connector reads from Amazon (safe) and can write (disabled for safety)

---

## Hour 2 — Navigate the Odoo Interface

Log into Odoo. Find the **Amazon** menu in the top navigation bar.

### Safe screens to explore:

| Menu | What You'll See |
|---|---|
| Amazon > Dashboard | Integration health overview |
| Amazon > Orders > All Orders | 43+ imported Amazon orders |
| Amazon > Orders > Import Jobs | 11 completed import jobs |
| Amazon > Orders > Status Sync Jobs | 400+ completed sync jobs |
| Amazon > FBA > FBA Sale Stock Events | 55+ processed events (all "done") |
| Amazon > FBA > Inventory Health | Daily audit comparisons |
| Amazon > Catalog > Products | 19 mapped IONIC products |
| Amazon > Alerts > Active Alerts | Current alerts (should be clear) |
| Amazon > Configuration > Instances | Amazon Egypt Production instance |

### On the Instance form, look but do not change:

| Tab | Status |
|---|---|
| Connection | Configured — API credentials are set |
| FBA Configuration | Configured — warehouse and 9 FBA locations |
| Sync Schedule | Partially configured — dangerous intervals set to "disabled" |
| Settlement / Accounting | **NOT configured** — this is your main task |

---

## Hour 3 — Verify the Live Pipeline

### Orders flowing?

Go to Amazon > Orders > Status Sync Jobs. You should see recent jobs (within the last 15 minutes) with state "done".

### Events processing?

Go to Amazon > FBA > FBA Sale Stock Events. Filter by state. All should be "done". Zero "pending", "manual_review", or "failed".

### Stock decreasing correctly?

Go to Inventory > Configuration > Locations. Filter for "Amazon". Check FBA Sellable (id=31) — the on-hand quantity should be less than 944 (the opening amount), decreasing as orders are fulfilled.

---

## Hour 4 — Understand Safety Controls

Read `docs/IMPLEMENTER_HANDOFF.md` Section 10 (Critical Safety Rules).

### Three things that must NEVER happen:

1. **Stock Push to Amazon** — disabled via `stock_push_interval=disabled` and cron 33 inactive
2. **Price Push to Amazon** — disabled via `price_push_interval=disabled` and cron 29 inactive
3. **Master Auto-Sync** — disabled via `auto_sync_enabled=False` and cron 43 inactive

### The Do-Not-Touch List (Section 20):

14 items that must not be changed without developer approval, including Cutover V2, FBA locations, interval settings, and API credentials.

---

## Hour 5 — Understand Cutover V2

Read `docs/IMPLEMENTER_HANDOFF.md` Section 7 (Cutover V2).

In one sentence: Cutover V2 prevents the system from double-counting historical fulfillments by initializing each pre-cutover event's processed quantity to the baseline value.

You don't need to understand the code. You need to know:
- It is working (validated with real data)
- Do NOT rebuild it, modify baselines, or change the cutover timestamp
- If you see a `manual_review` event, investigate before retrying — see Section 13

---

## Hour 6 — Plan Tomorrow

### Your next task: Settlement Accounting

Read `docs/IMPLEMENTER_HANDOFF.md` Section 14 (Accounting & Settlement Handoff).

Tomorrow, you should:
1. Meet with the accountant
2. Walk through the 18 configuration items together
3. Plan the Chart of Accounts for Amazon
4. DO NOT import settlements yet — just prepare

### Complete the Day 1 checklist:

See `docs/IMPLEMENTER_HANDOFF.md` Section 21 — tick off all Day 1 items.

---

## Quick Reference

| Need | Where |
|---|---|
| Full functional guide | `docs/IMPLEMENTER_HANDOFF.md` |
| Arabic version | `docs/IMPLEMENTER_HANDOFF_AR.md` |
| Current production status | `docs/CURRENT_STATUS.md` |
| Production snapshot (verified) | `docs/PRODUCTION_STATE_SNAPSHOT_2026-09-20.md` |
| Troubleshooting | `docs/OPERATIONS_RUNBOOK.md` Section 4 |
| Safety rules | `docs/IMPLEMENTER_HANDOFF.md` Sections 10 and 20 |
| Settlement setup procedure | `docs/IMPLEMENTER_HANDOFF.md` Sections 14-15 |
| Cron reference (active/inactive) | `docs/IMPLEMENTER_HANDOFF.md` Section 9 |

---

*Prepared 2026-09-20. For questions, contact the developer.*
