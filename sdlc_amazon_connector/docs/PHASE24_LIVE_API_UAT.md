# Phase 24 — Controlled Live Amazon SP-API UAT

**Module:** `sdlc_amazon_connector` v19.0.11.2.0
**Date:** 2026-10-11 (final verification pass)
**Phase:** 24A — Read-Only Preparation (Preflight Report with Security Hardening)
**Status:** AWAITING OPERATOR APPROVAL FOR PHASE 24B

---

## 1. Environment Verification

| Item | Expected | Verified | Status |
|---|---|---|---|
| Git branch | main | main @ `2b3abad` | PASS |
| Module version | 19.0.11.2.0 | 19.0.11.2.0 | PASS |
| Working tree | Clean (no staged changes) | Clean (only untracked docs/zip) | PASS |
| Docker PostgreSQL | `127.0.0.1:5434` | `amazon-staging-pg` Up, accepting connections | PASS |
| Staging database | `amazon_phase19_fresh` | Present, accessible as `odoo_stg` | PASS |
| Staging config | `odoo19_staging.conf` | Points to Docker PG `5434`, port `8069` | PASS |
| Shared Odoo | Port `8019` | Running (port in use), NOT modified | PASS |
| Protected DB | `amazon24sep` | NOT accessed, NOT modified | PASS |
| Staging Odoo process | Not running | Not running (no process on 8069) | PASS |
| Phase 23 UAT | 46 tests, 0 failures | 46 tests, 0 failures, 0 errors (114s) | PASS |
| Amazon instances in staging | 0 | 0 (no credentials present) | PASS |
| Queued background jobs | 0 | 0 across all job tables | PASS |

---

## 2. Complete Cron Audit (33 crons)

### Classification Methodology

Each cron was traced to its Python method and classified by whether its call chain can invoke Amazon HTTP requests, and whether it creates or modifies Odoo business records (SO, stock moves, accounting entries).

### Final Cron States After Security Hardening

**5 ACTIVE (LOCAL-only, safe):**

| Cron | Method | Classification | Reason safe |
|---|---|---|---|
| Clean Successful Operational Logs | `cron_cleanup_operational_records` | LOCAL | Deletes old `amazon.sync.log` records |
| Detect Stuck Jobs | `cron_detect_stuck_jobs` | LOCAL | Updates `amazon.operation.control` states |
| Evaluate Operational Alerts | `cron_evaluate_operational_alerts` | LOCAL | Reads existing DB records, creates alerts |
| Match FBA Reimbursements | `cron_match_fba_reimbursements` | LOCAL | Enqueues Phase7 `reimbursement_matching` job (local match only) |
| Refresh Operations Dashboard | `cron_refresh_operations_dashboard` | LOCAL | Aggregates existing DB records |

**10 DISABLED (API-capable, deactivated in staging):**

| Cron | XML ID | Classification | Why disabled |
|---|---|---|---|
| Check Connection Health | `cron_amazon_connection_health` | API-READ | Calls `get_access_token` + `get_marketplace_participations` |
| Dispatch Eligible Retries | `cron_amazon_dispatch_operational_retries` | API-MIXED | Re-queues source jobs → triggers API calls via other crons |
| Enqueue Daily FBA Inventory Audits | `cron_amazon_enqueue_inventory_audits` | API-INDIRECT | Creates `reconciliation.run` → processed by API-calling cron |
| Poll Inbound Operations | `cron_amazon_process_inbound_operation_jobs` | API-READ+MUTATE | Calls `get_inbound_operation_status` → updates Odoo records |
| Process FBA Inventory Audits | `cron_amazon_process_inventory_audits` | API-READ+MUTATE | Calls `get_all_inventory_summaries` → creates stock moves |
| Process FBA Sale Stock Events | `cron_amazon_process_fba_sale_stock_events` | LOCAL+MUTATE | Processes event queue → creates stock moves/pickings |
| Process Order Import Jobs | `cron_amazon_process_order_import_jobs` | API-READ+MUTATE | Calls `get_orders` → creates SO, stock moves |
| Process Removal Orders and FBA Events | `cron_amazon_process_phase7_jobs` | API-WRITE+MUTATE | Calls `create_report` (POST) → creates Odoo records |
| Sync Order Statuses | `cron_amazon_sync_order_statuses` | API-READ+MUTATE | Calls `get_orders` → updates order states |
| Synchronize Inbound Receiving | `cron_amazon_sync_inbound_receiving` | API-INDIRECT | Enqueues receiving sync jobs → API calls |

