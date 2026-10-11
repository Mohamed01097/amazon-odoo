# Phase 21 — Operational Hardening, Staging Isolation & Go-Live Preparation

**Date:** 2026-10-10
**Module:** sdlc_amazon_connector v19.0.11.2.0
**Git HEAD:** 79b2e3a

---

## 1. Safety Audit (Step A)

### Environment Summary

| Component | Value |
|---|---|
| Shared Odoo server | PID 19480, port 8019, no dbfilter |
| PostgreSQL | 14.24, local socket |
| Python | 3.12 (odoo19/venv) |
| Databases with module installed | amazon24sep (1 instance), amazon_phase19_fresh (0 instances) |
| Databases with module present but uninstalled | 14 (Saden, bnaa24se, dala, fmt, etc.) |
| Install/startup hooks | None |
| Automated actions (base_automation) | None |
| Active Amazon crons on amazon24sep | 6 (all local-only) |
| Inactive Amazon crons on amazon24sep | 27 |

### Automatic Live API Entry Points

All automatic API paths are **disabled**:

| Entry Point | Status | Mechanism |
|---|---|---|
| 10 API-calling crons | **INACTIVE** | Deactivated in Phase 20 |
| Install/init hooks | **NONE** | No hooks in manifest or __init__.py |
| Automated actions | **NONE** | No base_automation table entries |
| Startup workers | **NONE** | No custom worker threads |
| Queue processors | **SAFE** | `cron_dispatch_operational_retries` is INACTIVE |

Remaining API access is **manual only** — requires an operator to click a button in the UI (Import Orders, Sync Products, Test Connection, etc.).

**Safety gate: PASSED**

---

## 2. Cron Classification Matrix (Step C)

### Category: Local Housekeeping (no API calls)

| XML ID | Purpose | Default Active | Current Active | Interval | API Risk |
|---|---|---|---|---|---|
| cron_amazon_cleanup_operational_records | Purge old successful operation logs | Yes | **Yes** | 1 day | None |
| cron_amazon_detect_stuck_jobs | Flag jobs stuck beyond threshold | Yes | **Yes** | 10 min | None |
| cron_amazon_match_fba_reimbursements | Match reimbursement records to returns | Yes | **Yes** | 1 day | None |
| cron_amazon_operational_alerts | Evaluate local alert conditions | Yes | **Yes** | 15 min | None |
| cron_amazon_refresh_operations_dashboard | Recompute dashboard KPIs | Yes | **Yes** | 15 min | None |

### Category: Local Business Processing (no API calls)

| XML ID | Purpose | Default Active | Current Active | Interval | API Risk |
|---|---|---|---|---|---|
| cron_amazon_process_fba_sale_stock_events | Process queued FBA stock events | Yes | **Yes** | 1 min | None |
| cron_amazon_health_scores | Calculate product health scores | No | No | 1 day | None |

### Category: Amazon Read API

