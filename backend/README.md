# Specialty Archery: QuickBooks Desktop <-> StockTrim (QuickBooks side)

Proves that a FastAPI backend using [`qbwc-kit`](https://pypi.org/project/qbwc-kit/) (0.1.3, untouched) can read from and
write to QuickBooks Desktop Enterprise through QuickBooks Web Connector (QBWC). No StockTrim code yet.
Single client (Specialty Archery). Runs in Docker Compose (Python 3.12) behind Caddy.

```
QuickBooks Desktop <--COM--> QBWC --SOAP/http--> POST /qbwc (qbwc-kit, LoggingQBWCService)
                                                      |  authenticate -> DbAuthenticator.tasks_for() -> queued jobs
                                                      v
   curl / Swagger --> admin API (/api/v1/jobs|records|log)     JobTask generators (app/quickbooks/tasks): build qbXML, parse + store
                                  \                         /
                                   +-- PostgreSQL (jobs, qb_records, sync_log, po_link, bom_edge, push_run, push_item) --+   raw qbXML -> fixtures/qbxml/
```

Admin endpoints do no QuickBooks work: they only queue jobs. QBWC polls, picks up queued jobs, runs them.

## Local development

Python 3.12 (CLAUDE.md requires 3.12+; the container uses 3.12). From the repo root, start the dev stack (db on `127.0.0.1:5432`,
Caddy with a self-signed cert on `https://localhost:8443`; `DOMAIN=localhost`, `APP_URL=https://localhost:8443/qbwc` in the dev `.env`):
```
copy .env.example .env     # root .env: fill the secrets (see below)
docker compose -f docker-compose.yml -f compose.dev.yml up -d --build
```
Venv workflow (uses the db above; `backend/.env` is only for this, copy it from `backend/.env.example` and set `DATABASE_URL` to the owner
credentials from the root `.env`):
```
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt -c constraints.txt
copy .env.example .env
.venv\Scripts\python -m alembic upgrade head   # schema comes ONLY from Alembic (no create_all); downgrade: ... downgrade base
```
`DATABASE_URL` (required, must start with `postgresql`) is read by the app and by Alembic. In Docker it is built in `docker-compose.yml`
from the root `.env` (`migrate` as the DB owner, `app` as the least-privilege `qb_app`); no credentials are repeated elsewhere.
New schema change: edit `app/models.py`, then `.venv\Scripts\python -m alembic revision --autogenerate -m "..."` and review it.
`QWC_OWNER_ID` / `QWC_FILE_ID` GUIDs are pre-filled and must never change once the `.qwc` is imported (changing them forces re-authorization in QuickBooks).
`APP_URL` must be https (http only for localhost) and end with `/qbwc`.

Tests (no QuickBooks needed; `FakeWebConnector` + `FakeQuickBooks`; real PostgreSQL, the `<db>_test` database is created and migrated automatically):
- venv: `.venv\Scripts\python -m pytest -q` (db from compose.dev.yml must be up). Lint: `.venv\Scripts\python -m ruff check app tests alembic`.
- container (Python 3.12, owner credentials): `docker compose -f docker-compose.yml -f compose.dev.yml --profile test run --rm test` (same `-f` flags as the stack, or compose recreates the db without its dev port).
- Dependencies: `requirements.txt` (runtime) / `requirements-dev.txt`; transitive pins in `constraints.txt`. To refresh it after changing requirements:
  `docker compose build test` then `docker run --rm --entrypoint pip quickbook-test freeze > backend/constraints.txt`.

Snapshot: each page is bulk-upserted into `qb_records`; after the last page of a successful read, rows of that entity the read did not
return are deleted (nothing is deleted if any page fails; windowed specs set `QuerySpec.full_snapshot=False` and never prune). Commits are per page plus a final prune, not one transaction, so downstream consumers must read an entity's snapshot only after its read job status is `done`.
Benchmark (Postgres 17.12, local dev machine, 2026-11-16): 50,000 records upserted in pages of 100: 56 s; prune of 49,000 stale rows: 1.1 s.
Migrations need only `DATABASE_URL` (`alembic -c backend/alembic.ini ...` also works from the repo root). On startup, jobs left `running` become `error` (writes are re-queued).

## Deployment (client's Linux server, Docker Compose)

Services: `db` (postgres 17.12, not published), `migrate` (one-shot `alembic upgrade head` as the DB owner), `app` (non-root, one uvicorn worker, not published),
`caddy` (80/443, automatic Let's Encrypt; proxies only `/qbwc*`, request bodies capped at 64 MB, everything else 404). Volumes: `pgdata`, `appdata` (`/data`: `logs/` and `fixtures/`), `caddy_data`.
Networks: `edge` (caddy + app; normal, so the app keeps outbound internet for the later StockTrim client) and `backend` (`internal: true`: db, migrate, app, test). Caddy has no route to the db.
All containers use json-file log rotation (10 MB x 5).

### Server prerequisites (fresh Ubuntu)

1. Docker Engine + compose plugin from Docker's official apt repo (https://docs.docker.com/engine/install/ubuntu/): add the repo key and source as documented there, then
   `sudo apt-get install docker-ce docker-ce-cli containerd.io docker-compose-plugin`. Optional: `sudo usermod -aG docker $USER` (re-login).
2. Firewall: `sudo ufw allow 22,80,443/tcp && sudo ufw enable`. Docker-published ports bypass ufw, so the stack publishes only 80/443 (never publish db/app; never use `compose.dev.yml` here).
3. Time sync (TLS and QBWC tickets need a correct clock): `timedatectl` must show `System clock synchronized: yes` (else `sudo timedatectl set-ntp true`).
4. A public DNS A record for `DOMAIN` pointing at the server; inbound 80 and 443 reachable from the internet.

### First deployment

1. `git clone <repo> && cd quickbook && cp .env.example .env && chmod 600 .env`
2. Edit `.env`: set `DOMAIN`, `APP_URL=https://<DOMAIN>/qbwc`, optionally `ACME_EMAIL`, and generate every secret
   (`POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `QBWC_PASSWORD`, `ADMIN_TOKEN`) with `openssl rand -hex 24`. Keep `WRITES_ENABLED=false`.
3. `docker compose up -d --build`, then `docker compose ps` (db healthy, migrate exited 0, app healthy, caddy up) and `docker compose logs app caddy`.
4. Check `https://<DOMAIN>/qbwc?wsdl` returns the WSDL.
5. Admin API: it is deliberately unreachable from outside (TunnelGate accepts only direct loopback calls, and the app port is not published), so call it from inside the app container with
   `python -m app.cli METHOD /path` (METHOD is GET or POST; adds `X-Admin-Token` from the container environment; paths are under `/api/v1`; `--json` sends stdin as the JSON body; on Git Bash for Windows prefix the command with `MSYS_NO_PATHCONV=1`, otherwise `/jobs` is rewritten to a Windows path):
   ```
   docker compose exec -T app python -m app.cli GET /jobs
   docker compose exec -T app python -m app.cli POST /jobs/all-reads
   docker compose exec -T app python -m app.cli GET /qwc > SpecialtyArchery-StockTrim.qwc
   echo '{"quantity": 5, "rate": 2.5, "idempotency_key": "demo-1"}' | docker compose exec -T app python -m app.cli POST /jobs/create_test_po --json
   ```
   **Pre-flight dry run** (read-only: the StockTrim client refuses any non-GET, nothing is written to StockTrim or QuickBooks; the app logs only counts, and every `ref` in the response is a QuickBooks ListID, never a name):
   ```
   echo '{"stocktrim": true, "check_suppliers": true, "check_suppliers_by_code": false, "check_sales_orders": false, "sample_limit": 50}' | docker compose exec -T app python -m app.cli POST /preflight --json
   ```
   (The `echo '{...}' |` form works in bash and PowerShell, not in cmd.exe: there put the JSON in a file and use `< body.json`.) All body fields are optional. Needs the QuickBooks snapshot in the DB and (for the StockTrim part) `STOCKTRIM_AUTH_ID`/`STOCKTRIM_AUTH_SIGNATURE`. Duration: QuickBooks-only about a second; with StockTrim and the defaults about 13 seconds measured against the TEST account (319 products, 1,173 customers, 6 suppliers; products, customers, purchase orders and one `GET /api/SuppliersBulk`). Opt-ins: `check_suppliers_by_code` adds one GET per supplier (~382 calls, measured 90 seconds in total), `check_sales_orders` one GET per product (~2,346 calls, ~8 minutes), so leave them off unless needed; the CLI waits 600 s by default (`--timeout`). One run at a time per process: a second concurrent call gets `409 preflight_running`.
   Reading it: `verdict.ok_to_push` is true only when nothing blocks and every check completed; it is NOT permission to push (this stage pushes nothing). `verdict.blocking` lists reason codes with counts: `stocktrim_not_empty` (StockTrim already holds products/customers/POs/suppliers/sales orders: we never overwrite, someone decides; `detail` has the per-entity counts and `products_overlap_product_id`), `stocktrim_readable_conflict` (a StockTrim `productCodeReadable` equals one of our keys; BOM keys would be ambiguous), `stocktrim_incomplete` (a step had errors, hit the paging cap or aborted; `detail` is `{step: errors}`), `stocktrim_suppliers_not_checked` (`check_suppliers: false`), `stocktrim_unreachable_or_bad_credentials` (a wrong `STOCKTRIM_AUTH_ID` shows up as a 500, a wrong signature as 401; nothing else was read), `stocktrim_skipped`/`stocktrim_not_configured`, `qb_snapshot_empty` (no pushable products), `qb_products_quarantined`, `qb_suppliers_quarantined`, `bom_cycle_withheld`. `warnings` do not block (double spaces, non-ASCII/long keys, unresolved vendors, excluded BOM components, negative stock, sub-assemblies without a BOM, `sales_orders_not_checked`: StockTrim stores sales orders for unknown products, so an empty product list does not mean there are none). `quickbooks.issues` groups problems as `entity.code` with counts and up to `sample_limit` `{ref, code}` samples. `stocktrim.<entity>` give existing counts and overlap with our keys (`overlap_product_id`, `overlap_readable`, `overlap_readable_only`, each with count and ListID samples; `duplicate_readable_groups` inside StockTrim); BOM links cannot be listed (`boms: unknown_cannot_list`). `snapshot.synced_at_max` is the newest sync per QuickBooks entity (the snapshot is read in one REPEATABLE READ transaction), `fingerprint` is a sha256 over the pushable keys, BOM edges and the StockTrim counts/overlaps (a later push can require it unchanged), and `plan` is an upper bound of the calls a push would need (one per record) with the ETA at `STOCKTRIM_MAX_RPS`.
6. Send the `.qwc` and the QBWC password (`QBWC_PASSWORD`) to the client via separate channels.
7. Client IT: QuickBooks Desktop open with the company file, logged in as Admin (single-user mode for the first authorization).
   On the machine running QuickBooks (needs internet) open Web Connector -> **Add an application** -> the `.qwc`. In the QuickBooks
   dialog choose **"Yes, always; allow access even if QuickBooks is not running"** and an Admin user. Enter the password in Web
   Connector, tick the app, **Update Selected**. Optionally enable Auto-Run (every `RUN_EVERY_N_SECONDS`).
8. `DOMAIN` change: update `DOMAIN` and `APP_URL` in `.env`, `docker compose up -d`, re-issue the `.qwc` (step 5) and send it; the client removes the old
   app in Web Connector and adds the new one. OwnerID/FileID stay the same.

### Cutover from the old ngrok + systemd + SQLite deployment

Two servers must never answer QBWC at once.
1. On the client's server stop and disable the old units: `sudo systemctl disable --now <old-app-unit> <old-ngrok-unit>` (find them with `systemctl list-units | grep -Ei 'qb|ngrok'`).
2. Check nothing else holds the ports: `sudo ss -ltnp | grep -E ':(80|443)\b'` must be empty before `docker compose up`.
3. Archive the old `qb_poc.db` (copy it somewhere safe). It is NOT migrated; the new stack starts empty and re-reads everything from QuickBooks.
4. Do the first deployment above, then the client removes the old app in Web Connector and adds the new `.qwc` (new URL, same OwnerID/FileID).
5. Confirm the old URL is dead (`curl -sI https://<old-ngrok-domain>/qbwc?wsdl` fails or is not the WSDL) and the old ngrok tunnel is gone.

### Operations

- Logs: `docker compose logs -f app` / `caddy` / `db` (rotated by Docker, 10 MB x 5). QBWC callback log `qbwc.jsonl` (rotated daily) in the `appdata` volume (`docker compose exec app ls /data/logs`).
- Backup (nightly cron on the host, `crontab -e`; the repo is at `~/quickbook`, create `~/quickbook-backups` first; `POSTGRES_*` come from the repo `.env`):
  ```
  15 2 * * * cd ~/quickbook && set -a && . ./.env && set +a && docker compose exec -T db pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB" > ~/quickbook-backups/qb-$(date +\%F).dump && find ~/quickbook-backups -name 'qb-*.dump' -mtime +14 -delete && rsync -a ~/quickbook-backups/ <offsite-host>:quickbook-backups/
  ```
  Keeps 14 days locally and copies offsite. OPEN QUESTION: who owns the backups and where the offsite copy lives is not decided yet.
- Restore (with `.env` exported as above): `docker compose stop app`; `docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner < backup.dump`;
  re-run the init script (grants, idempotent) `docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh`; `docker compose start app`.
- Update: `git pull && docker compose up -d --build` (the `migrate` service applies new migrations before the app starts).
- Secret rotation (edit `.env` first, then):
  - `POSTGRES_PASSWORD` is read only when the volume is first created: `docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER ROLE <POSTGRES_USER> PASSWORD '<new>'"`, then `docker compose up -d --force-recreate migrate` (the `test` service also uses it).
  - `APP_DB_PASSWORD`: `docker compose up -d db` (picks up the new env), `docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh`, `docker compose up -d --force-recreate app`.
  - `QBWC_PASSWORD` / `ADMIN_TOKEN`: `docker compose up -d app`; for `QBWC_PASSWORD` then tell the client the new password (Web Connector asks for it again).
- Least-privilege DB role: `docker/db/init-app-role.sh` creates `qb_app` (no superuser/createdb/createrole, DML only, no DDL, read-only `alembic_version`, no access to other databases) on a FRESH volume only.
  On an existing volume run once (idempotent, also re-run after a restore): `docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh`.
- Never use `compose.dev.yml` on the server (it publishes the db on 127.0.0.1:5432 and Caddy on 8080/8443).

## Manual test with real QuickBooks

Use the stack from Local development; `python -m app.cli` is the admin client (see First deployment step 5).

1. **Connect QBWC**
   - Download the app file: `docker compose exec -T app python -m app.cli GET /qwc > SpecialtyArchery-StockTrim.qwc` (its URL is `APP_URL`; in dev `https://localhost:8443/qbwc` needs a certificate QBWC trusts, otherwise test against the hosted server).
   - Open QuickBooks **as Administrator**, in **single-user mode**, with the company file open.
   - Open Web Connector -> **Add an application** -> pick `SpecialtyArchery-StockTrim.qwc`.
   - In QuickBooks choose **"Yes, always; allow access even if QuickBooks is not running"** (and pick an Admin user).
   - In Web Connector enter the password (= `QBWC_PASSWORD`), tick the app, click **Update Selected**.
2. **Queue jobs and inspect**
   ```
   docker compose exec -T app python -m app.cli POST /jobs/all-reads
   docker compose exec -T app python -m app.cli POST /jobs/customers
   ```
   Click **Update Selected** again, then:
   ```
   docker compose exec -T app python -m app.cli GET /jobs
   docker compose exec -T app python -m app.cli GET "/records/customers?limit=5"
   docker compose exec -T app python -m app.cli GET /log
   ```
   Write test (needs `WRITES_ENABLED=true` in `.env` and `docker compose up -d app`; run `vendors` + `items` first or pass IDs):
   ```
   echo '{"quantity": 5, "rate": 2.5, "idempotency_key": "demo-1"}' | docker compose exec -T app python -m app.cli POST /jobs/create_test_po --json
   ```
   Re-POSTing the same `idempotency_key` after a failure retries the same job safely.
3. **Troubleshooting**
   - QBWC log: `C:\ProgramData\Intuit\QBWebConnector\log\QWCLog.txt`. Server side: `docker compose logs app`.
   - Wrong password: QBWC shows "authentication failed"; the server answers `nvu`. Password in QBWC must equal `QBWC_PASSWORD`; username `QBWC_USERNAME`.
   - Company file not open / QuickBooks not running: QBWC calls `connectionError` (logged; session ends). Open the file in single-user mode.
   - `0x80040400` or other hresult (usually qbXML element-order/parse errors): the job is marked `error` with the hresult. Compare `fixtures/qbxml/<entity>_rq.xml` with the SDK reference.
   - Status 3100 / 3250: feature not available (e.g. Advanced Inventory off). Job is marked `unsupported`; nothing else fails.
   - Domain change = new `.qwc` (re-download and re-add in Web Connector).
   - Legacy: the pre-Docker proof of concept ran behind ngrok (its free tier showed an interstitial page on browser-like `GET /qbwc?wsdl`). It is replaced by Caddy on a real domain; ngrok is no longer used.
   - **Reset**: remove the app in Web Connector; in QuickBooks Edit > Preferences > Integrated Applications > Company Preferences, remove the app; drop the database (`docker compose down -v` for dev). Keep OwnerID/FileID unchanged unless you want to re-authorize from scratch.

## Endpoints

All admin routes need header `X-Admin-Token`. Admin routes live under `/api/v1`; the SOAP endpoint stays at `/qbwc`.

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/qbwc` | qbwc-kit WSDL / SOAP (QBWC auth is the QBWC username/password) |
| GET | `/api/v1/qwc` | download the `.qwc` (`QWC_FILENAME`, default `SpecialtyArchery-StockTrim.qwc`) |
| POST | `/api/v1/jobs/{job_type}` | queue one job: customers, vendors, items, assemblies, sites, sales_orders, purchase_orders, create_test_po |
| POST | `/api/v1/jobs/all-reads` | queue the 7 read jobs |
| GET | `/api/v1/jobs`, `/api/v1/jobs/{id}` | jobs (`?status=`, `?limit=`) |
| GET | `/api/v1/records/{entity}` | stored records; accepts job-type alias (`customers`) or QB name (`Customer`); `limit`<=500, `offset` |
| GET | `/api/v1/log` | sync_log (`?limit=`, `?job_id=`) |

Job statuses: `queued` -> `running` -> `done` | `error` | `unsupported`. Write jobs run before reads in a session.

## Behaviour

- **qbXML version**: per request `min(version QuickBooks reports, MAX_QBXML_VERSION=16.0)`; if QuickBooks reports none, falls back to `13.0`. Logged on every request (`using qbXML x.y`).
- **Status codes**: 0 ok; 1 nothing found (fine, job `done`, logged); 3100 and 3250 -> job `unsupported` (3250 is what real QB says for a disabled feature, 3100 is what qbwc-kit's fake says); anything else -> job `error` with `[code] message`. Both are logged to `sync_log`; the session always carries on with the next job.
- **Paging**: `MaxReturned=PAGE_SIZE` (100) with iterator Start/Continue. `InventorySiteQueryRq` is sent without an iterator. Whether `ItemSitesQueryRq` accepts an iterator must be confirmed from the captured fixture.
- **Fixtures**: first request/response of every query per job is saved raw to `fixtures/qbxml/<entity>_rq.xml` / `_rs.xml` (tests write to a temp dir instead). Set `SAVE_FIXTURES=false` to disable.
- **Assemblies**: each assembly is rebuilt as parent -> components with quantities, sub-assemblies expanded recursively (cycle guard, depth 10), never flattened. The tree is in `job.result.tree` and printed to the console / `sync_log`:
  ```
  Bike
    - 2 x Wheel [assembly]
        - 32 x Spoke
  ```
- **Sites**: needs Advanced Inventory. If QB rejects the request the job is `unsupported` with QB's message.
- **create_test_po** (idempotent): at POST time a job is created with a unique idempotency key and `RefNumber = "ST-" + first 8 hex of sha256(key)` (11 chars, QB's limit). When it runs: (1) `PurchaseOrderQueryRq` by RefNumber (results also re-filtered client-side by exact RefNumber); if found, store its TxnID/EditSequence and finish `done` ("already existed; not re-created"); (2) otherwise `PurchaseOrderAddRq` with Memo `Created by integration - Review Required`. If the Add response is lost the job is `error`; POSTing the same key re-queues it and step 1 finds the PO instead of creating a second one.
- **PO state**: the QB SDK `PurchaseOrderAdd` has no draft / pending-approval field, so the expected result is a normal **Open** PO (not received, not closed) carrying the Memo. **Verified against QuickBooks Enterprise (trial) on 2026-10-22:** the add response had `IsManuallyClosed=false` / `IsFullyReceived=false`, and the PO (`ST-6B01C344`, Memo as above) shows in QuickBooks. A forced retry of the same idempotency key found it via `PurchaseOrderQueryRq` and did not create a second one.
- **Security**: tunnel gate (`app/qbwc/gate.py`): only `/qbwc` is reachable through a proxy/tunnel; every other path (admin API, `/docs`, `/openapi.json`) returns 404 unless the call is direct loopback with no `X-Forwarded-*`/`Forwarded` headers. Admin API additionally protected by `X-Admin-Token`; QBWC credentials compared in constant time; no CORS (no browser client); passwords are never logged.

## QBWC callback log

Every Web Connector SOAP callback is also written, one JSON object per line, to `LOG_DIR/qbwc.jsonl` (default `logs/`, git-ignored, created on first write). It rotates at UTC midnight into `qbwc.jsonl.YYYY-MM-DD`; the newest `LOG_RETENTION_DAYS` (14) rotated files are kept. The console output is unchanged and comes from the same call, so the two cannot drift.

Fields: `ts` (ISO 8601 UTC), `event` (callback name), `direction` (`in` = QBWC -> us, `out` = our reply), plus per event `version`, `username`, `ticket` (first 8 chars only), `company`, `qbXML`, `root` (request root element), `request_len`, `response_len`, `hresult`, `message`, `status`, `result`. Passwords, full tickets and qbXML bodies (customer data) are never logged, only sizes and the root element.

```
{"ts": "2026-10-22T14:03:11.482+00:00", "event": "sendRequestXML", "direction": "in", "ticket": "3f9a1c2e", "company": "C:\QB\sa.qbw", "qbXML": "13.0"}
{"ts": "2026-10-22T14:03:11.490+00:00", "event": "sendRequestXML", "direction": "out", "request_len": 412, "root": "CustomerQueryRq"}
```

## qbwc-kit 0.1.3 limitations and workarounds (library untouched)

- **L1** raw response XML is not passed to tasks -> `app/qbwc/service.py` `LoggingQBWCService.dispatch` stashes it per session for fixture saving.
- **L2** the library logs only some callbacks -> same override logs every callback (never the password or ticket).
- **L3** a QuickBooks hresult leaves the task suspended -> `on_session_end` (`app/qbwc/auth.py`) marks jobs still `running` as `error` with the hresult.
- **L4** `FakeQuickBooks` ignores filters and does not set TxnID -> `tests/conftest.py` subclass adds both; production also filters by exact RefNumber.
- **L5** `FakeQuickBooks` uses 3100 for unsupported while real QB uses 3250 -> `UNSUPPORTED_STATUS_CODES = {3100, 3250}` in `app/quickbooks/tasks/base.py`.
- `.qwc` generation: no `IsReadOnly` (absent = read/write, wanted here), `AuthFlags`, `PersonalDataPref`, `UnattendedModePref`, `CertURL`; `QBType` is fixed `QBFS` (correct for Desktop); `AppSupport` defaults to the app URL.

## To verify by hand in QuickBooks

- [ ] Customers/vendors/items counts match QuickBooks lists.
- [ ] An assembly containing a sub-assembly prints the right nested tree and quantities.
- [ ] `sites` is `done` with Advanced Inventory on, `unsupported` with it off.
- [ ] Sales/purchase orders include line items.
- [ ] `create_test_po` creates one Open PO with the Memo and `ST-xxxxxxxx` RefNumber; retrying creates no second PO.
- [ ] `ItemSitesQueryRq` iterator accepted (see `fixtures/qbxml/ItemSites_rq.xml` / `_rs.xml`).
- [ ] qbXML version the log shows matches your QuickBooks.

## Push foundation (Phase 2, stage 8; nothing is pushed yet)
- Tables (migration `0003`; `0004` adds run status `done_with_failures` and `push_item.ref`, the QuickBooks ListID): `bom_edge` (the edge set last pushed, the BOM diff source, since StockTrim cannot list edges), `push_run` (one row per run: status, options, pre-flight fingerprint, counts-only summary) and `push_item` (per-item progress: entity, key, status, attempts, short `error_code`; never names or response bodies). This per-item progress is the plan's `sync_progress`. The `qb_app` role gets DML on them through the default privileges set by `docker/db/init-app-role.sh` (verified by a test: DML yes, DDL no). On an existing volume whose role was created before these tables, re-run that script only if the default privileges are missing.
- `app/sync/mappers.py`: pure domain -> StockTrim request mappers and `build_push_plan` (ordered `(key, request)` pairs: suppliers, products, BOM edges children-first, customers). Product category = the top-level QuickBooks group of `FullName` (omitted for top-level items); emails with several addresses use the first valid one; address lines equal to the vendor/company/customer name are dropped; `stockOnOrder` is never sent.
- `NEGATIVE_STOCK_POLICY=as_is|clamp` (default `as_is`): `clamp` sends 0 for negative stock; the mapper reports `stock_adjusted`; the push engine (next stage) owns the fallback when StockTrim rejects negatives.
- `app/stocktrim/limiter.py`: one process-wide rate limiter per StockTrim base URL, shared by every `StockTrimClient` (a later, lower `STOCKTRIM_MAX_RPS` tightens it, never loosens, with one warning; `defer(seconds)` backs off after a 429).
- `STOCKTRIM_ENVIRONMENT=test|production` (default `test`): with the base URL and a hash of the auth id it forms `stocktrim_target(settings)`, stored in `bom_edge.target` and `push_run.target` so test and production ledgers never mix. **Production deployments must set `STOCKTRIM_ENVIRONMENT=production`.** `push_run` allows one `running` row (partial unique index), carries a heartbeat `updated_at` and `requested_by`; status/entity values are CHECK-constrained (constants in `app/models.py`). A BOM edge key is `bom_edge_key(parent, component)` (JSON array). Client-facing behaviours: `docs/client-behaviours.md`.

## Push engine (Phase 2, stage 9: suppliers and products)
Code: `app/sync/engine.py`, `app/api/v1/push.py`. Customers, BOM edges, sales orders and POs are NOT pushed yet (the endpoint rejects those steps with 422).
- **Gates for a real run:** `STOCKTRIM_WRITES_ENABLED=true` (default false; 403 otherwise), the pre-flight is re-run at start and must be `ok_to_push` (so the StockTrim account is empty), and its `fingerprint` must equal the `preflight_fingerprint` you pass, and you must pass the dry-run's `plan_fingerprint` too (both come from the dry-run response; `plan_fingerprint` is required, 422 otherwise). The run thread rebuilds the plan and, if it no longer matches `plan_fingerprint` (QuickBooks data changed since the dry-run), the run ends `failed` with `summary.error = plan_fingerprint_mismatch` before a single `push_item` or StockTrim write. One active run at a time (in-process lock + the partial unique index on `push_run`): a second start gets `409 push_running`.
- **Commands** (from the repo root; in PowerShell/bash the JSON is piped, in cmd.exe use `< body.json`; with Git Bash on Windows prefix `MSYS_NO_PATHCONV=1`):
  ```
  echo '{"steps": ["suppliers", "products"], "dry_run": true}' | docker compose exec -T app python -m app.cli POST /push --json
  echo '{"steps": ["suppliers", "products"], "dry_run": false, "preflight_fingerprint": "<from the dry-run>", "plan_fingerprint": "<from the dry-run>"}' | docker compose exec -T app python -m app.cli POST /push --json
  docker compose exec -T app python -m app.cli GET /push/runs/<run_id>
  echo '{}' | docker compose exec -T app python -m app.cli POST /push/runs/<run_id>/cancel --json
  echo '{}' | docker compose exec -T app python -m app.cli POST /push/runs/<run_id>/resume --json
  ```
  The dry-run writes nothing to StockTrim (its client refuses any non-GET) and no `push_item` rows; it returns the plan counts, the pre-flight verdict, `plan_fingerprint` and `preflight_fingerprint`.
- **Reading progress** (`GET /push/runs/<id>`): `status` (running/done/done_with_failures/failed/interrupted/cancelled), `counts` by entity and item status (pending/done/failed/skipped), `error_codes` by entity, `eta_seconds` at `STOCKTRIM_MAX_RPS`, `samples` (up to 50 `{entity, ref, error_code}`, failed items first; `ref` is the QuickBooks ListID, never a supplier code/name since a Vendor Name can be a personal name) and, at the end, `summary` (duration summed over resumes, `items_failed`, fatal error code, negative-stock outcome with ListIDs, read-back mismatch counts). Names, messages and response bodies are never stored or returned. A run that finishes with any `failed` item (or `skipped`/`aborted`) ends `done_with_failures` (`summary.items_failed` counts them); it is resumable like `failed`: `resume` re-sends only the items that are not `done`.
- **Order and rules:** suppliers (chunks of 50 through the array endpoint; a chunk that fails is retried supplier by supplier to isolate the bad record), then products (one upsert POST per product, 4 concurrent, shared rate limiter). Each item is marked `done` in its own short transaction right after the 200. A product whose supplier is not `done` in the run is pushed WITHOUT the supplier link (`supplier_link_dropped`): StockTrim would auto-create an unknown supplier. A supplier counts as done if it is `done` in this run OR in any earlier run on the same StockTrim target, so a products-only run after a suppliers run keeps the links. On a `resume`, products that were pushed without a link while their supplier is now `done` are re-pushed WITH it. A 429 defers the shared limiter, so every worker slows down.
- **Cancel / resume / restart:** cancel is honoured within a chunk; unstarted items stay `pending`. `resume` keeps the run id, needs the same StockTrim target and the same recomputed `plan_fingerprint` (else `409 target_mismatch` / `plan_fingerprint_mismatch`), skips `done` keys, retries `pending`/`failed`/`skipped(aborted)`, accepts the statuses `interrupted`, `cancelled`, `failed` and `done_with_failures`, and does NOT need an empty account. A fatal error (bad credentials, StockTrim unavailable) stops the run with status `failed` and marks every not-attempted item `skipped` + `aborted`. At app start every `running` run is marked `interrupted` (resume it). On a clean shutdown (SIGTERM; the app service has `stop_grace_period: 30s`) the active run is cancelled and the app waits up to 8 s for its worker, so the run ends `cancelled`, resumable.
- **Negative stock (A13):** sent as-is. Observed live (TEST account): StockTrim stores negative `stockOnHand` unchanged (`docs/stocktrim-api-notes.md`). If it ever answers a 400 naming the on-hand field, the item is retried once with 0, `negative_stock_rejected` (count + ListIDs) goes to the summary and the rest of the run is clamped. After the products step every negative item and a deterministic sample of 25 other products are read back by readable code and compared with what was sent: `negative_stock_stored_as_is`, `negative_stock_silently_altered` and per-field mismatch counts are in the summary.
- **Measured (TEST account, 2026-11-16):** 382 suppliers + 2,346 products at 5 rps took about 9.5 minutes wall clock including a cancel and a resume; read-back found only rounding differences (StockTrim stores cost, price and stock on hand with 2 decimals, half-up). Since the review fixes the mapper rounds these three half-up to 2 decimals before sending (negative ties round away from zero: an unproven assumption), and the read-back compares exactly.
- **Error codes (closed set, `engine.ERROR_CODES`):** rejected_400/404/409/other, auth, unavailable, unknown_outcome, aborted, negative_stock_rejected, supplier_link_dropped, dependency_failed, cancelled, internal.

## Next steps

StockTrim adapter, scheduler / incremental sync (`modified_after`).
