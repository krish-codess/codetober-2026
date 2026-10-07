#!/bin/sh
# Runs once, when the database volume is first created. Creates the two login users the services
# connect as; what each may do is granted to the group roles by migration 0002.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v api_password="$WRAPPED_API_DB_PASSWORD" -v batch_password="$WRAPPED_BATCH_DB_PASSWORD" <<'SQL'
CREATE ROLE wrapped_api_role NOLOGIN;
CREATE ROLE wrapped_batch_role NOLOGIN;
CREATE ROLE wrapped_api LOGIN PASSWORD :'api_password' IN ROLE wrapped_api_role;
CREATE ROLE wrapped_batch LOGIN PASSWORD :'batch_password' IN ROLE wrapped_batch_role;
SQL