**18 DISABLED (already inactive before hardening):**

Calculate Product Health Scores, Check Canceled Orders, Enqueue Inventory Audits (Compatibility), Export Stock Levels, Full Bidirectional Sync, Import All Orders (FBM + FBA), Import FBA Customer Returns, Import FBA Inventory Adjustments, Import FBA Reimbursements, Import FBM Orders, Import Settlement Reports, Master Auto-Sync Scheduler, Pull Prices, Refresh Removal Status, Smart Alert Scan, Sync Products, Update FBM Order Status, Update Product Prices.

**Verification method:** Direct SQL query against Docker PG staging database. No crons were modified on the shared server or `amazon24sep`.

---

## 3. API Endpoint Inventory

### 3.1 Authentication

| Method | Endpoint | HTTP | R/W on Amazon | Odoo side effects |
|---|---|---|---|---|
| `get_access_token` | `https://api.amazon.com/auth/o2/token` | POST | Read (token request) | None |
| `action_test_connection` | Calls `get_access_token` only | POST | Read | None (sanitizes whitespace) |

### 3.2 Read-Only Amazon API Methods (31 GET methods)

| Method | Endpoint | SP-API Permission | Odoo side effects via connector |
|---|---|---|---|
| `get_marketplace_participations` | `/sellers/v1/marketplaceParticipations` | Sellers | None |
| `get_orders` | `/orders/2026-01-01/orders` | Orders | **Caller may create SO** |
| `get_order` | `/orders/2026-01-01/orders/{id}` | Orders | **Caller may create SO** |
| `get_inventory_summaries` | `/fba/inventory/v1/summaries` | FBA Inventory | None |
| `search_catalog_items` | `/catalog/2022-04-01/catalogItems` | Catalog | None |
| `get_settlement_reports_list` | `/reports/2021-06-30/reports` (GET) | Reports | None |
| + 25 additional read methods | Various inbound, pricing, feed status | Various | None |

### 3.3 Write Amazon API Methods (22 POST/PUT/PATCH — ALL BLOCKED)

All methods using POST, PUT, or PATCH to SP-API endpoints are blocked by the diagnostic script's `RequestGuard`. This includes: `create_report`, `create_feed_document`, `upload_feed_document`, `create_feed`, `put_listings_item`, `patch_listings_item`, `create_fulfillment_order`, `cancel_fulfillment_order`, `create_inbound_plan`, and 13 inbound plan operations.

---

## 4. Diagnostic Script Security Review

### 4.1 Security Issues Found and Fixed

| Issue | Severity | Resolution |
|---|---|---|
| Endpoint allowlist defined but never enforced | CRITICAL | `RequestGuard` now monkey-patches `requests.request` at HTTP boundary |
| `max_retries=0` not passed to API calls | HIGH | Guard counts every HTTP request including retries — budget enforcement at wire level |
| `BLOCKED_METHODS` defined but not enforced | HIGH | Guard rejects non-GET SP-API calls before they reach the network |
| Cron safety check incomplete (only 6 of 10) | MEDIUM | `DANGEROUS_CRONS` list expanded to all 10 API-capable crons |
| Script continues after wrong marketplace | MEDIUM | Now aborts immediately if Egypt marketplace not found |
| Request budget ignores retries | MEDIUM | Budget tracks real HTTP requests, not logical API calls |

### 4.2 Enforcement Architecture

```
Diagnostic Script
    ↓ calls AmazonAPI methods
        ↓ calls _amazon_request()
            ↓ calls requests.request()  ← MONKEY-PATCHED
                ↓ RequestGuard.check()
                    ├── Verify URL is in ALLOWED_URL_PATTERNS
                    ├── Verify HTTP method is allowed (GET for SP-API, POST for OAuth only)
                    ├── Increment request_count
                    ├── If count > MAX_TOTAL_REQUESTS → raise BudgetExhausted
                    ├── If URL not in allowlist → raise EndpointBlocked
                    └── If blocked method → raise EndpointBlocked
```

### 4.3 Verified Safety Properties

