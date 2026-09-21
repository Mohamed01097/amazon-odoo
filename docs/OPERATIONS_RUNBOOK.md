# Operations Runbook

**Server:** ionic-odoo-dev-01
**OS:** Ubuntu 24.04.4 LTS
**Database:** amazon_prod12sep (PostgreSQL 16)
**Odoo:** 19 Community, service `odoo19`

---

## 1. Server Operations

### Check Odoo Service Status

```bash
systemctl status odoo19
```

### Restart Odoo (requires sudo)

```bash
sudo systemctl restart odoo19
```

**When to restart:** After module upgrade, after Python code changes, if crons appear stuck system-wide.

**DO NOT restart** during a running settlement import or active inbound shipment submission.

### View Odoo Logs

```bash
# Live tail
tail -f /var/log/odoo19/odoo19.log

# Last 100 lines
tail -100 /var/log/odoo19/odoo19.log

# Search for errors
grep -i error /var/log/odoo19/odoo19.log | tail -20

# Search for specific cron
grep "Process FBA Sale Stock Events" /var/log/odoo19/odoo19.log | tail -10
```

### Odoo Configuration

```bash
# View config (safe — passwords are in this file, use grep -v to exclude)
grep -v password /etc/odoo19.conf | grep -v admin_passwd
```

Key config values:
- `db_name = amazon_prod12sep`
- `dbfilter = ^amazon_prod12sep$`
- `addons_path` includes `/opt/odoo19/custom-addons/amazon-odoo`
- `http_port = 8069` (behind nginx)
- `workers = 0`, `max_cron_threads = 1`

### Nginx

```bash
# Check nginx status
systemctl status nginx

# Nginx config
cat /etc/nginx/sites-enabled/odoo19
```

Server name is `_` (catch-all). Proxies to `127.0.0.1:8069`.

---

## 2. Git Operations

### Check Repository State

```bash
cd /opt/odoo19/custom-addons/amazon-odoo
git status
git log --oneline -10
git branch -a
```

### Pull Updates (CAREFUL)

```bash
cd /opt/odoo19/custom-addons/amazon-odoo
git stash -u          # Save any uncommitted work
git pull origin main
git stash pop         # Restore uncommitted work (if any)
```

**After pulling code changes, restart Odoo and upgrade the module:**

```bash
sudo systemctl stop odoo19
/opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin -c /etc/odoo19.conf -u sdlc_amazon_connector --stop-after-init
sudo systemctl start odoo19
```

### Module Upgrade

```bash
# Stop service, upgrade, restart
sudo systemctl stop odoo19
/opt/odoo19/venv/bin/python /opt/odoo19/odoo/odoo-bin \
  -c /etc/odoo19.conf \
  -u sdlc_amazon_connector \
  --stop-after-init
sudo systemctl start odoo19
```

**DO NOT** run `-i` (install) on a module that is already installed — use `-u` (upgrade).

---

## 3. Database Operations

### Connect to Production Database (Read-Only)

```bash
sudo -u odoo19 psql -d amazon_prod12sep
```

### Useful Read-Only Queries

**Current FBA stock levels:**
```sql
SELECT sl.id, sl.complete_name, COALESCE(SUM(sq.quantity), 0) AS on_hand
FROM stock_location sl
LEFT JOIN stock_quant sq ON sq.location_id = sl.id
WHERE sl.id IN (29,30,31,32,33,34,35,36,37)
GROUP BY sl.id, sl.complete_name ORDER BY sl.id;
```

**Event state summary:**
```sql
SELECT state, COUNT(*) FROM amazon_fba_sale_stock_event GROUP BY state ORDER BY state;
```

**Depletion reconciliation:**
```sql
SELECT SUM(last_delta_qty) AS sum_delta FROM amazon_fba_sale_stock_event WHERE state='done';
-- Compare with: 944 - result = expected Sellable quant at location 31
```

**Order count:**
```sql
SELECT COUNT(*) FROM amazon_sale_order WHERE instance_id=6;
```

**Import/sync job status:**
```sql
SELECT state, COUNT(*) FROM amazon_order_import_job GROUP BY state;
SELECT state, COUNT(*) FROM amazon_order_status_sync_job GROUP BY state;
```

**Latest audit:**
```sql
SELECT id, name, state, create_date FROM amazon_inventory_reconciliation_run ORDER BY id DESC LIMIT 3;
```

**Instance key settings:**
```sql
SELECT auto_sync_enabled, stock_push_interval, price_push_interval, settlement_sync_interval,
       last_status_sync_at, last_stock_sync
FROM amazon_instance WHERE id=6;
```

**Cutover V2 state:**
```sql
SELECT id, state, history_start_at, cutover_at, baseline_count, total_fulfilled_before_cutover
FROM amazon_fba_sale_stock_cutover_run WHERE id=3;
```

**Cron status:**
```sql
SELECT ic.id, ic.active, ic.interval_number, ic.interval_type, ic.cron_name
FROM ir_cron ic
JOIN ir_model_data imd ON imd.res_id = ic.id AND imd.model = 'ir.cron'
WHERE imd.module = 'sdlc_amazon_connector'
ORDER BY ic.active DESC, ic.id;
```

### DANGEROUS — DO NOT RUN WITHOUT BACKUP/APPROVAL

```sql
-- NEVER run UPDATE/DELETE on production without explicit developer approval
-- NEVER modify amazon_fba_sale_stock_cutover_baseline
-- NEVER modify amazon_fba_sale_stock_cutover_run
-- NEVER modify stock_quant directly
-- NEVER delete amazon_fba_sale_stock_event records
```

---

## 4. Troubleshooting

### 1. Orders Stopped Importing