| XML ID | Purpose | Default Active | Current Active | Interval | API Risk |
|---|---|---|---|---|---|
| cron_amazon_import_orders | Fetch orders from SP-API | No | **No** | 30 min | **HIGH** — calls getOrders |
| cron_amazon_process_order_import_jobs | Process pending import jobs | No | **No** | 1 min | **HIGH** — calls getOrder/getOrderItems |
| cron_amazon_import_fbm_orders | Import FBM orders | No | No | 15 min | HIGH |
| cron_amazon_sync_order_statuses | Sync order status updates | No | **No** | 15 min | HIGH |
| cron_amazon_connection_health | Check API connection | No | **No** | 15 min | MEDIUM |
| cron_amazon_check_canceled | Check canceled orders | No | No | 2 hrs | HIGH |
| cron_amazon_sync_products | Sync product catalog | No | No | 6 hrs | HIGH |
| cron_amazon_pull_prices | Pull prices from Amazon | No | No | 6 hrs | HIGH |
| cron_amazon_pull_stock | Enqueue inventory audits | No | No | 4 hrs | MEDIUM |
| cron_amazon_import_fba_customer_returns | Import return reports | No | No | 1 day | HIGH |
| cron_amazon_import_fba_inventory_adjustments | Import adjustments | No | No | 1 day | HIGH |
| cron_amazon_import_fba_reimbursements | Import reimbursements | No | No | 1 day | HIGH |
| cron_amazon_import_settlement | Import settlement reports | No | No | 1 day | HIGH |
| cron_amazon_import_removal | Refresh removal status | No | No | 4 hrs | HIGH |
| cron_amazon_smart_alerts | Scan for alerts via API | No | No | 4 hrs | HIGH |
| cron_amazon_process_inventory_audits | Process inventory audits | No | **No** | 5 min | HIGH |
| cron_amazon_enqueue_inventory_audits | Enqueue inventory audits | No | **No** | 1 day | HIGH |
| cron_amazon_sync_inbound_receiving | Sync inbound receiving | No | **No** | 30 min | HIGH |
| cron_amazon_process_inbound_operation_jobs | Poll inbound operations | No | **No** | 1 min | HIGH |
| cron_amazon_process_phase7_jobs | Process removal/FBA events | No | **No** | 5 min | HIGH |

### Category: Amazon Write API

| XML ID | Purpose | Default Active | Current Active | Interval | API Risk |
|---|---|---|---|---|---|
| cron_amazon_export_stock | Export stock levels to Amazon | No | No | 2 hrs | **CRITICAL** — writes to Amazon |
| cron_amazon_update_prices | Update prices on Amazon | No | No | 4 hrs | **CRITICAL** |
| cron_amazon_update_fbm_status | Update FBM order status | No | No | 1 hr | **CRITICAL** |
| cron_amazon_full_sync | Full bidirectional sync | No | No | 1 hr | **CRITICAL** |
| cron_amazon_master_scheduler | Master auto-sync scheduler | No | No | 15 min | **CRITICAL** |

### Category: Mixed / Indirect

| XML ID | Purpose | Default Active | Current Active | Interval | API Risk |
|---|---|---|---|---|---|
| cron_amazon_dispatch_operational_retries | Retry failed operations | No | **No** | 5 min | HIGH — retries may call API |

### Recommended Controls

1. **Environment mode field** — Add `environment` selection ('development', 'staging', 'production') to `amazon.instance`. Gate all API-calling crons to check `environment == 'production'` before executing. Non-production environments should short-circuit with a log message.

2. **Credential validation gate** — Before any API call, verify that credentials are not placeholder values. Reject calls with obviously fake credentials.

3. **Activation audit log** — Log all cron active/inactive state changes to `amazon.sync.log` with the user who made the change.

4. **Manual confirmation for write APIs** — Export stock, update prices, and full sync should require an explicit operator confirmation via a wizard before the cron processes them.

---

## 3. Staging Isolation Architecture (Step B)

### Current Risk

The shared Odoo server (PID 19480, port 8019) has **no dbfilter**. It connects to every database on the PostgreSQL cluster and runs cron jobs against all of them. This means:
- Any database with `sdlc_amazon_connector` installed and active crons could trigger API calls
- The only current protection is that `amazon_phase19_fresh` has zero instances (so crons find nothing to process)

### Recommended Architecture

#### Option 1: Dedicated Staging Process (Recommended)

```
┌─────────────────────────────────┐    ┌────────────────────────────────┐
│   SHARED SERVER (existing)      │    │   STAGING SERVER (new)         │
│   Port: 8019                    │    │   Port: 8069                   │
│   Config: odoo19.conf           │    │   Config: odoo19_staging.conf  │
│   dbfilter: (unchanged)         │    │   db_name: amazon_phase19_fresh│
│   Serves: all 60+ databases    │    │   Serves: staging DB only      │
│   amazon24sep: crons INACTIVE   │    │   Crons: controlled           │
└─────────────────────────────────┘    └────────────────────────────────┘
              │                                     │
              └──────────┬──────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │   PostgreSQL 14.24  │
              │   Local socket      │
              └─────────────────────┘
```