| Property | Enforcement mechanism | Test evidence |
|---|---|---|
| Only allowlisted endpoints execute | `RequestGuard.check()` at `requests.request` boundary | Tests 01-04, 22-23 (allowed), Tests 05-13, 20-21, 25 (blocked) |
| All write endpoints blocked | Method check rejects POST/PUT/PATCH/DELETE for SP-API | Tests 05-11, 20-21 |
| Unknown endpoints fail closed | URL must match `ALLOWED_URL_PATTERNS` | Test 12 |
| OAuth only accepts POST | Method check specific to OAuth URL | Test 04 (POST allowed), Test 13 (GET blocked) |
| Budget enforced globally | Counter incremented per real HTTP request | Test 14 (3-request budget exhausts at 4th) |
| Retries counted toward budget | Guard sits below `_amazon_request` retry loop | Each retry calls `requests.request` → guard |
| Token values redacted | `redact()` function masks all but first 4 chars | Test 18 |
| URL query params redacted | `_safe_url()` redacts token/secret query params | Test 19 |
| Wrong marketplace aborts | Pre-flight check before any API call | Script logic verified |
| Wrong seller identity aborts | Instance marketplace must match `ARBP9OOSHTCHU` | Script logic verified |
| No Odoo business records created | Script never calls `_import_one_order` or equivalent | Code review verified |
| No crons activated | Script checks cron state, does not modify it | Script logic verified |
| S3 upload blocked | S3 URLs don't match allowlist | Test 25 |
| Guard is installed and removed | Monkey-patch lifecycle with try/finally | Tests 16-17 |

### 4.4 Safety Test Results

**27 tests, 0 failures, 0 errors** — all enforcement mechanisms verified.

### 4.5 Request Budget Analysis

The budget of 14 tracks **real HTTP requests** (at the `requests.request` boundary), not logical API calls. With `_amazon_request`'s default `MAX_RETRIES=3`, a single logical call could consume up to 4 budget units (1 initial + 3 retries). This means:

- Best case (no retries): 14 logical API calls
- Worst case (every call retries 3x): 3-4 logical calls before budget exhaustion
- Expected case (no 429/5xx errors): 14 logical calls

The guard raises `BudgetExhausted` at the 15th request, hard-stopping all further API traffic.

---

## 5. Credential Security Findings

### 5.1 Field-Level Analysis

| Field | Type | Storage | View restriction | ACL restriction |
|---|---|---|---|---|
| `refresh_token` | `fields.Text` | Plaintext in DB | `password="True"` + `group_amazon_technical_admin` | Read: all amazon groups; Write: manager+ |
| `client_id` | `fields.Char` | Plaintext in DB | No masking | Read: all amazon groups; Write: manager+ |
| `client_secret` | `fields.Char` | Plaintext in DB | `group_amazon_technical_admin` | Read: all amazon groups; Write: manager+ |
| `aws_access_key` | `fields.Char` | Plaintext in DB | Visible | Read: all amazon groups; Write: manager+ |
| `aws_secret_key` | `fields.Char` | Plaintext in DB | `group_amazon_technical_admin` | Read: all amazon groups; Write: manager+ |
| `seller_id` | `fields.Char` | Plaintext in DB | Visible | Read: all amazon groups; Write: manager+ |
| `marketplace_id` | `fields.Char` | Plaintext in DB | Visible | Read: all amazon groups; Write: manager+ |

### 5.2 Known Risks (Plaintext at Rest)

| Risk | Severity | Mitigation |
|---|---|---|
| Credentials readable via SQL on staging DB | MEDIUM | Docker PG isolation; no other users; transient credential window |
| Credentials in DB backup | MEDIUM | Do NOT backup staging DB while credentials present |
| Credentials readable by any Amazon user via ORM | MEDIUM | Only operator has staging access |
| Credentials in Odoo RPC responses | LOW | View-level `groups` attribute restricts UI display |

### 5.3 API Logger Redaction

The `AmazonAPI._sanitize_for_log()` and `_sanitize_headers()` methods redact all keys matching `SENSITIVE_KEYS`:
`access_token`, `authorization`, `client_secret`, `lwa_access_token`, `password`, `refresh_token`, `secret`, `signature`, `token`, `x-amz-access-token`, `x-amz-security-token`

Verified in `amazon_api.py:25-138`.

### 5.4 Credential Provisioning Procedure (Recommended)

**Operator Direct Entry via Staging Odoo UI:**

