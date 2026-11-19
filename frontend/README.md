# QuickBook admin UI (stage F1)

React 19 + TypeScript (strict, no `any`) + Tailwind v4 + GSAP. Nothing needs a local Node install; everything runs through Docker.

## Commands
```
docker compose --profile test run --rm --build test-web      # lint + typecheck + vitest
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build web caddy app
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
Same-origin `/api/v1`. The access token lives in memory only (`src/api/session.ts`). A refresh (HttpOnly cookie, 24 h absolute) runs on load and on a 401 `token_expired`/`invalid_token`; it is single-flight, a 401 `invalid_refresh` is retried once, and a definitive failure clears the token and sends the user to `/login` (`?next=` is honoured only for same-origin relative paths).

## Serving
`web` (nginx-unprivileged, port 8080, not published, read-only root fs) sits behind Caddy, which owns the security headers and the SPA CSP; nginx only sets `Cache-Control` (immutable for `/assets`, `no-cache` for `index.html`). `web` sits on an internal `spa` network shared with Caddy only, with all capabilities dropped. A broken SPA never stops `/qbwc`: Caddy does not depend on `web`. `npm ci` uses `.npmrc` `legacy-peer-deps=true` (npm 10 crashes resolving the vitest peer tree otherwise), so peer deps such as `@testing-library/dom` are listed explicitly.
