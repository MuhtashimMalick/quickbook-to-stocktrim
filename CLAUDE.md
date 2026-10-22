# QuickBook

## Stack
- **Frontend**: React + TypeScript + Tailwind CSS (Vite), GSAP for animation
- **Backend**: FastAPI (Python 3.12+), SQLAlchemy 2 + Alembic, Pydantic v2
- **Database**: PostgreSQL
- **Infra**: Everything runs in Docker via `docker compose`

## Layout
```
frontend/             React app (own Dockerfile, multi-stage -> nginx)
backend/              FastAPI app (own Dockerfile, non-root user)
docker-compose.yml    frontend, backend, db (+ volumes, healthchecks)
.env.example          all required env vars; never commit a real .env
.claude/agents/       supervisor, executor, frontend-designer
```

## Agent workflow
1. `supervisor` (Opus 5.5) analyzes the problem and writes the plan/tasks. It never writes code.
2. `executor` (Sonnet 5.5, `ponytail` skill) implements backend, infra, and app logic.
3. `frontend-designer` (Sonnet 5.5, `ui-ux-pro-max` + `gsap-core` skills) designs and builds the UI.
4. `supervisor` reviews the results and approves or returns a fix list.

For any non-trivial task, start with `supervisor`.

## Engineering rules
- **DRY / SOLID**; minimal code; reuse before writing; prefer popular libraries over custom code.
- **Security**: validate all input, ORM-only queries, argon2-hashed passwords, authz on every endpoint, strict CORS, secrets only via env vars, non-root containers, pinned dependencies.
- **Docker**: all commands run through compose; nothing should require a local Python/Node install. Multi-stage builds, healthchecks, `depends_on: condition: service_healthy`.
- **API**: REST, typed Pydantic schemas, versioned under `/api/v1`; derive frontend types from the OpenAPI schema where possible.
- **Frontend**: Tailwind theme tokens for all design values; reusable components; no `any`; honor `prefers-reduced-motion`; the design must avoid the generic AI-template look.
- **Tests**: pytest + httpx for backend, Vitest + Testing Library for frontend.

## Common commands
```
docker compose up --build
docker compose exec backend pytest
docker compose exec backend alembic upgrade head
docker compose exec frontend npm run lint
```