1. Developer starts staging Odoo: `./venv/bin/python odoo-bin -c odoo19_staging.conf`
2. Operator navigates to Amazon > Configuration > Instances.
3. Operator creates new instance with:
   - Seller ID, Marketplace ID (`ARBP9OOSHTCHU`), Region (`eu`)
   - Client ID, Client Secret, Refresh Token (entered manually, NOT copied from `amazon24sep` DB)
4. Operator clicks "Test Connection" to verify OAuth.
5. Developer stops Odoo, runs diagnostic via `odoo-bin shell`.
6. After diagnostics complete, operator deletes the instance via UI or SQL.
7. Credentials persist in DB only during the diagnostic window (minutes).

### 5.5 Credential Cleanup Procedure

After Phase 24B completes:

```sql
-- Run on Docker PG (port 5434) ONLY
DELETE FROM amazon_instance WHERE marketplace_id = 'ARBP9OOSHTCHU';
-- Verify:
SELECT count(*) FROM amazon_instance WHERE refresh_token IS NOT NULL;
-- Expected: 0
```

---

## 6. Live UAT Execution Plan (Phase 24B — REQUIRES APPROVAL)

### Test 1: Authentication
- **Endpoint:** `https://api.amazon.com/auth/o2/token` (POST)
- **Budget:** 1-2 requests
- **Expected:** Valid access token
- **Failure:** Abort all remaining tests

### Test 2: Marketplace Identity
- **Endpoint:** `/sellers/v1/marketplaceParticipations` (GET)
- **Budget:** 1 request
- **Expected:** Egypt marketplace `ARBP9OOSHTCHU` in response
- **Failure:** Abort — wrong seller/marketplace

### Test 3: FBA Orders (Read-Only, No Import)
- **Endpoint:** `/orders/2026-01-01/orders` (GET, `fulfilledBy=AMAZON`)
- **Budget:** 1-3 requests (pagination)
- **Date range:** Last 7 days
- **Expected:** Order list with IDs, statuses, amounts
- **Side effects:** NONE — script does not call `_import_one_order`

### Test 4: FBA Inventory
- **Endpoint:** `/fba/inventory/v1/summaries` (GET)
- **Budget:** 1-5 requests (pagination)
- **Expected:** SKU/ASIN list with quantities
- **Side effects:** NONE

### Test 5: Catalog Sample
- **Endpoint:** `/catalog/2022-04-01/catalogItems` (GET)
- **Budget:** 1 request
- **Expected:** Catalog items or 403 (non-critical)

### Test 6: Settlement Report Listing
- **Endpoint:** `/reports/2021-06-30/reports` (GET, list only)
- **Budget:** 1 request
- **Expected:** List of existing settlement reports (no creation)

**Total budget: 14 real HTTP requests (including any retries)**

---

## 7. Safety Controls Checklist

| # | Control | Status |
|---|---|---|
| 1 | Explicit operator approval | ☐ PENDING |
| 2 | Marketplace identity verified in script | ✓ HARDCODED `ARBP9OOSHTCHU` |
| 3 | Endpoint allowlist enforced at HTTP boundary | ✓ `RequestGuard` monkey-patches `requests.request` |
| 4 | Request budget enforced globally | ✓ 14-request hard limit at wire level |
| 5 | All SP-API write methods blocked | ✓ Only GET allowed for SP-API endpoints |
| 6 | No Odoo business record mutations | ✓ Script never calls import/create methods |
| 7 | All 10 API-capable crons deactivated | ✓ Verified via SQL |
| 8 | Zero queued background jobs | ✓ Verified across all 5 job tables |
| 9 | Credentials redacted in logs | ✓ `SENSITIVE_KEYS` redaction in `AmazonAPI` |
| 10 | Emergency stop | ✓ Ctrl+C; guard cleanup in `finally` block |

---

## 8. Regression Baseline

### Pre-Hardening (Phase 24A initial)
| Metric | Value |
|---|---|
| Total tests | 611 |
| Failures | 0 |
| Errors | 0 |
| Duration | 878.67s |

### Phase 24 Safety Tests
| Metric | Value |
|---|---|
| Total tests | 27 |
| Failures | 0 |
| Errors | 0 |
| Duration | 0.32s |

