# QuickBook admin UI (stages F1 to F5: shell, overview, runs, entries, operations, settings)

React 19 + TypeScript (strict, no `any`) + Tailwind v4 + GSAP. Nothing needs a local Node install; everything runs through Docker.

## Commands
```
docker compose --profile test run --rm --build test-web      # lint + typecheck + vitest
# deploy (one image at a time, 2 GB server; the full runbook is `Deploying the admin UI release` in backend/README.md):
GIT_SHA=$(git rev-parse --short HEAD) docker compose build migrate && docker compose build web
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --remove-orphans && docker compose up -d --force-recreate caddy
# dev overlay (https://localhost:8443): add -f docker-compose.yml -f compose.dev.yml
# ad-hoc npm (node_modules in a named volume):
docker run --rm -v "$PWD/frontend:/app" -v qb_web_nm:/app/node_modules -w /app node:22.23.5-alpine sh -c "npm ci && npm run gen:api"
```
Vite dev server: `npm run dev` proxies `/api` to `VITE_API_PROXY` (default `https://caddy`, i.e. run it on the compose `edge` network).

## Layout
`src/api` (openapi.json, generated schema.d.ts, client, in-memory session), `src/app` (shell, routes), `src/features/{auth,overview,runs,entries,operations,settings}`, `src/components` (Panel, StatusStamp, Loading, ErrorState, EmptyState, Mono, ...), `src/lib`, `src/styles/tokens.css`, `src/test`.

## API types
`src/api/openapi.json` comes from the backend (public routes only):
`docker compose exec -T app python -m app.cli openapi > frontend/src/api/openapi.json`, then `npm run gen:api` writes `src/api/schema.d.ts`. Commit both.

## Auth contract
Same-origin `/api/v1`. The access token lives in memory only (`src/api/session.ts`). A refresh (HttpOnly cookie, 24 h absolute) runs on load and on a 401 `token_expired`/`invalid_token`; it is single-flight, only a 401 `refresh_race` is retried (once, after 300 ms; every other 401 ends the session at once), and a definitive failure clears the token and sends the user to `/login` (`?next=` is honoured only for same-origin relative paths).

## Serving
`web` (nginx-unprivileged, port 8080, not published, read-only root fs) sits behind Caddy, which owns the security headers and the SPA CSP; nginx only sets `Cache-Control` (immutable for `/assets`, `no-cache` for `index.html`). `web` sits on an internal `spa` network shared with Caddy only, with all capabilities dropped. A broken SPA never stops `/qbwc`: Caddy does not depend on `web`. `npm ci` uses `.npmrc` `legacy-peer-deps=true` (npm 10 crashes resolving the vitest peer tree otherwise), so peer deps such as `@testing-library/dom` are listed explicitly.

Login and refresh return `session_expires_at` (the absolute end of the 24 h session; a refresh never moves it). `features/auth/SessionEndNotice` shows a calm notice 5 minutes before it and signs the user out like any expiry at the end.

## Screens (F2 to F4)
`/` overview (one `GET /overview` plus the last 5 cycles and, while a push runs, its progress), `/runs` (cycles, `?view=push` for push runs, `Load more` uses the `before` cursor), `/runs/:cycleId` (phase ledger, outcome, progress, event log), `/runs/push/:runId` (a push run without a cycle). `/entries` (F4) lists the failed / skipped / excluded records of one push run. All state is in the URL query: `run` (default: the latest real run), repeatable `outcome` (default failed + skipped), `entity`, `code`, `q` (debounced 300 ms), `page` (50 rows), `entry` (the detail drawer). The API has no entry id, so an entry is `source|entity|key|code` (`features/entries/params.ts`). CSV export fetches through the authenticated client and saves a Blob. Read-only: cancel and resume are CLI-only, so the pages show the command to copy. Run one test file: `docker compose -f docker-compose.yml -f compose.dev.yml --profile test run --rm test-web npx vitest run src/features/runs`.

## Operations and Settings (F5)
`/operations`: schedule (`GET/PUT /schedule`; one `ScheduleForm` draft, Save only when dirty and valid, errors mapped from the 422 body by `features/operations/scheduleForm.ts`), "Run sync now" (`POST /scheduler/run-now`, confirm `Dialog`, 202 goes to `/runs/<cycle_id>`, 409 `cycle_running` and 403 `scheduler_disabled` shown calmly), pre-flight (`POST /preflight` with the default options, read-only, report from codes and counts only) and a note with the CLI-only commands from `backend/README.md`. `/settings` is a read-only view of `GET /settings`. `ApiError.detail` carries the server's error detail (a code, or the 422 field list). Writes and the scheduler are server flags: the pages explain them, never change them.
