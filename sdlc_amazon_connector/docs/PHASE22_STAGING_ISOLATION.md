# Phase 22 — Staging Isolation Implementation & Security Validation

**Date:** 2026-10-11
**Module version:** 19.0.11.2.0
**Git HEAD:** 79b2e3a

---

## Objective

Establish a genuinely isolated staging environment where the shared Odoo server
cannot authenticate to the staging database, and the staging process cannot access
production databases.

---

## 1. Architecture Decision

### Problem
The shared Odoo server uses PostgreSQL role `odoo17` which is a SUPERUSER in the
main PostgreSQL 14 cluster (port 5432). No PostgreSQL-level mechanism within a
single cluster can prevent a superuser from accessing any database.

### Solution: Dedicated Docker PostgreSQL Container (Option B)

A separate PostgreSQL 14 instance running in Docker with:
- Independent credential store (role `odoo_stg` exists only here)
- Independent port (`127.0.0.1:5434`)
- Independent data directory (Docker volume `amazon-staging-pgdata`)
- No shared authentication with the main cluster

```
┌─────────────────────────────────────────────────────┐
│  Host: /home/odoo16                                 │
│                                                     │
│  ┌──────────────┐         ┌──────────────────────┐  │
│  │ Shared Odoo  │         │ Staging Odoo         │  │
│  │ Port 8019    │         │ Port 8069            │  │
│  │ Role: odoo17 │         │ Role: odoo_stg       │  │
│  │ No dbfilter  │         │ db_name: a_p19_fresh │  │
│  └──────┬───────┘         └──────────┬───────────┘  │
│         │                            │              │
│         ▼              ✗ BLOCKED ✗   ▼              │
│  ┌──────────────┐         ┌──────────────────────┐  │
│  │ PG 14 :5432  │ ──✗──▶ │ Docker PG 14 :5434   │  │
│  │ 61 databases │ ◀──✗── │ 1 database           │  │
│  │ odoo17 SUPER │         │ odoo_stg (local)     │  │
│  └──────────────┘         └──────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### Why Option B over alternatives

| Option | Verdict | Reason |
|--------|---------|--------|
| A — Separate PG cluster | Viable | Needs sudo for `pg_createcluster`; same isolation |
| **B — Docker PG** | **Selected** | Docker already in use (CRM on :5433); no sudo needed |
| C — Dedicated VM | Overkill | Adds operational burden for a dev staging environment |

---

## 2. Implementation Details

### Docker Container

| Property | Value |
|----------|-------|
| Container name | `amazon-staging-pg` |
| Image | `postgres:14-alpine` |
| Port | `127.0.0.1:5434` → 5432 |
| Volume | `amazon-staging-pgdata` |
| Role | `odoo_stg` |
| Database | `amazon_phase19_fresh` |
| Restart policy | `unless-stopped` |
| PG config | `shared_buffers=32MB, work_mem=4MB, max_connections=20` |

### Staging Odoo Process

| Property | Value |
|----------|-------|
| Config file | `/home/odoo16/odoo/v19/odoo19/odoo19_staging.conf` |
| HTTP port | 8069 |
| Gevent port | 8072 |
| DB host | 127.0.0.1 |
| DB port | 5434 |
| DB user | odoo_stg |
| DB name | amazon_phase19_fresh |
| dbfilter | `^amazon_phase19_fresh$` |
| list_db | False |
| Data dir | `/home/odoo16/.local/share/Odoo_staging` |
| Addons path | odoo19/addons, enterprise_addons, amazon-odoo |

### Database Migration

- Source: `amazon_phase19_fresh` in main cluster (95 MB, 640 tables, 1407 modules)
- Method: `pg_dump -Fc` → `pg_restore --no-owner --no-privileges`
- Dump file: `/tmp/amazon_phase19_fresh.dump` (7.5 MB compressed)
- Filestore: copied to `/home/odoo16/.local/share/Odoo_staging/filestore/` (460 files, 16 MB)
- Original database: **preserved intact** in main cluster

---

## 3. Isolation Verification Evidence

### Test 1 — Shared server CANNOT authenticate to Docker PG
```
$ PGPASSWORD=odoo17 psql -h 127.0.0.1 -p 5434 -U odoo17 -d amazon_phase19_fresh
FATAL: password authentication failed for user "odoo17"
```
**PASS** — The `odoo17` role does not exist in the Docker PG instance.

### Test 2 — Shared server CANNOT authenticate to Docker PG (postgres db)
```
$ PGPASSWORD=odoo17 psql -h 127.0.0.1 -p 5434 -U odoo17 -d postgres
FATAL: password authentication failed for user "odoo17"
```
**PASS**

### Test 3 — Staging role CANNOT authenticate to main cluster (production)
```
$ PGPASSWORD=odoo_stg_2026 psql -h /var/run/postgresql -p 5432 -U odoo_stg -d amazon24sep
FATAL: password authentication failed for user "odoo_stg"
```
**PASS** — `odoo_stg` does not exist in the main cluster's `pg_hba.conf` auth.

### Test 4 — Staging role CANNOT authenticate to main cluster (original staging)
```
$ PGPASSWORD=odoo_stg_2026 psql -h /var/run/postgresql -p 5432 -U odoo_stg -d amazon_phase19_fresh
FATAL: password authentication failed for user "odoo_stg"
```
**PASS**

### Test 5 — Staging Odoo connects to Docker PG only
```
odoo: database: odoo_stg@127.0.0.1:5434
111 modules loaded in 1.48s
Registry loaded in 2.029s
```
**PASS**

### Test 6 — No Amazon credentials in staging
```
amazon_instance count: 0
Instances with credentials: 0
```
**PASS** — No instances, no refresh tokens, no client IDs.

### Test 7 — Shared server cannot execute staging crons
The shared server runs on port 5432 with role `odoo17`. It cannot connect to port
5434 (Test 1). Therefore it cannot execute any cron in the Docker-hosted database.
**PASS**

### Test 8 — Docker PG not exposed publicly
```
Port binding: 127.0.0.1:5434->5432/tcp
```
**PASS** — Bound to localhost only.

---

## 4. Database Integrity Verification

| Metric | Original (main cluster) | Restored (Docker) | Match |
|--------|------------------------|-------------------|-------|
| Table count | 640 | 640 | ✓ |
| Module version | 19.0.11.2.0 | 19.0.11.2.0 | ✓ |
| ir_module_module rows | 1407 | 1407 | ✓ |
| ir_cron rows | 68 | 68 | ✓ |
| amazon_instance rows | 0 | 0 | ✓ |
| amazon_product rows | 0 | 0 | ✓ |
| amazon_sale_order rows | 0 | 0 | ✓ |
| Filestore files | 460 | 460 | ✓ |

---

## 5. Cron Safety Assessment

15 Amazon crons are active in the staging database. However:
- **0 Amazon instances** → crons find no records to process
- **0 API credentials** → even if a cron ran, no API authentication possible
- All API-calling crons require an active instance with valid credentials

**Risk: NONE** — No live API calls can occur.

---

## 6. Regression Results

```
565 post-tests in 849.68s, 637388 queries
sdlc_amazon_connector: 621 tests 849.59s 637388 queries
0 failed, 0 error(s) of 565 tests when loading database 'amazon_phase19_fresh'
```

**PASS** — 565 tests, 0 failures, 0 errors. Matches Phase 19/20 baseline.

Runtime: 849s (vs 595s on local socket) — ~43% slower over TCP to Docker PG.
This is expected and acceptable for development/UAT use.

---

## 7. How to Operate

### Start staging
```bash
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin -c odoo19_staging.conf
```
Access at: `http://localhost:8069`