**Staging config file** (`odoo19_staging.conf`):
```ini
[options]
; Copy from odoo19.conf, then override:
db_name = amazon_phase19_fresh
dbfilter = ^amazon_phase19_fresh$
http_port = 8069
gevent_port = 8072
; Same addons_path, db credentials
; workers = 0 (for development; set >0 for load testing)
```

**Start staging:**
```bash
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin -c odoo19_staging.conf
```

**Stop staging:**
```bash
kill $(pgrep -f 'odoo19_staging.conf')
```

**Key protections:**
- `db_name` + `dbfilter` restricts staging process to one database only
- Staging never touches `amazon24sep` or any other database
- Shared server continues unchanged — crons on `amazon24sep` remain inactive
- `amazon_phase19_fresh` has no instances, so even if shared server runs its crons, nothing happens

**Shared server cron protection for staging DB:**
The shared server will also connect to `amazon_phase19_fresh` and run its active crons. Since there are zero instances, this is harmless. However, if you later create a test instance on the staging DB, the shared server would also run crons against it. To prevent this:
- Keep API-calling crons on `amazon_phase19_fresh` at their fresh-install defaults (active for internal housekeeping, but no instance = no API calls)
- OR add a `dbfilter` to the shared server (requires approval and restart)
- OR use a separate PostgreSQL user for staging with access restricted to the staging DB only

#### Option 2: PostgreSQL-Level Isolation (Strongest)

Create a dedicated PostgreSQL role for staging:
```sql
CREATE ROLE odoo_staging WITH LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE amazon_phase19_fresh TO odoo_staging;
REVOKE CONNECT ON DATABASE amazon24sep FROM odoo_staging;
```

This guarantees the staging process cannot access `amazon24sep` even if misconfigured. The shared server's existing role cannot be restricted without affecting other projects.

#### Recommendation

Use **Option 1** (dedicated process + config file) for simplicity. Add **Option 2** (PostgreSQL role) if stronger isolation is needed for production staging.

---

## 4. Credential & Secret Security (Step D)

### Current State

| Credential | Storage | Status |
|---|---|---|
| SP-API Refresh Token | `amazon_instance.refresh_token` (DB field) | SET on amazon24sep |
| LWA Client ID | `amazon_instance.client_id` (DB field) | SET |
| LWA Client Secret | `amazon_instance.client_secret` (DB field) | SET |
| AWS Access Key | `amazon_instance.aws_access_key` (DB field) | EMPTY |
| AWS Secret Key | `amazon_instance.aws_secret_key` (DB field) | EMPTY |

### Access Control

| Group | Read | Write |
|---|---|---|
| Manager | Yes | Yes |
| Technical Administrator | Yes | Yes |
| User | Yes | **No** |

### Security Assessment

| Area | Status | Note |
|---|---|---|
| Credentials stored in DB | Standard Odoo pattern | Encrypted at rest if PostgreSQL TDE enabled |
| Logging redaction | **PASS** | No credential values found in _logger calls |
| Backup exposure | **RISK** | DB dumps in `/home/odoo16/db_backups_phase18/` contain credentials in plaintext |
| Environment separation | **PARTIAL** | Single DB has both dev and production credentials |
| Credential rotation | **NOT DOCUMENTED** | No rotation procedure exists |
| User field read access | **RISK** | Regular Users can read credential fields (read=True) |

### Recommendations

1. **Restrict User read access** — Change `ir.model.access` for User group to `perm_read=False` on credential fields, or use `groups` attribute on field definitions to hide from non-managers.
2. **Encrypt backups** — Use `gpg` encryption on database dumps that contain credentials.
3. **Staging credentials** — Never copy production credentials to staging. Use obviously fake placeholder values (e.g., `STAGING-NOT-REAL-TOKEN`).
4. **Document rotation** — Create a procedure for rotating SP-API refresh tokens and LWA credentials.
5. **Audit trail** — Log credential field changes to `amazon.sync.log`.

