-- Control-plane state for shipd. Idempotent: run on every start, like `pgroll init`.
CREATE SCHEMA IF NOT EXISTS shipd;

CREATE TABLE IF NOT EXISTS shipd.runs (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name                text        NOT NULL CHECK (name ~ '^[a-z0-9_]{1,40}$'),
  operations          jsonb       NOT NULL CHECK (jsonb_typeof(operations) = 'array'),
  compatible_app_versions text[]  NOT NULL CHECK (cardinality(compatible_app_versions) > 0),
  settings            jsonb       NOT NULL,
  state               text        NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending','expanding','expanded','contracting','completed','reverting','reverted','failed')),
  reason              text        NOT NULL DEFAULT '',
  correlation_id      text        NOT NULL,
  tables              jsonb       NOT NULL DEFAULT '[]',
  rows_total          bigint      NOT NULL DEFAULT 0 CHECK (rows_total >= 0),
  rows_done           bigint      NOT NULL DEFAULT 0 CHECK (rows_done >= 0),
  verification        jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),
  started_at          timestamptz,
  ddl_done_at         timestamptz,
  expanded_at         timestamptz,
  contract_started_at timestamptz,
  finished_at         timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now()
);
-- Submitting the same migration twice returns the existing run instead of starting a second one.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_live_per_name ON shipd.runs (name)
  WHERE state NOT IN ('reverted','failed');
-- pgroll allows one in-flight migration per schema; enforce it where two controllers cannot race.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_in_flight ON shipd.runs ((true))
  WHERE state IN ('pending','expanding','expanded','contracting','reverting');

-- One row per guard tick (500 ms) while a migration is touching the database.
CREATE TABLE IF NOT EXISTS shipd.samples (
  run_id           bigint      NOT NULL REFERENCES shipd.runs (id) ON DELETE CASCADE,
  at               timestamptz NOT NULL DEFAULT clock_timestamp(),
  phase            text        NOT NULL CHECK (phase IN ('expand','contract')),
  blocked          integer     NOT NULL CHECK (blocked >= 0),
  max_wait_ms      integer     NOT NULL CHECK (max_wait_ms >= 0),
  migrator_wait_ms integer     NOT NULL CHECK (migrator_wait_ms >= 0),
  active           integer     NOT NULL CHECK (active >= 0),
  rollbacks_per_s  real        NOT NULL,
  rows_done        bigint      NOT NULL CHECK (rows_done >= 0),
  PRIMARY KEY (run_id, at)
);

-- Schema versions an application may connect to. `live` flips on once the backfill has finished.
CREATE TABLE IF NOT EXISTS shipd.schema_versions (
  version    text        PRIMARY KEY,
  seq        bigint      GENERATED ALWAYS AS IDENTITY UNIQUE,
  live       boolean     NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- The compatibility matrix: which application versions can run against which schema versions.
CREATE TABLE IF NOT EXISTS shipd.compat (
  app_version    text NOT NULL CHECK (app_version ~ '^[A-Za-z0-9._-]{1,20}$'),
  schema_version text NOT NULL REFERENCES shipd.schema_versions (version) ON DELETE CASCADE,
  PRIMARY KEY (app_version, schema_version)
);

-- Rows whose old value could not be represented in the new column. Kept so the contract phase drops nothing silently.
CREATE TABLE IF NOT EXISTS shipd.quarantine (
  run_id      bigint      NOT NULL REFERENCES shipd.runs (id) ON DELETE CASCADE,
  table_name  text        NOT NULL,
  pk          text        NOT NULL,
  column_name text        NOT NULL,
  raw         jsonb       NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, table_name, column_name, pk)
);
