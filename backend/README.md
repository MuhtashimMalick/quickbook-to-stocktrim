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
                                   +-- PostgreSQL (jobs, qb_records, sync_log, po_link) --+   raw qbXML -> fixtures/qbxml/
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

## Next steps

StockTrim adapter, scheduler / incremental sync (`modified_after`).