---

## 5. Monitoring & Alerting (Step E)

### Existing Capabilities

| Capability | Model/Feature | Status |
|---|---|---|
| Sync logging | `amazon.sync.log` (3,856 entries on amazon24sep) | **Active** |
| Operations dashboard | `amazon.operations.dashboard` | **Active** (cron refreshes every 15 min) |
| Smart alerts | `amazon.smart.alert` (13 alert types) | Cron **inactive** (requires API) |
| Health scores | `amazon.product.health` | Available but inactive |
| Job monitoring | `amazon.operation.control` + `amazon.operation.job.monitor` | **Active** |
| Stuck job detection | `cron_amazon_detect_stuck_jobs` | **Active** (10 min) |
| API telemetry | `amazon.api.operation.metric` | Available |
| Rate-limit metrics | Available | Via Operations menu |
| Operational alerts | `cron_amazon_operational_alerts` | **Active** (15 min) |

### Monitoring Gaps

| Gap | Severity | Recommendation |
|---|---|---|
| No external alerting (email/Slack) | Medium | Configure Odoo mail server + alert email recipients |
| No cron execution monitoring | High | Add cron execution logging to sync log |
| No unexpected API call detection | High | Add API call counter to dashboard; alert on calls when crons should be off |
| No backup monitoring | Medium | Scheduled backup script with success/failure notification |
| No disk space monitoring | Low | Standard OS-level monitoring |

### Proposed Alert Severity Levels

| Level | Response | Examples |
|---|---|---|
| P1 Critical | Immediate | Unauthorized API calls, credential failure, data corruption |
| P2 High | Same day | Failed order imports, inventory mismatch >5%, stuck jobs >2h |
| P3 Medium | Next day | Settlement mismatch, rate limiting, partial import |
| P4 Low | Weekly | Health score degradation, sync delays, log cleanup needed |

---

## 6. Backup, Restore & Rollback (Step F)

### Existing Backups

Location: `/home/odoo16/db_backups_phase18/`
Contents: 8 database dumps + 8 filestore archives (Phase 18, taken 2026-10-10)

### Backup Procedures

#### 1. PostgreSQL Backup
```bash
DBUSER="odoo17"  # from odoo19.conf
DBNAME="amazon24sep"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/home/odoo16/db_backups"

# Custom format (supports selective restore)
PGPASSWORD="$DBPASS" pg_dump -U "$DBUSER" -Fc -f "$BACKUP_DIR/${DBNAME}_${TIMESTAMP}.dump" "$DBNAME"

# Verify
pg_restore -l "$BACKUP_DIR/${DBNAME}_${TIMESTAMP}.dump" | head -5
```

#### 2. Filestore Backup
```bash
FILESTORE="/home/odoo16/.local/share/Odoo/filestore/$DBNAME"
tar czf "$BACKUP_DIR/${DBNAME}_${TIMESTAMP}.filestore.tgz" -C "$(dirname $FILESTORE)" "$(basename $FILESTORE)"
```

#### 3. Source Code Version
```bash
cd /home/odoo16/odoo/v19/amazon-odoo
git rev-parse HEAD > "$BACKUP_DIR/${DBNAME}_${TIMESTAMP}.git_head"
git diff > "$BACKUP_DIR/${DBNAME}_${TIMESTAMP}.uncommitted.patch"
```

#### 4. Configuration Backup
```bash
cp /home/odoo16/odoo/v19/odoo19/odoo19.conf "$BACKUP_DIR/odoo19_${TIMESTAMP}.conf"
```

#### 5. Integrity Verification
```bash
cd "$BACKUP_DIR"
sha256sum ${DBNAME}_${TIMESTAMP}.* > "${DBNAME}_${TIMESTAMP}.sha256"
sha256sum -c "${DBNAME}_${TIMESTAMP}.sha256"
```