**Check:**
- Is cron 26 (Sync Order Statuses) active? Query: `SELECT active FROM ir_cron WHERE id=26;`
- Latest status sync job: `SELECT state, create_date FROM amazon_order_status_sync_job ORDER BY id DESC LIMIT 5;`
- Connection health: `SELECT connection_health, last_amazon_error_message FROM amazon_instance WHERE id=6;`

**Safe actions:**
- Click "Import Orders" on the instance form in Odoo UI
- Check Odoo logs for API errors: `grep "Sync Order" /var/log/odoo19/odoo19.log | tail -20`
- If cron 26 is inactive, investigate why before re-enabling

**Escalate if:** Jobs are being created but failing, or API returns authentication errors.

### 2. Order Exists in Amazon But Not in Odoo

**Check:**
```sql
SELECT * FROM amazon_sale_order WHERE amazon_order_ref = 'ORDER-ID-HERE';
```

**Safe action:** Click "Import Orders" on instance form. Next sync will pick it up.

### 3. Stock Not Depleted After Order Import

**Check:**
```sql
SELECT id, state, amazon_cumulative_fulfilled_qty, processed_fulfilled_qty, last_delta_qty
FROM amazon_fba_sale_stock_event
WHERE amazon_order_ref = 'ORDER-ID-HERE';
```

- If `state='pending'`: wait for cron 27 (runs every minute)
- If `state='done'` and `last_delta_qty=0`: C=P, no new fulfillment to deplete
- If `state='manual_review'`: see below
- If no event exists: product may be unmapped or order not FBA

### 4. Event in `manual_review`

**Check error code:**
```sql
SELECT id, last_error_code, last_error_message, amazon_cumulative_fulfilled_qty AS C,
       cutover_baseline_fulfilled_qty AS B, processed_fulfilled_qty AS P
FROM amazon_fba_sale_stock_event WHERE state='manual_review';
```

- `CUTOVER_BASELINE_OUTSIDE_COVERAGE`: Old order outside 1-year baseline window. Contact developer.
- `CUTOVER_BASELINE_EXCEEDS_CUMULATIVE`: B > C anomaly. Investigate on Amazon Seller Central.
- `UNMAPPED_FBA_SKU`: Map the product first.

**DO NOT** retry without understanding the cause.

### 5. Double Depletion Suspected

**Check for duplicate events:**
```sql
SELECT amazon_order_ref, amazon_order_item_id, COUNT(*)
FROM amazon_fba_sale_stock_event
GROUP BY amazon_order_ref, amazon_order_item_id HAVING COUNT(*) > 1;
```

Should return 0 rows (unique constraint prevents duplicates).

**Escalate immediately** if double depletion is confirmed.

### 6. Amazon/Odoo Stock Mismatch in Audit

**Normal:** Small differences (5-20 units) due to fulfillment timing.
**Abnormal:** Persistent growing gaps, or total gap > 50 units.

**Check:**
```sql
SELECT name, mismatch_count, unmapped_count, create_date
FROM amazon_inventory_reconciliation_run ORDER BY id DESC LIMIT 5;
```

### 7. SP-API Quota Exceeded (HTTP 429)

**Action:** Wait 15-30 minutes. Built-in retry handles this. Check logs:
```bash
grep "429\|throttl\|quota" /var/log/odoo19/odoo19.log | tail -10
```

### 8. Authentication Failure

**Check:**
```sql
SELECT connection_health, token_health, last_amazon_error_code
FROM amazon_instance WHERE id=6;
```

Credentials are configured on the instance. DO NOT expose them. If authentication fails persistently, verify credentials in the Odoo UI (Amazon > Configuration > Instances > Connection tab) and check Amazon Seller Central for app authorization status.

### 9. Cron Not Running

**Check:**
```sql
SELECT id, active, cron_name, nextcall FROM ir_cron WHERE id IN (25,26,27,35,36,39,40,46,47,48,49,50,51,52,56);
```

If a cron that should be active (from the 15-active list) is inactive, investigate before re-enabling. Check Odoo logs for the last execution error.

### 10. Unmapped SKU

**Check:** Amazon > Catalog > Products in Odoo UI.
**Fix:** Use Amazon > Catalog > Import / Map Products to link new SKUs.

### 11. Settlement Import Issues (When Configured)

Only relevant after accounting is configured. Pre-checks:
```sql
SELECT settlement_journal_id, amazon_clearing_account_id, amazon_sales_account_id
FROM amazon_instance WHERE id=6;
```

All must be non-NULL before importing settlements.

---

## 5. Backup Procedure

The server has `odoo19-backup.service` and `odoo19-backup.timer` configured. Check:

```bash
systemctl status odoo19-backup.timer
systemctl list-timers | grep odoo
```

### Manual Database Backup

```bash
sudo -u odoo19 pg_dump amazon_prod12sep > /tmp/amazon_prod12sep_backup_$(date +%Y%m%d_%H%M%S).sql
```

**Always take a backup before:** module upgrades, manual SQL operations, major configuration changes.

---

## 6. Credential Locations

Credentials are stored in the following locations. DO NOT print or log their values.

| Credential | Location |
|---|---|
| Amazon SP-API (Refresh Token, Client ID/Secret) | `amazon_instance` table, instance id=6 |
| AWS Access Key / Secret | `amazon_instance` table (may be NULL if using LWA-only) |
| Database password | `/etc/odoo19.conf` (`db_password` field, if set) |
| Odoo admin password | `/etc/odoo19.conf` (`admin_passwd` field) |

To verify credentials work: use "Test Connection" button on the instance form in Odoo UI.

---

*This runbook was prepared on 2026-09-20 using verified server paths and database state.*
