# Specialty Archery: QuickBooks Desktop <-> StockTrim (QuickBooks side)

Proves that a FastAPI backend using [`qbwc-kit`](https://pypi.org/project/qbwc-kit/) (0.1.3, untouched) can read from and
write to QuickBooks Desktop Enterprise through QuickBooks Web Connector (QBWC). The StockTrim push engine, the scheduler and the admin UI are described below.
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
`web` (the admin SPA: nginx-unprivileged, read-only, not published), `caddy` (80/443, automatic Let's Encrypt; proxies `/qbwc` (exact; bodies capped at 64 MB) and `/api/v1/*` (1 MB) to the app and everything else to `web`; see Public exposure). Volumes: `pgdata`, `appdata` (`/data`: `logs/` and `fixtures/`), `caddy_data`.
Networks: `edge` (caddy + app; normal, so the app keeps outbound internet for the StockTrim client), `spa` (`internal`: caddy + web only) and `backend` (`internal: true`: db, migrate, app, test). Caddy has no route to the db; `web` has no route to anything but Caddy.
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
   (`POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `QBWC_PASSWORD`, `ADMIN_TOKEN`) with `openssl rand -hex 24`, and `JWT_SECRET` with `openssl rand -hex 32` (required, min 32 chars: the app refuses to start without it). Keep every flag off at first: `WRITES_ENABLED`, `STOCKTRIM_WRITES_ENABLED`, `SCHEDULER_ENABLED` and `SCHEDULER_ALLOW_PRODUCTION` all `false`, `STOCKTRIM_ENVIRONMENT=test`, `ALERT_WEBHOOK_URL` empty (the order to switch them on is in the go-live order below).
3. Build one image at a time (2 GB server: add the swapfile first, see the runbook below): `GIT_SHA=$(git rev-parse --short HEAD) docker compose build migrate`, `docker compose build web`, then `docker compose up -d --remove-orphans`, then `docker compose up -d --force-recreate caddy` (REQUIRED: the Caddyfile is a single-file bind mount). `docker compose ps -a` (db healthy, migrate exited 0, app and web healthy, caddy up) and `docker compose logs app caddy web`.
4. Check `https://<DOMAIN>/qbwc?wsdl` returns the WSDL.
5. Admin API (CLI): the CLI-only routes (see Public exposure) are unreachable from outside, and the app port is not published, so call it from inside the app container with
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
   Reading it: `verdict.ok_to_push` is true only when nothing blocks and every check completed; it is NOT permission to push (a real push needs the gates in Push engine). `verdict.blocking` lists reason codes with counts: `stocktrim_not_empty` (StockTrim already holds products/customers/POs/suppliers/sales orders: we never overwrite, someone decides; `detail` has the per-entity counts and `products_overlap_product_id`), `stocktrim_readable_conflict` (a StockTrim `productCodeReadable` equals one of our keys; BOM keys would be ambiguous), `stocktrim_incomplete` (a step had errors, hit the paging cap or aborted; `detail` is `{step: errors}`), `stocktrim_suppliers_not_checked` (`check_suppliers: false`), `stocktrim_unreachable_or_bad_credentials` (a wrong `STOCKTRIM_AUTH_ID` shows up as a 500, a wrong signature as 401; nothing else was read), `stocktrim_skipped`/`stocktrim_not_configured`, `qb_snapshot_empty` (no pushable products), `qb_products_quarantined`, `qb_suppliers_quarantined`, `bom_cycle_withheld`. `warnings` do not block (double spaces, non-ASCII/long keys, unresolved vendors, excluded BOM components, negative stock, sub-assemblies without a BOM, `sales_orders_not_checked`: StockTrim stores sales orders for unknown products, so an empty product list does not mean there are none). `quickbooks.issues` groups problems as `entity.code` with counts and up to `sample_limit` `{ref, code}` samples. `stocktrim.<entity>` give existing counts and overlap with our keys (`overlap_product_id`, `overlap_readable`, `overlap_readable_only`, each with count and ListID samples; `duplicate_readable_groups` inside StockTrim); BOM links cannot be listed (`boms: unknown_cannot_list`). `snapshot.synced_at_max` is the newest sync per QuickBooks entity (the snapshot is read in one REPEATABLE READ transaction), `fingerprint` is a sha256 over the pushable keys, BOM edges and the StockTrim counts/overlaps (a later push can require it unchanged), and `plan` is an upper bound of the calls a push would need (one per record) with the ETA at `STOCKTRIM_MAX_RPS`.
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
- Update: `git pull`, then `GIT_SHA=$(git rev-parse --short HEAD) docker compose build migrate`, `docker compose build web`, `docker compose up -d --remove-orphans`, `docker compose up -d --force-recreate caddy` (REQUIRED); the `migrate` service applies new migrations before the app starts. For a release use the consolidated runbook below (backup first).
- Secret rotation (edit `.env` first, then):
  - `POSTGRES_PASSWORD` is read only when the volume is first created: `docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER ROLE <POSTGRES_USER> PASSWORD '<new>'"`, then `docker compose up -d --force-recreate migrate` (the `test` service also uses it).
  - `APP_DB_PASSWORD`: `docker compose up -d db` (picks up the new env), `docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh`, `docker compose up -d --force-recreate app`.
  - `JWT_SECRET`: `docker compose up -d app`; every issued access token becomes invalid (users log in again; refresh cookies keep working).
  - `QBWC_PASSWORD` / `ADMIN_TOKEN`: `docker compose up -d app`; for `QBWC_PASSWORD` then tell the client the new password (Web Connector asks for it again).
- Least-privilege DB role: `docker/db/init-app-role.sh` creates `qb_app` (no superuser/createdb/createrole, DML only, no DDL, read-only `alembic_version`, no access to other databases) on a FRESH volume only.
  On an existing volume run once (idempotent, also re-run after a restore): `docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh`.
- Never use `compose.dev.yml` on the server (it publishes the db on 127.0.0.1:5432 and Caddy on 8080/8443).

## Admin-UI authentication (stage B1)

Users live in `app_user` (username lowercase `[a-z0-9._-]{3,40}`, argon2id hash); there are no roles, no signup and no password reset. Manage them from inside the container (direct DB access, no HTTP; the password is prompted twice and never an argument):
```
docker compose exec app python -m app.cli users add <username>      # also: passwd (revokes that user's sessions) | disable | enable
docker compose exec app python -m app.cli users list               # username, active, last login (no hashes)
```
Password policy: at least 14 characters, not the username, not in a built-in list of common passwords.

- `POST /api/v1/auth/login` `{username, password}` -> `{access_token, token_type: "bearer", expires_in, session_expires_at}` plus the cookie `qb_refresh` (opaque 32 random bytes, only its sha256 is stored; HttpOnly, Secure, SameSite=Strict, Path=/api/v1/auth, lifetime `REFRESH_TOKEN_HOURS`, default 24 from LOGIN: a refresh does not extend the session, the rotated cookie inherits the original expiry and Max-Age is the remaining time, so the user logs in again after 24 h). JSON body only.
- `POST /api/v1/auth/refresh` (cookie only) -> new access token and a rotated cookie. Presenting an already rotated/revoked refresh token revokes ALL of that user's refresh tokens (401 `invalid_refresh`), except a token rotated less than `REFRESH_REUSE_GRACE_SECONDS` (default 10) ago: that is a benign concurrent refresh (only if it comes from the same IP and User-Agent as the rotation; logged at INFO), answered 401 `refresh_race` (every other refresh failure is `invalid_refresh`) without revoking anything or clearing the cookie (the browser retries with the cookie the first response set). Replaying a token revoked by logout/passwd always revokes all.
- `POST /api/v1/auth/logout` (cookie) -> 204, revokes it and clears the cookie. `GET /api/v1/auth/me` (Bearer) -> `{username, last_login_at}`.
- Access token: HS256 JWT signed with `JWT_SECRET`, lifetime `ACCESS_TOKEN_MINUTES` (default 15), claims `sub` (user id), `username`, `iat`, `exp`, `jti`; the algorithm is pinned and the user (must be active) is looked up on every request. 401 `{"detail": "token_expired"}` or `"invalid_token"`.
- Every other admin route accepts `Authorization: Bearer <access token>`, or `X-Admin-Token` on a direct loopback call only (the CLI above; unchanged). On a proxied request (any `X-Forwarded-*`/`Forwarded` header or a non-loopback peer) `X-Admin-Token` is ignored. Login, refresh and logout are the only public routes.
- Login protection: unknown user, wrong password, disabled user and a locked account all answer 401 `invalid_credentials` (an argon2 verify runs in every case, outside any DB transaction, with at most 4 verifies at once; more waiting than 2 s gets 429 `rate_limited`). 5 consecutive failures within 15 minutes lock the account for 15 minutes (the lock is logged as a WARNING with user id and IP; `users list` shows `locked=yes|no`); a success, `users passwd` or `users enable` resets it. Login and refresh are limited to 10 attempts per minute per client IP (429 `rate_limited`, in-memory LRU sliding window, IPv6 keyed by its /64: fine for the single uvicorn worker). The client IP is the last `X-Forwarded-For` entry (set by Caddy), else the socket peer. Login failures never set a cookie.
- CSRF: login, refresh and logout reject (403 `cross_site_request`) a `Sec-Fetch-Site` other than same-origin/none or an `Origin` that is not the request `Host` or is not https (or the request's own scheme).
- Access tokens are not revocable: after logout they stay valid for up to `ACCESS_TOKEN_MINUTES` (15, accepted). `users passwd` and `users disable` reject all older access tokens at once (`tokens_valid_after`).
- Frontend contract: keep the access token in memory only (never localStorage); refresh is single-flight (one request at a time); on a 401 `refresh_race` (benign concurrent refresh, no `Set-Cookie`) retry once after ~300 ms with the cookie the first response set; on `invalid_refresh` go to the login page. Login and refresh responses carry `session_expires_at` (UTC ISO): the absolute end of the session, which rotation never moves; warn before it.
- Passwords, hashes, tokens and the cookie are never logged; failed logins log only the outcome and the username length.
- Migrations `0008`-`0010` add `app_user` and `refresh_token` (plus rotation and lockout bookkeeping) (DML for `qb_app` via the default privileges, no DDL).

## Public exposure (stage B2)

One domain: `https://<DOMAIN>/qbwc` -> app (unchanged), `https://<DOMAIN>/api/v1/*` -> app (admin API for the SPA, Bearer JWT), everything else -> the `web` service (the SPA); Caddy answers 404 itself for `/docs*`, `/redoc*`, `/openapi.json*`, `/health*`, `/qbwc*` (except the exact `/qbwc`) and `/api*` outside `/api/v1/*`, so the SPA fallback never turns them into a 200. Unknown other paths return the SPA `index.html` (200).
Two layers, both must allow a request:
1. Caddy (`docker/caddy/Caddyfile`): explicit `handle`s for `/qbwc` (exact) and `/api/v1/*` (`request_body max_size 1MB`, `X-Admin-Token` stripped); everything else goes to the SPA (`web`), except the reserved backend-looking paths, which Caddy answers 404. `/docs`, `/redoc`, `/openapi.json` are never routed. No `trusted_proxies`, no Caddy rate limit (core Caddy has none; the app limits login/refresh).
   Headers on every response: `Strict-Transport-Security: max-age=31536000; includeSubDomains`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'; default-src 'none'` (API-only on `/qbwc` and `/api/v1/*`; the SPA paths get the SPA policy), `Server` removed.
2. App: `TunnelGate` lets a proxied request through only for the decoded `scope["path"]` `== /qbwc` or starting with `/api/v1/` (raw_path ignored, nothing normalised); all else (including `/docs`, `/redoc`, `/openapi.json`, `/health`, `/api/v1`, dot-segment/encoded variants) is 404. Direct loopback calls (the CLI) are unchanged, so `/docs` and `/openapi.json` stay loopback-only.
   Inside `/api/v1` exposure is default-deny: routes on `router` in `api/v1/router.py` carry the route-level dependency `direct_only` (404 unless a direct loopback call, decided on the matched route so encoded or trailing-slash variants cannot bypass it); only routes registered on `public` (and the auth router) are proxy-reachable. `tests/test_exposure.py` freezes both sets, so a new route is CLI-only until someone adds it to `public` and to that test.

| Reachable through the proxy (Bearer; X-Admin-Token is ignored) | CLI only (loopback + X-Admin-Token; 404 when proxied, even with a valid Bearer) |
|---|---|
| `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` (no auth), `GET /auth/me`, `GET /health`, `GET /overview`, `GET /settings`, `GET /reason-codes`, `GET /cycles`, `GET /cycles/{id}`, `GET /events`, `GET /push/runs`, `GET /push/runs/{id}`, `GET /push/runs/{id}/entries`, `GET /push/runs/{id}/entries.csv`, `GET /schedule`, `PUT /schedule`, `POST /scheduler/run-now`, `POST /preflight` (read-only), `GET /jobs`, `GET /jobs/{id}` | `GET /qwc`, `GET /records/{entity}` (raw QuickBooks records), `GET /log`, `POST /jobs/all-reads`, `POST /jobs/{job_type}` (incl. `create_test_po`), `POST /push`, `POST /push/runs/{id}/cancel`, `POST /push/runs/{id}/resume` |

Hidden existence: `redirect_slashes` is off, and a proxied wrong-method (405) or bad-body (422) call to a CLI-only route is also answered 404 (`hide_cli_only` in `qbwc/gate.py`).

Login flood protection: a busy argon2 slot answers 429 after 0.1 s (`VERIFY_WAIT`), login/refresh share a global 60/minute bucket on top of the per-IP 10/minute (429 with `Retry-After: 60`), and the CSRF/rate-limit dependencies are `async` so a rejected request never takes a worker thread (the pool is shared with `/qbwc`). `GET /health?probe=true` allows one StockTrim probe at a time (a concurrent one reports `error: busy`).

Leak rules: `GET /jobs` `status_message` may contain QuickBooks error text and is visible only to authenticated admins. Dev: never run `caddy trust` (HSTS would stick to `localhost`); use `curl -k`.

The refresh cookie is `Path=/api/v1/auth`, so a browser never sends it to `/qbwc` or any other route. Login success, lock, refresh reuse and benign replay are logged (never secrets). Access-token and CSRF rules are in the section above.

Post-deploy smoke matrix (run from any machine against the real domain; expected status codes; `D=https://<DOMAIN>`):
```
curl -s -o /dev/null -w '%{http_code}\n' "$D/qbwc?wsdl"                       # 200
for p in /docs /redoc /openapi.json / /health /qbwc/x /qbwcfoo /QBWC /api/v1/qwc /api/v1/records/customers /api/v1/jobs/create_test_po /api/v1/q%77c /api/v1/../openapi.json; do
  curl -s --path-as-is -o /dev/null -w "$p %{http_code}\n" "$D$p"; done          # all 404
curl -s -o /dev/null -w '%{http_code}\n' "$D/api/v1/health"                    # 401 (no token)
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Admin-Token: dummy-not-the-real-token" "$D/api/v1/health"   # 401 (never send the real ADMIN_TOKEN over the internet; Caddy strips the header anyway)
curl -s -H 'Content-Type: application/json' -d '{"username":"nobody","password":"wrong-password-1"}' "$D/api/v1/auth/login"   # 401 {"detail":"invalid_credentials"}
curl -s -D - -o /dev/null "$D/api/v1/health" | grep -iE 'strict-transport|x-content-type|referrer-policy|x-frame|content-security|^server'   # 5 headers, no Server
```
Every signed-in user (there are no roles) can start a cycle (`POST /scheduler/run-now`), run the pre-flight and change the schedule (`PUT /schedule`); nothing in the proxy-reachable set pushes to StockTrim directly (`POST /push` is CLI-only). Then log in as a real user (`users add`) and check `GET /api/v1/health` with the Bearer is 200 and `GET /api/v1/qwc` with the same Bearer is 404.

## Deploying the admin UI release (consolidated)

One ordered runbook for the release that adds the admin API (B1-B6), the sync schedule, the run log and the SPA (`web`). It replaces the old per-stage "Deploying B1+B2" and "Deploying F1" lists. The server is a 2 GB droplet: build ONE image at a time. The database ends at Alembic revision `0012` (this release adds no migration).

**0. Planning window.** Deploy BEFORE the client's Web Connector connects (outside its Auto-Run times); `/qbwc` is down for about 30 s while `app` restarts. The `.qwc` does not change when `DOMAIN`, `APP_URL`, `QWC_OWNER_ID` and `QWC_FILE_ID` are unchanged: prove it with `docker compose exec -T app python -m app.cli GET /qwc | sha256sum` before and after (same hash).

**1. Pre-checks.**
```
git rev-parse HEAD                       # write it down: the code you are leaving
git status --short                       # must be clean
free -h; swapon --show; df -h /; docker system df
sudo ss -ltnp | grep -E ':(80|443)\b'    # only caddy (docker-proxy) holds them
docker compose exec app alembic current  # the revision you are leaving
docker compose exec -T app python -m app.cli GET /jobs > ~/quickbook-backups/jobs-before.json   # nothing may be running or queued that matters
```

**2. Backups** (never skip; this is the rollback).
```
mkdir -p ~/quickbook-backups && set -a && . ./.env && set +a
docker compose exec -T db pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB" > ~/quickbook-backups/pre-ui-$(date +%F).dump
pg_restore --list ~/quickbook-backups/pre-ui-$(date +%F).dump | head -5        # must list the TOC (a pg_restore binary on the host, or: docker compose exec -T db pg_restore --list < the dump)
cp .env ~/quickbook-backups/env-pre-ui-$(date +%F) && chmod 600 ~/quickbook-backups/env-pre-ui-$(date +%F)
docker run --rm -v quickbook_caddy_data:/d -v ~/quickbook-backups:/b alpine tar czf /b/caddy_data-$(date +%F).tgz -C /d .    # the volume name is <project>_caddy_data: check `docker volume ls`
scp ~/quickbook-backups/*$(date +%F)* <offsite-host>:quickbook-backups/
```
Who owns the offsite copy is an OPEN decision (see Operations); until it is decided, copy it to the person doing the deployment.

**3. Swapfile** (once; the Node build must not be OOM-killed on 2 GB).
```
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**4. Check out** the release: `git fetch && git checkout <branch/commit>` (or `git pull`).

**5. `.env` changes, without printing values.**
```
comm -13 <(grep -oE '^[A-Z_]+=' .env | sort) <(grep -oE '^[A-Z_]+=' .env.example | sort)   # keys the new .env.example has and yours lacks
printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)" >> .env      # only if the key is missing
grep '^JWT_SECRET=' .env | cut -d= -f2- | tr -d '\n' | wc -c    # must print 64
```
Set every flag EXPLICITLY to `false` (add the line when it is missing): `WRITES_ENABLED`, `STOCKTRIM_WRITES_ENABLED`, `SCHEDULER_ENABLED`, `SCHEDULER_ALLOW_PRODUCTION`; keep `STOCKTRIM_ENVIRONMENT=test` (it becomes `production` only in the go-live order below); `ALERT_WEBHOOK_URL` stays empty for now. Check: `grep -cE '^(WRITES_ENABLED|STOCKTRIM_WRITES_ENABLED|SCHEDULER_ENABLED|SCHEDULER_ALLOW_PRODUCTION)=false$' .env` prints 4 and `grep -c '^ALERT_WEBHOOK_URL=.' .env` prints 0. Then `chmod 600 .env`.

**6. Validate the configuration.** `docker compose config -q` (no output = ok), then the Caddyfile with the pinned image:
`docker run --rm -e DOMAIN=<domain> -e ACME_EMAIL= -v "$PWD/docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2.10.2 caddy validate --config /etc/caddy/Caddyfile`.

**7. Build ONE image at a time, watching memory** (`free -h` in a second shell; the running stack keeps serving while you build):
```
GIT_SHA=$(git rev-parse --short HEAD) docker compose build migrate     # the backend image (app reuses it)
docker compose build web                                               # the Node build: the memory-hungry one
```
If a build is OOM-killed: `docker builder prune -f`, check swap is on (`swapon --show`), and run the failed build again alone.

**8. Cutover.**
```
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --remove-orphans
docker compose up -d --force-recreate caddy      # REQUIRED: the Caddyfile is a single-file bind mount and git replaces its inode, so the running Caddy keeps the old file
```

**9. Verify.**
```
docker compose ps -a                              # db healthy, migrate exited 0, app healthy, web healthy, caddy up
docker compose logs migrate --tail 20             # applied cleanly
docker compose exec app alembic current           # = 0012 (head)
docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh    # re-apply the grants (idempotent)
docker compose logs app caddy web --tail 50       # no tracebacks
```
`docker compose exec -T app python -m app.cli GET /qwc | sha256sum` equals the hash from step 0.

**10. First admin** (needs a TTY, so NO `-T`): `docker compose exec app python -m app.cli users add <name>`. Every signed-in user may start a cycle and change the schedule (there are no roles): add only people who may.

**11. Smoke matrix** (from any machine; `D=https://<DOMAIN>`). Run the matrix from Public exposure (every line must match), plus the SPA checks: `$D/` is 200 `text/html` with the SPA CSP, `$D/some/spa/route` is 200 (SPA fallback), `$D/assets/<a hashed file from index.html>` is 200 and immutable. Then a non-mutating SOAP call (no credentials involved):
```
curl -s -H 'Content-Type: text/xml; charset=utf-8' -H 'SOAPAction: "http://developer.intuit.com/serverVersion"' \
  -d '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><serverVersion xmlns="http://developer.intuit.com/"/></soap:Body></soap:Envelope>' "$D/qbwc"
```
Do NOT send a wrong-credential `authenticate`: it records `last_qbwc_auth_refused` and the UI would show a refused login that never happened. Then sign in in a browser (no CSP violations in the console), check Overview, Settings (flags all off, the new `version`) and Operations.

**12. The Web Connector path.** Tell the client's IT to proceed (Update Selected once, then Auto-Run as before). Watch `docker compose logs -f app`: a successful authenticate sets `last_qbwc_contact` (Overview shows it). A refused login (`last_qbwc_auth_refused`, QBWC "authentication failed") means the password in Web Connector differs from `QBWC_PASSWORD`; do not change the server first, compare the two.

**13. Rollback.** Never roll the code back alone: the database is at `0012` and older code does not know its tables. Restore the backup from step 2, then the matching code:
```
cd ~/quickbook && set -a && . ./.env && set +a
docker compose stop app
docker compose exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner < ~/quickbook-backups/pre-ui-<date>.dump
docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh
git checkout <the commit from step 1>          # code that matches the restored database
GIT_SHA=$(git rev-parse --short HEAD) docker compose build migrate && docker compose up -d --remove-orphans && docker compose up -d --force-recreate caddy
```
Restore `.env` from the backup copy if you changed it. Caddy-only problem (bad Caddyfile): `git checkout <commit from step 1> -- docker/caddy/Caddyfile && docker compose up -d --force-recreate caddy` (no database restore needed).

### Go-live order and the writes-flag policy

Each flag change: edit `.env`, then `docker compose up -d app` (about 30 s of `/qbwc` downtime: do it outside the client's sync times).
1. ALL flags stay off (`WRITES_ENABLED`, `STOCKTRIM_WRITES_ENABLED`, `SCHEDULER_ENABLED`, `SCHEDULER_ALLOW_PRODUCTION` = false, `STOCKTRIM_ENVIRONMENT=test`) until the QuickBooks snapshot is complete (all read types `done`) and a UI pre-flight (Operations) against the TEST account is clean.
2. Set `ALERT_WEBHOOK_URL` and verify an alert really arrives (an aborted cycle, e.g. a read timeout, must post to the webhook; Settings shows `alert_webhook_configured`).
3. Production target switch: `STOCKTRIM_ENVIRONMENT=production` plus the production `STOCKTRIM_BASE_URL` and credentials. Run the pre-flight until `ok_to_push`; if it says `stocktrim_not_empty`, a person decides deliberately (never overwrite).
4. First manual, supervised INITIAL run from the CLI of ALL SIX steps (`suppliers`, `products`, `bom`, `customers`, `sales_orders`, `purchase_orders`): a dry-run first (review the plan), then the real run with both fingerprints (see Push engine). This needs `STOCKTRIM_WRITES_ENABLED=true`.
5. Keep `STOCKTRIM_WRITES_ENABLED=true` and use `SCHEDULER_ALLOW_PRODUCTION=false` as the single gate. Set `SCHEDULER_ENABLED=true` and review 3-5 days of `dry_run_only` cycles (reason `production_not_allowed`) in the UI.
6. Only then `SCHEDULER_ALLOW_PRODUCTION=true`.

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
- **Security**: tunnel gate (`app/qbwc/gate.py`): through a proxy only `/qbwc` and `/api/v1/*` are reachable (and within `/api/v1` only the `public` routes, see Public exposure); every other path (`/docs`, `/openapi.json`, CLI-only routes) returns 404 unless the call is direct loopback with no `X-Forwarded-*`/`Forwarded` headers. Admin API additionally protected by `X-Admin-Token`; QBWC credentials compared in constant time; no CORS (no browser client); passwords are never logged.

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

## Push foundation (Phase 2, stage 8)
- Tables (migration `0003`; `0004` adds run status `done_with_failures` and `push_item.ref`, the QuickBooks ListID): `bom_edge` (the edge set last pushed, the BOM diff source, since StockTrim cannot list edges), `push_run` (one row per run: status, options, pre-flight fingerprint, counts-only summary) and `push_item` (per-item progress: entity, key, status, attempts, short `error_code`; never names or response bodies). This per-item progress is the plan's `sync_progress`. The `qb_app` role gets DML on them through the default privileges set by `docker/db/init-app-role.sh` (verified by a test: DML yes, DDL no). On an existing volume whose role was created before these tables, re-run that script only if the default privileges are missing.
- `app/sync/mappers.py`: pure domain -> StockTrim request mappers and `build_push_plan` (ordered `(key, request)` pairs: suppliers, products, BOM edges children-first, customers). Product category = the top-level QuickBooks group of `FullName` (omitted for top-level items); emails with several addresses use the first valid one; address lines equal to the vendor/company/customer name are dropped; `stockOnOrder` is never sent.
- `NEGATIVE_STOCK_POLICY=as_is|clamp` (default `as_is`): `clamp` sends 0 for negative stock; the mapper reports `stock_adjusted`; the push engine (next stage) owns the fallback when StockTrim rejects negatives.
- `app/stocktrim/limiter.py`: one process-wide rate limiter per StockTrim base URL, shared by every `StockTrimClient` (a later, lower `STOCKTRIM_MAX_RPS` tightens it, never loosens, with one warning; `defer(seconds)` backs off after a 429).
- `STOCKTRIM_ENVIRONMENT=test|production` (default `test`): with the base URL and a hash of the auth id it forms `stocktrim_target(settings)`, stored in `bom_edge.target` and `push_run.target` so test and production ledgers never mix. **Production deployments must set `STOCKTRIM_ENVIRONMENT=production`.** `push_run` allows one `running` row (partial unique index), carries a heartbeat `updated_at` and `requested_by`; status/entity values are CHECK-constrained (constants in `app/models.py`). A BOM edge key is `bom_edge_key(parent, component)` (JSON array). Client-facing behaviours: `docs/client-behaviours.md`.

## Push engine (Phase 2, stages 9-13: suppliers, products, BOM edges, customers, incremental sync, sales orders, purchase orders)
Code: `app/sync/engine.py`, `app/api/v1/push.py`. Steps (`suppliers`, `products`, `bom`, `customers`, `sales_orders`, `purchase_orders`) always run in that order whatever the order in the request.
- **Gates for a real run:** `STOCKTRIM_WRITES_ENABLED=true` (default false; 403 otherwise), the pre-flight is re-run at start and must be `ok_to_push` (so the StockTrim account is empty, or in sync mode, see below), and its `fingerprint` must equal the `preflight_fingerprint` you pass, and you must pass the dry-run's `plan_fingerprint` too (both come from the dry-run response; `plan_fingerprint` is required, 422 otherwise). The run thread rebuilds the plan and, if it no longer matches `plan_fingerprint` (QuickBooks data changed since the dry-run), the run ends `failed` with `summary.error = plan_fingerprint_mismatch` before a single `push_item` or StockTrim write. One active run at a time (in-process lock + the partial unique index on `push_run`): a second start gets `409 push_running`.
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
- **Order and rules:** suppliers (chunks of 50 through the array endpoint; a chunk that fails is retried supplier by supplier to isolate the bad record), then products (one upsert POST per product, 4 concurrent, shared rate limiter). Each item is marked `done` in its own short transaction right after the 200. A product whose supplier is not `done` in the run is pushed WITHOUT the supplier link (`supplier_link_dropped`): StockTrim would auto-create an unknown supplier. A supplier counts as done if it is in the `pushed_item` ledger of the same StockTrim target (this run or any earlier one), so a products-only run after a suppliers run keeps the links. On a `resume`, products that were pushed without a link while their supplier is now `done` are re-pushed WITH it. A 429 defers the shared limiter, so every worker slows down.
- **Cancel / resume / restart:** cancel is honoured within a chunk; unstarted items stay `pending`. `resume` keeps the run id, needs the same StockTrim target and the same recomputed `plan_fingerprint` (else `409 target_mismatch` / `plan_fingerprint_mismatch`), skips `done` keys, retries `pending`/`failed`/`skipped(aborted)`, accepts the statuses `interrupted`, `cancelled`, `failed` and `done_with_failures`, and does NOT need an empty account. A fatal error (bad credentials, StockTrim unavailable) stops the run with status `failed` and marks every not-attempted item `skipped` + `aborted`. At app start every `running` run is marked `interrupted` (resume it). On a clean shutdown (SIGTERM; the app service has `stop_grace_period: 30s`) the active run is cancelled and the app waits up to 8 s for its worker, so the run ends `cancelled`, resumable.
- **Negative stock (A13):** sent as-is. Observed live (TEST account): StockTrim stores negative `stockOnHand` unchanged (`docs/stocktrim-api-notes.md`). If it ever answers a 400 naming the on-hand field, the item is retried once with 0, `negative_stock_rejected` (count + ListIDs) goes to the summary and the rest of the run is clamped. After the products step every negative item and a deterministic sample of 25 other products are read back by readable code and compared with what was sent: `negative_stock_stored_as_is`, `negative_stock_silently_altered` and per-field mismatch counts are in the summary.
- **Measured (TEST account, 2026-11-16):** 382 suppliers + 2,346 products at 5 rps took about 9.5 minutes wall clock including a cancel and a resume; read-back found only rounding differences (StockTrim stores cost, price and stock on hand with 2 decimals, half-up). Since the review fixes the mapper rounds these three half-up to 2 decimals before sending (negative ties round away from zero: an unproven assumption), and the read-back compares exactly.
- **BOM step (stage 10):** the planned edges (readable-code keys, children's BOMs before their parents') are diffed against the `bom_edge` rows of the target: a new edge or one with a changed quantity becomes a POST (an upsert by pair), an edge that is stored but no longer planned becomes a DELETE with BOTH readable codes (a 404 counts as success: already gone, or the first try did the delete). One `push_item` (entity `bom_edge`, key = `bom_edge_key(parent, component)`, ref = the parent's ListID) per call, 4 concurrent in chunks of 50 through the shared rate limiter, so an unchanged second run makes 0 calls. An edge is attempted only if BOTH its products are `done` for the target (in this run or any earlier real run); otherwise it fails with `dependency_failed` and the next resume retries it. The `bom_edge` row (quantity) is written in the SAME transaction that marks the item `done`, right after the 200, so the ledger never disagrees with the progress table; a row's `parent_hash` is stamped only once every planned edge of that parent is stored with its planned quantity (every POST or DELETE of an edge first blanks the hash of the whole parent in the same transaction, so a parent with a failed sibling stays unstamped). A plain DELETE 404 is counted as `summary.bom_delete_404`. A summed quantity of 1e14 or more (float exactness) is dropped with a `nonpositive_quantity` warning like a bad quantity. Quantities are rounded half-up to 5 decimals first, like StockTrim stores them. The dry-run reports `bom_to_post` / `bom_to_delete`. First load: 4,063 POSTs, about 14 minutes at 5 rps.
- **Customers step (stage 10):** one `PUT /api/Customers` per active, valid customer (code = QuickBooks ListID; only the mapper's `.ok` list), `push_item` entity `customer` (key = ref = ListID), chunks of 50. The probe showed PUT is an UPSERT BY CODE (case-insensitive, full replace), so it is called like the POSTs: transient failures are retried, a lost response is harmless, and a resume simply skips the `done` keys and re-sends the rest. There is therefore NO `unknown_outcome` reconciliation (that would be needed for a non-idempotent PUT: one `GET /api/Customers` and a comparison of the codes). About 13,588 PUTs, roughly 45 minutes at 5 rps. There is no customer DELETE in the API: every pushed customer can only be removed in the StockTrim UI.
- **Modes (stage 11):** `POST /push` takes `mode`: `auto` (default) = `initial` while the target has no completed (`done`, non-dry-run) run, else `sync`; `initial` = push every planned record (strict gate: the account must be empty; once the target has a baseline, `suppliers`/`products`/`customers` answer `409 step_already_pushed`; only `bom` is diffed in every mode); `sync` = the incremental run below (`409 no_completed_initial_run` when the target has no baseline). `push_run.options.mode` and `summary.mode` record it.
- **Incremental sync:** the ledger `pushed_item(target, entity, key, ref, payload_hash, last_run_id, pushed_at)` (migration `0005`; entities supplier, product, customer; BOM keeps `bom_edge`) is upserted in the SAME transaction that marks the `push_item` `done`. A sync run builds the plan and diffs it against the ledger with `payload_hash` = sha256 of the exact request payload (the mappers already round to StockTrim's precision, so there are no phantom diffs): `new` (not in the ledger), `changed` (hash differs or is NULL), `unchanged` (no item, no call), `requeued`, `vanished`. A product pushed without its supplier link (or clamped) is stored with the hash of what was actually SENT (never equal to the plan), so the next sync re-posts it with the link and the hash converges; NULL means only 'backfill/unknown' (the only thing `stamp_only` adopts). `summary.sync.products.inexact` counts the ledger products whose last push was inexact (and the dry-run shows them as `changed`). Keys are compared by their NORMALISED form, like StockTrim does: a case-only key rename is `changed` (the ledger row is replaced, never new + vanished) and a BOM edge whose normalised pair is still planned is never DELETEd. The dry-run returns per step `new/changed/unchanged/requeued/vanished` (+ `vanished_list_ids`), `plan.bom_to_post/bom_to_delete`, `planned_calls`, `eta_seconds` and, with foreign records, `verdict.foreign_hash`. The `plan_fingerprint` of a sync run covers exactly the items that WILL be pushed (keys + payload hashes), so the dry-run -> real-run handshake works; a resume re-checks it against the run's own items.
  ```
  echo '{"steps": ["suppliers", "products", "bom", "customers"]}' | docker compose exec -T app python -m app.cli POST /push --json     # dry-run, auto mode
  echo '{"steps": [...], "dry_run": false, "preflight_fingerprint": "<..>", "plan_fingerprint": "<..>"}' | docker compose exec -T app python -m app.cli POST /push --json
  ```
- **Vanished keys** (in the ledger, no longer planned): suppliers, customers and products are REPORTED only (counts + QuickBooks ListIDs in `summary.sync`), never deleted. Inactive items stay in the plan (`discontinued=true`); a product that disappears entirely from QuickBooks is rare: follow-up = mark it `discontinued` in StockTrim. BOM edges that left the plan are DELETEd (stored edge set).
- **Re-queue after a wipe:** the sync gate's pre-flight lists what StockTrim holds. A ledger product / supplier / customer that is missing there is treated as new again (`requeued` count), and when a product is re-queued the stored `bom_edge` rows whose parent or component is that product are dropped, so those edges are re-POSTed (edges cannot be listed, so the stored set cannot detect a wipe by itself). The re-queued rows leave the ledger in the same transaction that lays out the items. A sync run that leaves out a step whose entities went missing from StockTrim is refused: `409 requeue_requires_step` with `detail = {code, steps: [...]}` (a bom-only run with a wiped product, a products-only run with a wiped supplier): add the named steps. An empty or failed StockTrim listing looks exactly like a wipe, so a step that would re-queue more than max(25, 5% of its ledger rows) is blocked with `409 mass_requeue` (`detail.requeued` = count per step) unless the request carries `acknowledge_requeue` = `{"customers": <the count>}` per such step (typed field, exact counts; dry-run and real run alike). A scheduler must never pass it. This replaces the manual reset SQL.
- **Mass delete:** BOM edges to DELETE (stored for the target but no longer planned) above max(25, 5% of the stored edges) are blocked with `409 mass_delete` (`detail.to_delete`; an empty assemblies read looks like this) unless the request carries `acknowledge_delete` = `{"bom": <the count>}` (dry-run and real run alike; the scheduler never passes it).
- **Sync gate:** the account may hold what we pushed: every StockTrim product (by productId), supplier and customer must be in the ledger for this target (comparison-normalised) or be a documented probe record (`PROBE_CODES` in `app/preflight/stocktrim_inventory.py`, currently only the customer `zz-probe-cust`, honoured ONLY when `STOCKTRIM_ENVIRONMENT=test`). Anything else is foreign and blocks (`stocktrim_not_empty`; a blank customer code is always foreign), unless the request carries `acknowledge_foreign` = the `verdict.foreign_hash` returned by the dry-run (sha256 of the sorted list of `entity:normalised-key` of every foreign record, computed server-side; a wrong or stale hash is `409 acknowledge_foreign_mismatch`). Purchase orders block the INITIAL load but only warn in sync mode (`purchase_orders_present`, count): the client's first PO must not stop every sync. A foreign record that shares a planned key is counted as the warning `foreign_in_plan` (count only) and the count is part of the foreign hash: acknowledging it means those foreign records are OVERWRITTEN (adopted) by our push, and an acknowledgement with a non-zero `foreign_in_plan` is refused in production (`409 foreign_in_plan_production`). Details:
  - Only `POST /push` runs in sync mode. `POST /api/v1/preflight` stays strict and reports `stocktrim_not_empty` after a first load.
  - A `done_with_failures` (or `failed`/`cancelled`) run does NOT establish sync mode: resume it until it is `done`. Dry-runs never establish it either.
  - A record that StockTrim stored but we did not mark `done` (crash after the 200; the ledger write and `done` are one transaction) counts as foreign until its run is resumed.
  - BOM edges added by hand in StockTrim are invisible to us (the API cannot list them): they are neither diffed nor deleted.
  - Customers: only CHANGED customers are PUT (a PUT is a full replace), which narrows the overwrite of hand edits in StockTrim to customers that changed in QuickBooks.
- **Baseline migration cost and `stamp_only`:** migration `0005` backfills the ledger from the done push items with a NULL hash, so the first sync run would re-send everything once (about 70 minutes on the full data set; every write is a proven upsert, nothing is lost). To avoid that when you KNOW StockTrim holds exactly what the earlier runs pushed with the same mappers, adopt the current state as the baseline without any StockTrim call. Dry-run first, then the real call with the two fingerprints it returned (the real call needs `STOCKTRIM_WRITES_ENABLED=true` and an ok sync pre-flight):
  ```
  echo '{"steps": ["suppliers", "products", "customers"], "stamp_only": true}' | docker compose exec -T app python -m app.cli POST /push --json
  echo '{"steps": ["suppliers", "products", "customers"], "stamp_only": true, "dry_run": false, "preflight_fingerprint": "<from the dry-run>", "plan_fingerprint": "<from the dry-run>"}' | docker compose exec -T app python -m app.cli POST /push --json
  ```
  It writes the plan's payload hashes into the NULL-hash ledger rows only (a row with a real hash, including an inexact row, is left alone: a genuinely changed record stays `changed`) and it records an audit `push_run` with `options.mode = "stamp"` (a stamp is never a baseline for `mode=auto`). Guards: only with `STOCKTRIM_ENVIRONMENT=test` (`409 stamp_test_only`); refused when the QuickBooks snapshot is newer than the last completed real run (`409 stamp_snapshot_newer`: stamping would hide those differences); refused when any ledger key is missing from StockTrim (`409 stamp_requires_all_present`); its `plan_fingerprint` includes the mode, so a sync dry-run's fingerprint cannot approve a stamp and vice-versa. Stamp only when the snapshot is what was pushed, with the same mappers (see 'Clearing values' below: the Stage 11b mapper change alters supplier and category hashes).
- **Applying migration `0005` (or any later one) on the dev stack:** `docker compose -f docker-compose.yml -f compose.dev.yml run --rm migrate` (the SAME `-f` flags as the stack, so it uses the same services and networks; `migrate` is the one-shot `alembic upgrade head` as the DB owner).
- **Resume after the snapshot changed:** `resume` re-checks the plan fingerprint of the run's own items, so if QuickBooks data changed since the run was approved the answer is `409 plan_fingerprint_mismatch`. The clean path is a NEW sync run (dry-run, then real run): the items that were already `done` are in the ledger with their hashes and are not sent again.
- **Clearing values (Stage 11b):** a value cleared in QuickBooks is cleared in StockTrim where the API allows it. Probed on the TEST account (docs/stocktrim-api-notes.md section 3a): an explicit empty string `""` clears the supplier email, contact, address lines, state, post code and country and the product category; `null` is ignored everywhere. The supplier and product mappers therefore send `""` for those fields when QuickBooks has no value; fields that cannot be cleared safely (product cost, price, minimum/maximum shelf level: only `0`, a real value that changes planning; the product's supplier link: `supplierCode ""` leaves the `suppliers` list) stay omitted. The payload hash is of what is sent, so the change altered the hash of every supplier with a missing field and of every product without a category: the first sync after it shows them as `changed`.
- **Hash version:** no separate hash-version constant. The hash is of the exact payload, so a mapper or DTO change re-sends precisely the records whose payload changed; a constant prefix would only force a full re-send of everything (including all customers, a full-replace PUT that overwrites hand edits) and is better done deliberately with `stamp_only`/a reset.
- **Emergency reset** (not needed after a wipe any more; sync re-queues). WARNING: deletes the history and ledgers of ONE target; double-check the target string and never run it against production unless that account was wiped too:
  ```
  docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM pushed_item WHERE target = '<target>'; DELETE FROM push_run WHERE target = '<target>'; DELETE FROM bom_edge WHERE target = '<target>';"
  ```
  (`push_item` rows go with their run; `<target>` is `push_run.target`, see `SELECT DISTINCT target FROM push_run`.) The next run on that target is a first run again (strict).
- **Sales-order and purchase-order steps (stage 13):** `{"steps": ["sales_orders", "purchase_orders"]}`, same dry-run -> real-run handshake and the same incremental diff (ledger entities `sales_order_line` and `purchase_order`, migration `0006`; an unchanged run makes 0 calls; the ledger ref is the order's TxnID). `stockOnOrder` is never sent on products: stock on order comes from the PO records only.
  - **Sales orders:** one StockTrim record per order LINE of the orders dated within the last `SALES_HISTORY_YEARS` years (default 3; `.env`/compose/`Settings.sales_history_years`). `productId` = the item Name (SKU key, as pushed); lines whose item is not a pushed product (shipping, discounts, fees, non-inventory, unknown) are skipped, as are lines inside bundles (`SalesOrderLineGroupRet`) and lines with a zero or negative quantity. Quantity = the ordered quantity, except for manually closed orders where it is the line's `Invoiced` quantity (0 -> skipped), because closing cancels the unshipped remainder. `externalReferenceId` = `TxnID:TxnLineID` (StockTrim upserts on it: a retry never duplicates), `orderDate` = TxnDate at midnight UTC, `unitPrice` = Rate (2 decimals), `customerCode` = the customer ListID (only if that customer is a pushed one: StockTrim answers 400 for an unknown customer). No location. Sent through `POST /api/SalesOrdersBulk` (probed 2026-11-17, `docs/stocktrim-api-notes.md`): up to 50 lines of ONE customer per call (only the call's header customer counts); a rejected call falls back to one `POST /api/SalesOrders` per line. Dry-run `plan.sales_orders`: `orders`, `lines`, and skipped `non_product`, `group`, `zero_quantity`, `closed_uninvoiced`.
  - **Purchase orders:** ALL QuickBooks POs (no date window), one `POST /api/PurchaseOrders` each, identity `clientReferenceNumber` = `QB-<TxnID>`, `referenceNumber` = RefNumber (RefNumber-TxnNumber, else RefNumber- plus the first 8 characters of the TxnID, when several POs in the snapshot share that RefNumber, compared casefolded and without trailing spaces: StockTrim merges POs with the same referenceNumber), `orderDate` = TxnDate. Status: open -> `Sent` with the REMAINING quantity per line (Quantity - ReceivedQuantity, lines at 0 dropped); fully received, or manually closed -> `Received` with the full ordered quantity. Lines are summed per product (StockTrim merges lines by productId), `unitPrice` = Rate, `etaDate` = ExpectedDate. A PO whose vendor is not a pushed supplier is skipped (StockTrim would auto-create the supplier), as are POs without a pushable line. A PO that changes state in QuickBooks is simply re-sent with the new status (its payload hash changed). Dry-run `plan.purchase_orders`: `sent`, `received` (POs to send), `vendor_not_pushed`, `no_lines` (POs skipped), `lines_non_product`.
  - **Sync gate:** POs in StockTrim whose clientReferenceNumber is in the ledger are ours and do not count; any other PO stays the `purchase_orders_present` warning in sync mode (blocking in the initial load).
  - ETA at 5 rps: sales-order bulk calls (about one per customer with orders plus extras) plus one call per PO.
- **Error codes (closed set, `engine.ERROR_CODES`):** rejected_400/404/409/other, auth, unavailable, unknown_outcome, aborted, negative_stock_rejected, supplier_link_dropped, dependency_failed, cancelled, internal.

## Scheduler and health (Phase 2, stage 12)

One cycle per day pulls fresh QuickBooks data and then syncs it to StockTrim, without anyone clicking. **Off by default** (`SCHEDULER_ENABLED=false`).

- **Enable:** set in the root `.env` (all are in `.env.example` and `docker-compose.yml`): `SCHEDULER_ENABLED=true`, `SYNC_DAILY_AT=02:00` (HH:MM) in `SCHEDULER_TZ` (IANA name, default `UTC`), then `docker compose up -d app`. Other settings: `SCHEDULER_READ_JOBS` (default customers, vendors, items, other_items, assemblies, sales_orders, purchase_orders; `sites` is unsupported on this client), `SCHEDULER_READ_TIMEOUT_HOURS=6`, `SYNC_MAX_PLANNED_CALLS=2000` (about 3x a normal daily volume: new keys are otherwise unguarded; raise it only for a supervised run; a manual push is never capped), `SCHEDULER_ALLOW_PRODUCTION=false`, `ALERT_WEBHOOK_URL` (optional Slack-compatible webhook; the URL is a secret).
- **Why it needs the client's Web Connector:** QuickBooks Desktop is only reachable when the Web Connector connects to us (Auto-Run on the client's PC, every `RUN_EVERY_N_SECONDS`). The cycle queues the read jobs and waits for that connection; with no connection it times out. The first real run therefore needs the public domain and the client's connector.
- **One cycle:** (a) queue the read jobs through the normal job queue (a type still queued/running from an earlier cycle is waited for, not queued twice); (b) wait, polling every 30 s, until all are done; (c) freshness guard: every pulled entity must have been rewritten since the cycle started; (d) a `sync`-mode dry-run of ALL steps, automatic checks, then the real run with the dry-run's two fingerprints; the cycle waits for the push run and records it. The scheduler never sends `acknowledge_foreign`, `acknowledge_requeue`, `acknowledge_delete` or `stamp_only` (its runs carry `requested_by=scheduler`): a human handles those.
- **Cycle result** (`sync_cycle` table, `GET /health`): `done`; `dry_run_only` (the checks passed but the real run is not allowed: `writes_disabled` = `STOCKTRIM_WRITES_ENABLED` false, `production_not_allowed` = `STOCKTRIM_ENVIRONMENT=production` without `SCHEDULER_ALLOW_PRODUCTION=true`); `aborted` with a reason: `push_running` (a manual push is running; checked before the dry-run), `empty_entity` (a read type has no rows while its ledger has rows: an empty read looks like a wipe), `mass_vanish` (a step's vanished count above max(25, 5%)), `mass_delete` (engine: BOM DELETEs above max(25, 5% of the stored edges)), `lock_lost` (the advisory-lock connection died before the real run), `read_job_failed` (a read job ended error/unsupported), `read_timeout`, `stale_snapshot`, `no_completed_initial_run` (the first push is a manual initial run), `mass_requeue`, `requeue_requires_step`, `foreign_records`, `preflight_not_ok` (blocking findings), `too_many_calls` (planned calls over the cap), or another engine code (`push_running`, ...); `failed` (`push_done_with_failures`, `push_failed`, or an exception type); `interrupted` (shutdown, restart or `stale`: a `running` row found while holding the lock). Every non-`done` result writes an ERROR log line and, with `ALERT_WEBHOOK_URL`, posts `{text, cycle_status, reason}` (5 s timeout; a failed post is only logged). Reasons, summaries and alerts hold codes and counts only.
- **One cycle at a time:** a Postgres advisory lock plus a unique index on a `running` cycle; a `running` row left by a crash is marked `interrupted` (at startup and whenever a cycle starts holding the lock). If the process was down at the daily slot, the cycle runs at startup when no cycle started since the slot and it is within `SCHEDULER_READ_TIMEOUT_HOURS` of it. Alerts add `environment`, `cycle_id` and, for a failed push, `push_error`. Shutdown stops the scheduler thread (the cycle ends `interrupted`) before the push run is cancelled (resumable).
- **Manual trigger** (any signed-in UI user, or the CLI; 202; 409 `cycle_running` while one runs; 403 when the scheduler is disabled): `docker compose exec app python -m app.cli POST /scheduler/run-now`. A timed-out cycle leaves its read jobs queued: they run when the Web Connector next connects, and the next cycle reuses them.
- **Health:** `docker compose exec app python -m app.cli GET /health` (add `?probe=true` for one read-only StockTrim GET): database, scheduler state and next run, `last_qbwc_contact` and its age (written on every SUCCESSFUL Web Connector authenticate, at most once a minute; refused logins go to `last_qbwc_auth_refused`), the last cycle, queued/running QuickBooks jobs, per-entity snapshot `synced_at_max` and record counts, the latest push run (status, item counts), the writes flag and the environment.
- Simplifications: one cycle every 1, 3 or 7 days (no hourly stock sync yet), an entity that is empty in QuickBooks is not checked by the freshness guard, alerts go to a webhook only (no email).

## Sync schedule (stage B5)

The schedule is stored in Postgres (`app_state` key `schedule`, no table) so the admin UI can change it: **every 1, 3 or 7 days** at a **time** in a **time zone**, switched on or off. `SYNC_DAILY_AT` / `SCHEDULER_TZ` are only the default: with no stored schedule the effective one is "every 1 day at SYNC_DAILY_AT in SCHEDULER_TZ" (`source: env_default`, nothing is written until the first PUT).

- **Anchor:** runs happen on `start_date + k * every_days` at `time` in `tz`, independent of earlier cycles and of restarts. `start_date` defaults to today in `tz` on save; if that day's slot has already passed, the first run is the next slot (a saved schedule never fires a slot from before it was saved). Example: every 3 days from Monday 2026-11-16 at 02:00 runs Mon 16, Thu 19, Sun 22, Wed 25 Nov, and so on. Weekly = 7 days from the anchor's weekday.
- **DST:** a local time that does not exist that day (spring forward) runs at the shifted instant (02:30 becomes 03:30); an ambiguous one (fall back) runs at its first occurrence, once.
- **Restart safety:** at startup the loop runs one cycle at once if the latest slot is not older than `SCHEDULER_READ_TIMEOUT_HOURS`, no cycle started since it, and the schedule was saved before it.
- **Two switches:** `SCHEDULER_ENABLED` (env) is the ceiling: false = no automatic cycle at all, whatever is stored (the UI shows "disabled on the server"; `ceiling_enabled` in the API). The stored `enabled=false` only pauses the automatic runs. Manual run-now is unaffected by the stored switch (it needs the ceiling, as before). `SCHEDULER_ALLOW_PRODUCTION` and every safety check are unchanged.
- **Applies at once:** the loop re-reads the schedule at every wake-up (at most 60 s apart) and a PUT wakes it immediately; no restart.
- **API** (Bearer, reachable through the proxy; extra fields are rejected): `GET /schedule` returns the effective schedule (`start_date` is `null` for the env default and for daily schedules: the anchor means nothing there), `source` (`database`|`env_default`), the next 3 runs (`utc` and `local` ISO), `ceiling_enabled`, `allow_production` and the last cycle (id, status, started_at). `PUT /schedule` takes `{"enabled": true, "every_days": 3, "time": "02:00", "tz": "America/Chicago", "start_date": "2026-11-16"}` (`start_date` optional: a past date of any age is fine (it only sets the phase of the `k * every_days` slots), a future one at most 365 days ahead (`422 start_date_out_of_range`); `time` strictly `HH:MM`; `tz` an IANA name), stores it with `updated_by` = the signed-in user, writes a `schedule_changed` run event (counts only: `every_days`, `enabled`) and returns the same body as GET. `/health`, `/overview` and `/settings` show the stored schedule (`scheduler.time`, `every_days`, `tz`, `schedule_enabled`, `source`, `next_run_at`; `/settings.schedule`).
- **CLI, for emergencies** (inside the container; the PUT is recorded as actor `cli`; not possible through the proxy):

```
docker compose exec -T app sh -c "echo '{\"enabled\": false, \"every_days\": 1, \"time\": \"02:00\", \"tz\": \"UTC\"}' | python -m app.cli PUT /schedule --json"
```

  Back to the env default: `DELETE FROM app_state WHERE key = 'schedule';` in psql.
- Migration 0012 only widens the `run_event.code` check constraint for `schedule_changed`.

## Run log (stage B3)

What happened in a cycle or run, and which records were left out and why, is stored in Postgres (migration 0011); the read endpoints are in the Read APIs section and the UI in `frontend/README.md`. Codes, counts and business keys only: never names, payloads, API response bodies or secrets.

- **`run_event`:** phase events of a cycle (`cycle_started`, `reads_queued`, `waiting_for_web_connector` (at most one per 15 minutes), `reads_done`, `snapshot_fresh`, `dry_run_planned`, `check_failed:<reason>`, `real_run_started`, `cycle_finished`) and of a push run (`run_started`, `step_started`/`step_finished` with item counts, `run_finished`), plus the admin actions `preflight_requested`, `run_cancel_requested`, `run_resumed`. `actor` = the signed-in username, `cli` (the loopback admin token) or `scheduler`; the same value is `push_run.requested_by`.
- **`push_exclusion`:** per run (dry-runs too), one row per record the plan leaves out: `entity`, business `key`, QuickBooks `ref` (ListID/TxnID) and a `code` (quarantined keys, inactive suppliers/customers, a dropped supplier link, skipped BOM lines, and the skipped order lines / purchase orders: `non_product`, `group`, `zero_quantity`, `closed_uninvoiced`, `vendor_not_pushed`, `no_lines`, `lines_non_product`). For a record that failed to parse, and for BOM issues, the ListID stands in for the key. Their counts equal the pre-flight issue counts and the plan's `sales_orders`/`purchase_orders` stats.
- **`push_item.action`** (`upsert`|`delete`; a vanished BOM edge is the only delete) and `created_at`.
- **`app/reasons.py`:** the catalog of every code above, every push item error code, issue code, status and reason, as title / explanation / next step in plain English. `tests/test_reasons.py` fails when a code that can be emitted has no entry.
- **Retention** (applied after every scheduler cycle, or by hand: `docker compose exec app python -m app.cli prune`, which prints the counts deleted):

| What | Kept | Exceptions |
|---|---|---|
| `run_event` | `HISTORY_RETENTION_DAYS` (default 90) | none |
| dry-run `push_run` (with items) | 30 days | the 3 below |
| real `push_run` (with items) | 180 days | the 3 below |
| `push_exclusion` rows | only for the newest 3 runs per StockTrim target (dry and real together; `EXCLUSION_KEEP_RUNS`), because consecutive runs repeat the same rows (~34k per run on the dev data) | a resumable run keeps its rows |

  Never deleted by age: a resumable run (`interrupted`/`failed` with pending items), the latest run per target, and the latest completed real run per target (the sync baseline). Exclusions of an older run that is kept are removed; its run row, items and events stay.

## Read APIs for the admin UI (stage B4)

GET only, all under `/api/v1`, all reachable through the proxy with a Bearer token (frozen in `tests/test_exposure.py`), typed responses. Counts, codes, timestamps and short labels only: never secrets, StockTrim credentials, payloads, raw QuickBooks records or error bodies.

| Route | What |
|---|---|
| `GET /cycles?limit<=100&before=<iso\|id>` | cycles newest first (`before` = ISO time or the id of the last row seen) |
| `GET /cycles/{id}` | the cycle, its read jobs, the linked run's progress, `reason_info` (title / explanation / next step) and `wrote_to_stocktrim` (`yes` / `no` / `unknown`, see below) |
| `GET /events?cycle_id=&run_id=&after_id=&limit<=200` | run events ascending with catalog titles; poll with `after_id=<last_id>` |
| `GET /push/runs?limit&before&dry_run` | runs newest first: mode, steps, item counts by status, requested_by, target as `<env> @ <host>` |
| `GET /push/runs/{id}/entries?outcome=&entity=&code=&q=&key=&limit<=200&offset=` | failed / skipped items and excluded records in one list, `total`, per-outcome `counts` and `entities` (the sorted entity facet: distinct entities under every filter EXCEPT `entity`, so the dropdown never loses options), reason text, StockTrim HTTP status and rejected field names. `qb_label` (FullName, Name or RefNumber) is looked up in the QuickBooks snapshot when the page is read (one query per page), never stored; null when the record is gone. `key`: an exact key match (preferred over `q` when both are sent). `q`: case-insensitive contains on key, ref and label (`%` and `_` are literal). Key/ref search is instant; a name (label) search on a very large run (~33k entries) takes a few seconds because it scans the QuickBooks snapshot (a `pg_trgm` index would fix that if it ever matters) |
| `GET /push/runs/{id}/entries.csv` | the same filters as a streamed CSV (UTF-8 BOM, cells starting with `= + - @` tab or CR get a leading `'`), at most 200,000 rows (`X-Truncated: true` when more matched) |
| `GET /settings` | non-secret settings (StockTrim host only, webhook as a boolean), the read job list and the users (no hashes); `version` comes from `APP_VERSION` or `GIT_SHA` when set |
| `GET /reason-codes` | the whole reason catalog |
| `GET /overview` | one call for the dashboard: last cycle, next run, running cycle / push with phase and its catalog `phase_title` (null when unknown), last QuickBooks contact and last refused Web Connector login (`last_qbwc_auth_refused`), snapshot freshness, queued/running jobs, latest real push run (built on `/health`) |

`wrote_to_stocktrim` is a string, never a guess toward "no": `yes` when the linked real run completed at least one item; `unknown` when a real run started but its outcome is not certain (a failed item with `unknown_outcome`, `unavailable` or `internal`, the set `WRITE_MAYBE_CODES` in `app/sync/engine.py`; the run is still running; or the run is gone and the summary does not settle it); `no` otherwise (dry-run only, aborted before a real run, or a finished real run whose failures are all certain rejections). The cycle summary stores `push: {run_id, status, items: {done, failed, ...}}` for every real run (and a stub `{run_id, items: {done: 0, failed: 0}}` the moment the run starts), so after the 180-day prune `items.done > 0` still answers `yes`; the cycle also finds its run through `summary.push.run_id` when `push_run_id` could not be written.

`python -m app.cli openapi` prints the OpenAPI JSON of the proxy-reachable routes only (needs neither settings nor the database), so the frontend can generate its types from exactly what is exposed.

## Next steps

Hourly stock sync, `modified_after` incremental reads.
