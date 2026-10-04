#!/bin/sh
# First-boot bootstrap (runs once, as the postgres superuser, inside the postgres container).
# Creates the owner + least-privilege login roles. Passwords come from the environment only.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_pw="$GS_OWNER_PASSWORD" -v pipeline_pw="$GS_PIPELINE_PASSWORD" -v api_pw="$GS_API_PASSWORD" <<'SQL'
CREATE ROLE gs_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE gs_pipeline_role NOLOGIN;
CREATE ROLE gs_api_role NOLOGIN;
CREATE ROLE gs_pipeline LOGIN PASSWORD :'pipeline_pw' IN ROLE gs_pipeline_role;
CREATE ROLE gs_api LOGIN PASSWORD :'api_pw' IN ROLE gs_api_role;
ALTER DATABASE :"DBNAME" OWNER TO gs_owner;
ALTER SCHEMA public OWNER TO gs_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- Dagster run/event storage lives in its own database owned by the pipeline role.
CREATE DATABASE dagster OWNER gs_pipeline;
-- tables gs_owner creates later are readable by both roles without re-granting
ALTER DEFAULT PRIVILEGES FOR ROLE gs_owner IN SCHEMA public GRANT SELECT ON TABLES TO gs_pipeline_role, gs_api_role;
SQL