### Start/stop Docker PG
```bash
docker start amazon-staging-pg
docker stop amazon-staging-pg
```

### View Docker PG logs
```bash
docker logs amazon-staging-pg --tail 20
```

### Connect to staging database
```bash
PGPASSWORD=odoo_stg_2026 psql -h 127.0.0.1 -p 5434 -U odoo_stg -d amazon_phase19_fresh
```

### Run tests
```bash
cd /home/odoo16/odoo/v19/odoo19
./venv/bin/python odoo-bin -c odoo19_staging.conf \
  -d amazon_phase19_fresh -u sdlc_amazon_connector \
  --test-enable --test-tags=/sdlc_amazon_connector \
  --stop-after-init --http-port=8977 --gevent-port=8978
```

---

## 8. Rollback Procedure

1. Stop staging Odoo: `kill <staging_pid>` or Ctrl+C
2. Stop container: `docker stop amazon-staging-pg`
3. Remove container: `docker rm amazon-staging-pg`
4. Remove volume: `docker volume rm amazon-staging-pgdata` (requires approval)
5. Remove filestore: `rm -rf ~/.local/share/Odoo_staging` (requires approval)
6. Remove config: `rm odoo19_staging.conf`

The original `amazon_phase19_fresh` in the main cluster is untouched.

---

## 9. Existing Limitations

1. **Host-level access:** A privileged OS user (`root`, `odoo16`) can access both
   PG instances. This is inherent to same-host deployment and only resolvable with
   a separate VM (Option C).

2. **Docker role is superuser:** The `odoo_stg` role in the Docker container is a
   superuser (Docker's `POSTGRES_USER` default). This is acceptable because:
   - The container has only one database
   - The isolation boundary is the container itself, not role restrictions within it
   - Odoo requires elevated privileges for module upgrades

3. **Disk space:** The host has only 9.8 GB free (96% used). The staging
   infrastructure uses ~350 MB. General disk cleanup is recommended but is outside
   Phase 22 scope.

---

## 10. Phase 22 Status Matrix

| Dimension | Status | Evidence |
|-----------|--------|----------|
| Architecture design | **PASS** | Docker PG selected, implemented, verified |
| Infrastructure implementation | **PASS** | Container running, DB restored, filestore copied |
| Database isolation (credential) | **PASS** | Tests 1-4: mutual authentication failure |
| Database isolation (network) | **PASS** | Docker PG on localhost:5434 only |
| Shared-server exclusion | **PASS** | `odoo17` cannot authenticate to Docker PG |
| Cron isolation | **PASS** | Shared server cannot reach Docker PG |
| Amazon API safety | **PASS** | 0 instances, 0 credentials |
| Functional staging validation | **PASS** | 111 modules loaded, clean startup |
| Regression tests | **PASS** | 565 tests, 0 failed, 0 errors (849.68s) |
| Production readiness | **BLOCKED** | Requires production credentials, go-live checklist |

**Overall Phase 22: PASS**
