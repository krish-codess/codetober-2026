#!/bin/sh
# Runs once, when the database volume is first created. Creates the two login users the services
# connect as; what each may do is granted to the group roles by migration 0002.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v api_password="$UPLIFT_API_DB_PASSWORD" -v batch_password="$UPLIFT_BATCH_DB_PASSWORD" <<'SQL'
CREATE ROLE uplift_api_role NOLOGIN;
CREATE ROLE uplift_batch_role NOLOGIN;
CREATE ROLE uplift_api LOGIN PASSWORD :'api_password' IN ROLE uplift_api_role;
CREATE ROLE uplift_batch LOGIN PASSWORD :'batch_password' IN ROLE uplift_batch_role;
SQL