### Post-Hardening Regression (includes 27 new safety tests)
| Metric | Value |
|---|---|
| Total tests | 638 |
| Failures | 0 |
| Errors | 0 |
| Duration | 922.14s |
| Queries | 730,044 |
| Result | **PASS — no regressions** |

---

## 9. Outstanding Blockers

| # | Blocker | Status | Resolution |
|---|---|---|---|
| 1 | Operator must provide real SP-API credentials | PENDING | Operator direct entry via staging UI |
| 2 | LWA application must have required SP-API permissions | PENDING | Operator to verify in Seller Central |
| 3 | API-capable crons must be deactivated | ✓ DONE | 10 crons deactivated in staging |
| 4 | Diagnostic script security review | ✓ DONE | 27 safety tests PASS |
| 5 | Full regression after hardening | ✓ DONE | 611 tests, 0 failures, 0 errors |
| 6 | Operator approval for Phase 24B | PENDING | This document requests it |

---

## 10. Exact Operations Proposed for Phase 24B

**In order:**

1. Developer starts staging Odoo (`odoo-bin -c odoo19_staging.conf`).
2. Operator creates Amazon instance and enters real credentials via UI.
3. Operator clicks "Test Connection" to verify OAuth.
4. Developer stops staging Odoo.
5. Developer runs diagnostic via `odoo-bin shell` with `RequestGuard` enforcement.
6. Diagnostic authenticates (OAuth POST, budget 1-2).
7. Diagnostic verifies Egypt marketplace (GET sellers, budget 1).
8. Diagnostic retrieves FBA orders (GET, budget 1-3, no import).
9. Diagnostic retrieves FBA inventory (GET, budget 1-5).
10. Diagnostic retrieves catalog sample (GET, budget 1).
11. Diagnostic lists settlement reports (GET, budget 1).
12. Diagnostic writes sanitized JSON report to `/tmp/phase24_diagnostic_report.json`.
13. Operator deletes the Amazon instance or clears credentials via SQL.
14. Developer re-enables the 10 deactivated crons (instance is gone).

**Total: ≤14 real HTTP requests. Zero Amazon writes. Zero Odoo business record mutations.**

---

## 11. Final Verification Pass (Independent Re-Check)

All findings were independently re-verified in a separate session:

| Check | Method | Result |
|---|---|---|
| Docker PG alive | `docker ps` + SQL `current_database()` | `amazon-staging-pg` Up, DB `amazon_phase19_fresh` on 5434 |
| Zero instances | SQL `SELECT count(*) FROM amazon_instance` | 0 rows |
| Zero credentials | SQL check all credential columns | 0 rows |
| Shared Odoo untouched | `ss -tlnp | grep 8019` | Running on 8019, NOT modified |
| Staging port free | `ss -tlnp | grep 8069` | Not listening (staging not running) |
| Git diff minimal | `git diff --stat HEAD` | 1 file changed (1 line in `tests/__init__.py`) |
| New files untracked | `git status` | `test_phase24_diagnostic_safety.py`, `PHASE24_LIVE_API_UAT.md` |
| Diagnostic script exists | `ls -la` scratchpad path | 18,857 bytes, EXISTS |
| 33 crons inventoried | SQL query with XML IDs | 5 active (LOCAL), 28 inactive |
| 10 API crons disabled | SQL verify `active=f` | All 10 confirmed disabled |
| Zero queued jobs | SQL across 6 job tables | 0 active jobs in all tables |
| 5 active crons LOCAL-only | Python call-chain trace | No API calls in any active cron |
| Diagnostic script audit | Full 482-line code review | All 14 safety properties verified |
| Safety tests pass | `--test-tags phase24_safety` | 27 tests, 0 failures, 0 errors |
| Credential fields audited | Model + view + ACL inspection | Plaintext at rest, view-masked, group-restricted |
| Full regression | `--test-tags /sdlc_amazon_connector` | 638 tests, 0 failures, 0 errors (922.14s) |

---

## APPROVAL GATE

**Phase 24A preparation and security hardening are complete.**

**The following actions require explicit operator approval:**

1. ☐ Using real Amazon SP-API credentials in the staging environment.
2. ☐ Making live Amazon API requests (≤14 read-only requests as detailed above).
3. ☐ Creating a real Amazon instance in the staging database.
4. ☐ Executing the Phase 24B diagnostic script.

**Do NOT proceed with Phase 24B until the operator explicitly approves.**
