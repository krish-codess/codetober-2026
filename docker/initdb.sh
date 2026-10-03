#!/bin/sh
# Runs once, when the PostgreSQL volume is first created. Creates the least-privilege
# role the API and runner use: DML on the result tables and TEMP for the postgres
# engine's scratch tables. No DDL, no DELETE. Migrations run as the owner.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v app_password="$TYDLC_APP_PASSWORD" <<'SQL'
CREATE ROLE tydlc_app LOGIN PASSWORD :'app_password';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO tydlc_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO tydlc_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO tydlc_app;
SQL