### Restore Procedures

#### Database Restore
```bash
# Stop any Odoo process using the target database
# Create fresh database
PGPASSWORD="$DBPASS" createdb -U "$DBUSER" "${DBNAME}_restored"
PGPASSWORD="$DBPASS" pg_restore -U "$DBUSER" -d "${DBNAME}_restored" "$BACKUP_DIR/${DBNAME}.dump"
```

#### Filestore Restore
```bash
FILESTORE_DIR="/home/odoo16/.local/share/Odoo/filestore"
tar xzf "$BACKUP_DIR/${DBNAME}.filestore.tgz" -C "$FILESTORE_DIR/"
mv "$FILESTORE_DIR/$DBNAME" "$FILESTORE_DIR/${DBNAME}_restored"
```

#### Code Rollback
```bash
cd /home/odoo16/odoo/v19/amazon-odoo
git stash -u  # preserve uncommitted work
git checkout <target_commit>
# Then upgrade module:
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin -c odoo19.conf -d $DBNAME -u sdlc_amazon_connector --stop-after-init --http-port=8977
```

#### Cron Safety During Recovery
After any restore:
1. Verify all API-calling crons are inactive before starting Odoo
2. Check instance credentials are appropriate for the environment
3. Run `--stop-after-init` first to apply any migrations without cron execution

---

## 7. Operational Runbooks (Step G)

### Runbook 1: Starting/Stopping Amazon Staging

**Preconditions:** odoo19_staging.conf exists, amazon_phase19_fresh database exists

**Start:**
```bash
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin -c odoo19_staging.conf &
```

**Verify:** Open `http://localhost:8069` — should show only `amazon_phase19_fresh`

**Stop:**
```bash
kill $(pgrep -f 'odoo19_staging.conf')
```

### Runbook 2: Safely Enabling/Disabling Scheduled Actions

**Preconditions:** Identify the cron XML ID from the classification matrix above

**Enable a cron (via SQL — use when Odoo UI is not available):**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d "$DBNAME" -c "
UPDATE ir_cron SET active = TRUE
WHERE id = (SELECT res_id FROM ir_model_data
            WHERE module='sdlc_amazon_connector'
            AND name='cron_amazon_import_orders')
RETURNING cron_name, active;"
```

**Disable:**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d "$DBNAME" -c "
UPDATE ir_cron SET active = FALSE
WHERE id = (SELECT res_id FROM ir_model_data
            WHERE module='sdlc_amazon_connector'
            AND name='cron_amazon_import_orders')
RETURNING cron_name, active;"
```

**Via Odoo UI:** Settings > Technical > Scheduled Actions > search "Amazon" > toggle Active checkbox

**Verification:**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d "$DBNAME" -At -c "
SELECT d.name, c.active FROM ir_cron c
JOIN ir_model_data d ON d.model='ir.cron' AND d.res_id=c.id
WHERE d.module='sdlc_amazon_connector'
ORDER BY c.active DESC, d.name;"
```

### Runbook 3: Manual Order Import

**Preconditions:** Instance configured with valid credentials, products mapped

**Via UI:**
1. Amazon > Configuration > Instances > select instance
2. Click "Import Orders" button
3. Monitor sync log (Amazon > Reports > Sync Logs)

**Via shell (test mode):**
```bash
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin shell -c odoo19.conf -d amazon24sep <<'EOF'
inst = env['amazon.instance'].browse(6)
inst.action_import_orders()
env.cr.commit()
EOF
```

**Verification:** Check Amazon > Orders > All Orders for new records

### Runbook 4: Emergency Shutdown of Amazon Integration

**Preconditions:** Unexpected API activity detected or system issue

**Step 1 — Disable all Amazon crons immediately:**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d amazon24sep -c "
UPDATE ir_cron SET active = FALSE
WHERE id IN (SELECT res_id FROM ir_model_data
             WHERE module='sdlc_amazon_connector'
             AND model='ir.cron')
RETURNING cron_name, active;"
```

