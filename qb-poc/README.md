# Specialty Archery: QuickBooks Desktop <-> StockTrim (QuickBooks side)

Proves that a FastAPI backend using [`qbwc-kit`](https://pypi.org/project/qbwc-kit/) (0.1.3, untouched) can read from and
write to QuickBooks Desktop Enterprise through QuickBooks Web Connector (QBWC). No StockTrim code yet.
Single client (Specialty Archery). Plain venv, not Docker (approved exception for this POC). Python 3.10+.

```
QuickBooks Desktop <--COM--> QBWC --SOAP/http--> POST /qbwc (qbwc-kit, LoggingQBWCService)
                                                      |  authenticate -> DbAuthenticator.tasks_for() -> queued jobs
                                                      v
   curl / Swagger --> admin API (/jobs, /records, /log)     JobTask generators (app/tasks): build qbXML, parse + store
                                  \                         /
                                   +------ SQLite (jobs, qb_records, sync_log) ------+   raw qbXML -> fixtures/qbxml/
```

Admin endpoints do no QuickBooks work: they only queue jobs. QBWC polls, picks up queued jobs, runs them.

## Setup

```
py -3.10 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```
Edit `.env`: set `QBWC_PASSWORD` and `ADMIN_TOKEN` to long random values
(`python -c "import secrets;print(secrets.token_urlsafe(24))"`). The Specialty Archery `QWC_OWNER_ID` / `QWC_FILE_ID` GUIDs are
pre-filled and must never change once the `.qwc` is imported (changing them forces re-authorization in QuickBooks).
`APP_URL` must be https (http only for localhost) and end with `/qbwc`.

Tests: `.venv\Scripts\python -m pytest -q` (no QuickBooks needed; uses `FakeWebConnector` + `FakeQuickBooks`).

## Run for Specialty Archery

1. Terminal 1 (bind loopback only): `.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
2. Terminal 2: `ngrok http 127.0.0.1:8000 --url https://unrupturable-unreticently-carl.ngrok-free.dev`
   (authtoken is configured once via `ngrok config add-authtoken`; never put it in the repo).
   Check `https://<domain>/qbwc?wsdl` shows the WSDL.
3. Download the app file locally (admin routes are not reachable through the tunnel by design):
   `curl -H "X-Admin-Token: <token>" -o SpecialtyArchery-StockTrim.qwc http://localhost:8000/qwc`
4. Send the `.qwc` and the QBWC password (`QBWC_PASSWORD`) to the client via separate channels.
5. Client IT: QuickBooks Desktop open with the company file, logged in as Admin (single-user mode for the first authorization).
   On the machine running QuickBooks (needs internet) open Web Connector -> **Add an application** -> the `.qwc`. In the QuickBooks
   dialog choose **"Yes, always; allow access even if QuickBooks is not running"** and an Admin user. Enter the password in Web
   Connector, tick the app, **Update Selected**. Optionally enable Auto-Run (every `RUN_EVERY_N_SECONDS`).
6. Keep `WRITES_ENABLED=false` unless a write is deliberately tested.
7. Moving to the hosted server: set `APP_URL=https://<new-domain>/qbwc`, re-download and re-send the `.qwc`; the client removes the old
   app in Web Connector and adds the new one. OwnerID/FileID stay the same. The hosted reverse proxy (nginx/caddy) must set
   `X-Forwarded-For`, because the tunnel gate relies on it to tell proxied traffic from local calls.

## Manual test with real QuickBooks

1. **Start**: `.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
   (Swagger UI at http://localhost:8000/docs, use "Authorize" with your `ADMIN_TOKEN`.)
2. **Connect QBWC**
   - Download the app file: `curl -H "X-Admin-Token: <token>" -o SpecialtyArchery-StockTrim.qwc http://localhost:8000/qwc`
   - Open QuickBooks **as Administrator**, in **single-user mode**, with the company file open.
   - Open Web Connector -> **Add an application** -> pick `SpecialtyArchery-StockTrim.qwc`.
   - In QuickBooks choose **"Yes, always; allow access even if QuickBooks is not running"** (and pick an Admin user).
   - In Web Connector enter the password (= `QBWC_PASSWORD`), tick the app, click **Update Selected**.
3. **Queue jobs and inspect**
   ```
   curl -X POST -H "X-Admin-Token: <token>" http://localhost:8000/jobs/all-reads
   curl -X POST -H "X-Admin-Token: <token>" http://localhost:8000/jobs/customers
   ```
   Click **Update Selected** again, then:
   ```
   curl -H "X-Admin-Token: <token>" http://localhost:8000/jobs
   curl -H "X-Admin-Token: <token>" "http://localhost:8000/records/customers?limit=5"
   curl -H "X-Admin-Token: <token>" http://localhost:8000/log
   ```
   Write test (needs `WRITES_ENABLED=true` in `.env` and restart; run `vendors` + `items` first or pass IDs):
   ```
   curl -X POST -H "X-Admin-Token: <token>" -H "Content-Type: application/json" \
     -d '{"quantity": 5, "rate": 2.5, "idempotency_key": "demo-1"}' http://localhost:8000/jobs/create_test_po
   ```
   Re-POSTing the same `idempotency_key` after a failure retries the same job safely.
4. **Troubleshooting**
   - QBWC log: `C:\ProgramData\Intuit\QBWebConnector\log\QWCLog.txt`. Server console logs every callback.
   - Wrong password: QBWC shows "authentication failed"; the server answers `nvu`. Password in QBWC must equal `QBWC_PASSWORD`; username `QBWC_USERNAME`.
   - Company file not open / QuickBooks not running: QBWC calls `connectionError` (logged; session ends). Open the file in single-user mode.
   - `0x80040400` or other hresult (usually qbXML element-order/parse errors): the job is marked `error` with the hresult. Compare `fixtures/qbxml/<entity>_rq.xml` with the SDK reference.
   - Status 3100 / 3250: feature not available (e.g. Advanced Inventory off). Job is marked `unsupported`; nothing else fails.
   - ngrok must be running while QBWC syncs. Domain change = new `.qwc` (re-download and re-add in Web Connector).
   - ngrok free interstitial: `GET /qbwc?wsdl` with a browser-like User-Agent (incl. QBWC's .NET `Mozilla/4.0 ...` one) returned the ngrok HTML warning page
     (`ERR_NGROK_6024`) in our live check, while SOAP `POST /qbwc` returned normally. Plain curl UA gets the WSDL. QBWC fetches the WSDL only via the
     `.qwc`-listed URL and then POSTs, so check a real QBWC run; if it fails, a paid ngrok plan or another tunnel removes the interstitial.
   - Connection refused (local testing only): use `localhost` exactly as in the `.qwc`; if uvicorn binds only `127.0.0.1` while `localhost` resolves to IPv6 `::1`, edit `APP_URL` to `http://127.0.0.1:8000/qbwc` and re-download the `.qwc`.
   - **Reset**: remove the app in Web Connector; in QuickBooks Edit > Preferences > Integrated Applications > Company Preferences, remove the app; delete `qb_poc.db`. Keep OwnerID/FileID unchanged unless you want to re-authorize from scratch.

## Endpoints

All admin routes need header `X-Admin-Token`. Paths follow the spec as written (no `/api/v1` prefix in this POC).

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/qbwc` | qbwc-kit WSDL / SOAP (QBWC auth is the QBWC username/password) |
| GET | `/qwc` | download the `.qwc` (`QWC_FILENAME`, default `SpecialtyArchery-StockTrim.qwc`) |
| POST | `/jobs/{job_type}` | queue one job: customers, vendors, items, assemblies, sites, sales_orders, purchase_orders, create_test_po |
| POST | `/jobs/all-reads` | queue the 7 read jobs |
| GET | `/jobs`, `/jobs/{id}` | jobs (`?status=`, `?limit=`) |
| GET | `/records/{entity}` | stored records; accepts job-type alias (`customers`) or QB name (`Customer`); `limit`<=500, `offset` |
| GET | `/log` | sync_log (`?limit=`, `?job_id=`) |

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
- **Security**: tunnel gate (`app/tunnel_gate.py`): only `/qbwc` is reachable through a proxy/tunnel; every other path (admin API, `/docs`, `/openapi.json`) returns 404 unless the call is direct loopback with no `X-Forwarded-*`/`Forwarded` headers. Admin API additionally protected by `X-Admin-Token`; QBWC credentials compared in constant time; no CORS (no browser client); passwords are never logged.

## QBWC callback log

Every Web Connector SOAP callback is also written, one JSON object per line, to `LOG_DIR/qbwc.jsonl` (default `logs/`, git-ignored, created on first write). It rotates at UTC midnight into `qbwc.jsonl.YYYY-MM-DD`; the newest `LOG_RETENTION_DAYS` (14) rotated files are kept. The console output is unchanged and comes from the same call, so the two cannot drift.

Fields: `ts` (ISO 8601 UTC), `event` (callback name), `direction` (`in` = QBWC -> us, `out` = our reply), plus per event `version`, `username`, `ticket` (first 8 chars only), `company`, `qbXML`, `root` (request root element), `request_len`, `response_len`, `hresult`, `message`, `status`, `result`. Passwords, full tickets and qbXML bodies (customer data) are never logged, only sizes and the root element.

```
{"ts": "2026-10-22T14:03:11.482+00:00", "event": "sendRequestXML", "direction": "in", "ticket": "3f9a1c2e", "company": "C:\QB\sa.qbw", "qbXML": "13.0"}
{"ts": "2026-10-22T14:03:11.490+00:00", "event": "sendRequestXML", "direction": "out", "request_len": 412, "root": "CustomerQueryRq"}
```

## qbwc-kit 0.1.3 limitations and workarounds (library untouched)

- **L1** raw response XML is not passed to tasks -> `app/qbwc_service.py` `LoggingQBWCService.dispatch` stashes it per session for fixture saving.
- **L2** the library logs only some callbacks -> same override logs every callback (never the password or ticket).
- **L3** a QuickBooks hresult leaves the task suspended -> `on_session_end` (`app/auth.py`) marks jobs still `running` as `error` with the hresult.
- **L4** `FakeQuickBooks` ignores filters and does not set TxnID -> `tests/conftest.py` subclass adds both; production also filters by exact RefNumber.
- **L5** `FakeQuickBooks` uses 3100 for unsupported while real QB uses 3250 -> `UNSUPPORTED_STATUS_CODES = {3100, 3250}` in `app/tasks/base.py`.
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

Postgres via `DATABASE_URL` (add a driver), Alembic migrations (tables are created with `create_all` for now), StockTrim adapter, scheduler / incremental sync (`modified_after`).
