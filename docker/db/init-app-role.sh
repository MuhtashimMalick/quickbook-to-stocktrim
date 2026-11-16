#!/bin/sh
# Creates the least-privilege app role (idempotent). Runs once on a fresh volume via /docker-entrypoint-initdb.d;
# on an existing volume: docker compose exec db sh /docker-entrypoint-initdb.d/init-app-role.sh
set -e
psql -v ON_ERROR_STOP=1 -v pw="$APP_DB_PASSWORD" -v owner="$POSTGRES_USER" -v db="$POSTGRES_DB" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE qb_app LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'qb_app') \gexec
ALTER ROLE qb_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'pw';
REVOKE CONNECT, TEMP ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO qb_app;
GRANT CONNECT ON DATABASE :"db" TO :"owner";
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO qb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO qb_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO qb_app;
SELECT to_regclass('public.alembic_version') IS NOT NULL AS has_av \gset
\if :has_av
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM qb_app;
\endif
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qb_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO qb_app;
SQL