**Step 2 — Verify no running import jobs:**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d amazon24sep -At -c "
SELECT id, state FROM amazon_order_import_job
WHERE state IN ('pending','running','in_progress');"
```

**Step 3 — Verify no active connections processing Amazon data:**
```bash
PGPASSWORD="$DBPASS" psql -U "$DBUSER" -d postgres -c "
SELECT pid, state, query_start::timestamp(0), left(query, 80)
FROM pg_stat_activity
WHERE datname='amazon24sep' AND query ILIKE '%amazon%' AND state='active';"
```

**Step 4 — Document the incident:**
Record timestamp, reason for shutdown, affected records, and recovery plan.

**Recovery:** Re-enable only the specific crons needed, one at a time, after root-cause resolution.

### Runbook 5: Handling Settlement Mismatches

**Preconditions:** Settlement report shows `reconciliation_status = 'mismatch'`

1. Amazon > Accounting > Settlement Reports > filter by "Mismatch"
2. Open the settlement record
3. Compare calculated_total vs reported_total
4. Check line-level amounts for the mismatch source
5. If unknown category: check `amazon_suspense_account_id` on instance
6. Do NOT post the accounting entry until mismatch is resolved
7. Document the mismatch reason in the settlement's chatter

### Runbook 6: Investigating FBA Stock Discrepancies

1. Amazon > FBA > Inventory Differences
2. Compare Amazon-reported quantities vs Odoo stock levels
3. Check `amazon.inventory.reconciliation` records for the product/instance
4. If discrepancy is real: create manual stock adjustment (with approval)
5. If discrepancy is timing: wait for next reconciliation cycle
6. Never write directly to `stock.quant`

---

## 8. Production Go-Live Checklist (Step H)

### Infrastructure

- [ ] Isolated Odoo process for Amazon (dedicated config, port, dbfilter)
- [ ] HTTPS configured (reverse proxy)
- [ ] Database access restrictions (PostgreSQL role or dbfilter)
- [ ] Service management (systemd unit for auto-start/restart)
- [ ] Automated backup schedule
- [ ] Monitoring and alerting configured
- [ ] Log rotation configured

### Amazon Integration

- [x] SP-API authorization (credentials exist on amazon24sep)
- [x] Marketplace: Amazon.eg (ARBP9OOSHTCHU)
- [x] Seller ID configured
- [x] Region: Europe
- [ ] API permission scopes verified with Amazon
- [ ] Rate limit behavior tested under load
- [x] Order import functional (164 orders imported successfully)
- [ ] Inventory reconciliation validated with real data
- [x] Inbound shipping functional (4 shipments including LTL)
- [x] Returns/reimbursements import functional (audit-only)
- [ ] Settlement import validated with real settlements

### Odoo

- [x] Users and permissions (Manager, Tech Admin, User groups)
- [x] Company: Eisa Heikal For Commercial & Industrial Investment
- [x] Products: 19 SKUs mapped
- [x] Egypt VAT: 14% inclusive, deterministic resolver
- [x] FBA stock locations configured (Transit, Received, Sellable, Reserved, Unsellable)
- [ ] Cron activation plan documented and approved
- [x] Logging: 3,856 sync logs, operational dashboard
- [x] Alerts system available (13 alert types)

### Accounting

- [ ] Settlement accounting strategy decided (settlement-based vs invoice-aware)
- [ ] Revenue cutoff date set
- [ ] Suspense account configured
- [ ] Manual approval workflow for posting documented
- [ ] Reconciliation controls documented
- [ ] auto_post_invoice decision made

### Operations

- [ ] User acceptance testing by business operators
- [ ] Operator training completed
- [x] Emergency rollback procedure documented (this document)
- [ ] Cutover plan created (transition from current to managed state)
- [ ] Production signoff from stakeholders

### Legend
- [x] = Completed / Verified
- [ ] = Outstanding / Not Yet Done

---

## 9. Risk Register

| # | Risk | Severity | Likelihood | Mitigation | Status |
|---|---|---|---|---|---|
| R1 | Shared server runs crons on any database | High | High (no dbfilter) | Deactivated API crons; staging has no instances | **MITIGATED** |
| R2 | Accidental cron reactivation | High | Medium | noupdate=True prevents XML reload overwrite; runbook documents safe activation | **MITIGATED** |
| R3 | Credential exposure in backups | Medium | High | Backups contain plaintext credentials | **OPEN** — encrypt backups |
| R4 | No external alerting | Medium | High | Internal dashboard/alerts exist but no email/Slack notification | **OPEN** |
| R5 | Accountant decisions pending | High | Certain | Settlement accounting blocked until decisions made | **OPEN** |
| R6 | No production monitoring | High | Certain | No external health checks or paging | **OPEN** |
| R7 | Single operator knowledge | Medium | High | Runbooks created; training not yet done | **PARTIAL** |
| R8 | Write API crons could export wrong data | Critical | Low (all inactive) | Write crons default inactive; require explicit activation | **MITIGATED** |
| R9 | Refresh token expiration | Medium | Medium | No rotation procedure; token could expire silently | **OPEN** |
| R10 | No HTTPS | High | Certain (localhost only) | Production must use reverse proxy with TLS | **OPEN** |

---

## 10. Deployment Readiness Assessment

### Development: READY

- Code stable at v19.0.11.2.0
- 565/0/0 regression on clean install
- All accounting/inventory boundaries enforced
- Cron upgrade safety validated
- Multi-company isolation tested
- FBA E2E workflow tested with mocked API

### Staging: PARTIAL

| Item | Status |
|---|---|
| Staging database exists | READY (amazon_phase19_fresh) |
| Staging isolation design | READY (documented above) |
| Staging config file | NOT CREATED (requires approval) |
| Staging process tested | NOT DONE |
| UI/security validated | READY (Phase 20) |
| Cron safety on staging | READY (no instances) |

**Blockers:** Staging config file creation requires approval.

### Production: BLOCKED

| Item | Status |
|---|---|
| Infrastructure isolation | NOT IMPLEMENTED |
| HTTPS | NOT CONFIGURED |
| Backup automation | NOT IMPLEMENTED |
| Monitoring/alerting | PARTIAL (internal only) |
| Accountant decisions | NOT MADE |
| Operator training | NOT DONE |
| Live API validation | NOT AUTHORIZED |
| Production signoff | NOT OBTAINED |

**Blockers:**
1. Accountant decisions for settlement accounting (Phase 16)
2. Infrastructure isolation implementation
3. Live API validation authorization
4. Operator training
5. Stakeholder production signoff

---

## 11. Prioritized Implementation Plan

### Priority 1 — Immediate (Before Live UAT)
1. Create `odoo19_staging.conf` and test staging isolation
2. Encrypt database backups containing credentials
3. Configure Odoo mail server for alert notifications

### Priority 2 — Before Production
4. Resolve accountant decisions (settlement strategy, cutoff, suspense account)
5. Set up HTTPS reverse proxy
6. Create systemd service units
7. Implement automated backup schedule with verification
8. Set up external monitoring (health endpoint + cron execution tracking)

### Priority 3 — Go-Live Prerequisites
9. Conduct operator training
10. Perform controlled live API validation (read-only first)
11. Validate inventory reconciliation with real Amazon data
12. Validate settlement import with real settlement reports
13. Create cutover plan with rollback checkpoints
14. Obtain stakeholder signoff

### Priority 4 — Post Go-Live
15. Enable crons per the activation plan (one at a time, with monitoring)
16. Validate write API operations (if/when authorized)
17. Implement credential rotation procedure
18. Review and tune alert thresholds based on real operational data

---

## Overall Phase 21 Status: **PASS**

All investigation, documentation, and safety verification objectives completed.
No code changes made. No databases modified. No credentials exposed.
Safety gate verified — no automatic live API paths remain active.
