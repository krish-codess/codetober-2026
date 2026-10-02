-- 0001: core schema. Every table states its integrity rules here, not in application code.
-- Published values (prices, baskets, index) are APPEND-ONLY: a trigger rejects UPDATE/DELETE,
-- so a historical value can only change by adding a new, visible vintage.

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'table % is append-only (% rejected); publish a new vintage instead',
        TG_TABLE_NAME, TG_OP USING ERRCODE = 'restrict_violation';
END $$;

-- ---------------------------------------------------------------- reference data
CREATE TABLE world (
    world_id     text PRIMARY KEY CHECK (world_id ~ '^[a-z0-9-]{1,32}$'),
    name         text NOT NULL,
    price_source text NOT NULL CHECK (price_source IN ('snapshots', 'trade_history')),
    currency     text NOT NULL DEFAULT 'ISK',
    is_synthetic boolean NOT NULL
);

CREATE TABLE server (
    server_id text PRIMARY KEY CHECK (server_id ~ '^[a-z0-9-]{1,40}$'),
    world_id  text NOT NULL REFERENCES world,
    name      text NOT NULL,
    region_id bigint,
    UNIQUE (world_id, name)
);

CREATE TABLE division (
    division_id text PRIMARY KEY CHECK (division_id ~ '^[a-z_]{1,32}$'),
    label       text NOT NULL,
    keywords    text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE item (
    item_id      bigint PRIMARY KEY CHECK (item_id > 0),
    name         text NOT NULL,
    division_id  text NOT NULL REFERENCES division,
    esi_group    text,
    esi_category text,
    volume_m3    double precision CHECK (volume_m3 >= 0)
);

CREATE TABLE activity (
    activity_id text PRIMARY KEY CHECK (activity_id ~ '^[a-z0-9-]{1,40}$'),
    world_id    text NOT NULL REFERENCES world,
    label       text NOT NULL
);

-- Nominal currency income per hour, effective-dated so a patch that changes bounties is a new row.
CREATE TABLE activity_rate (
    activity_id    text NOT NULL REFERENCES activity,
    effective_from date NOT NULL,
    isk_per_hour   double precision NOT NULL CHECK (isk_per_hour >= 0),
    PRIMARY KEY (activity_id, effective_from)
);

CREATE TABLE activity_yield (
    activity_id  text NOT NULL REFERENCES activity,
    item_id      bigint NOT NULL REFERENCES item,
    qty_per_hour double precision NOT NULL CHECK (qty_per_hour > 0),
    PRIMARY KEY (activity_id, item_id)
);

-- An index series is one (world, server-or-all, division-or-all) combination.
CREATE TABLE index_series (
    series_id   serial PRIMARY KEY,
    world_id    text NOT NULL REFERENCES world,
    server_id   text REFERENCES server,      -- NULL = cross-server
    division_id text REFERENCES division,    -- NULL = all divisions
    UNIQUE NULLS NOT DISTINCT (world_id, server_id, division_id)
);

-- ---------------------------------------------------------------- provenance
CREATE TABLE raw_manifest (
    sha256        char(64) PRIMARY KEY,
    source        text NOT NULL,
    kind          text NOT NULL,
    day           date NOT NULL,
    key           text NOT NULL,
    path          text NOT NULL,
    bytes         bigint NOT NULL CHECK (bytes > 0),
    fetched_at    timestamptz NOT NULL,
    observed_at   timestamptz NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now(),
    CHECK (fetched_at >= observed_at - interval '1 minute')
);
CREATE INDEX raw_manifest_partition ON raw_manifest (source, kind, day);
CREATE TRIGGER raw_manifest_append_only BEFORE UPDATE OR DELETE ON raw_manifest
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE ingest_run (
    run_id      uuid PRIMARY KEY,
    job         text NOT NULL,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    status      text NOT NULL CHECK (status IN ('running', 'ok', 'degraded', 'failed')),
    requested   integer NOT NULL DEFAULT 0 CHECK (requested >= 0),
    stored      integer NOT NULL DEFAULT 0 CHECK (stored >= 0),
    failed      integer NOT NULL DEFAULT 0 CHECK (failed >= 0),
    detail      jsonb NOT NULL DEFAULT '{}',
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);
CREATE INDEX ingest_run_recent ON ingest_run (job, started_at DESC);

CREATE TABLE data_quality_daily (
    world_id        text NOT NULL REFERENCES world,
    day             date NOT NULL,
    n_payloads      integer NOT NULL CHECK (n_payloads >= 0),
    n_late_payloads integer NOT NULL CHECK (n_late_payloads >= 0),
    n_rows          bigint NOT NULL CHECK (n_rows >= 0),
    n_valid         bigint NOT NULL CHECK (n_valid >= 0),
    n_quarantined   bigint NOT NULL CHECK (n_quarantined >= 0),
    reasons         jsonb NOT NULL DEFAULT '{}',
    raw_fingerprint char(64) NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, day),
    CHECK (n_valid + n_quarantined = n_rows)
);

-- ---------------------------------------------------------------- published prices (vintaged)
CREATE TABLE item_price_daily (
    server_id     text NOT NULL REFERENCES server,
    item_id       bigint NOT NULL REFERENCES item,
    day           date NOT NULL,
    vintage       integer NOT NULL CHECK (vintage >= 1),
    price         double precision CHECK (price > 0),
    volume        double precision CHECK (volume >= 0),
    n_obs         integer NOT NULL CHECK (n_obs >= 0),
    status        text NOT NULL CHECK (status IN ('ok', 'thin', 'rejected', 'missing')),
    method        text NOT NULL,
    input_hash    char(16) NOT NULL,
    reason        text NOT NULL CHECK (reason IN ('initial', 'late_data', 'source_revision', 'method_change')),
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, item_id, day, vintage),
    -- a price is published only when it was actually observed: no fabricated values
    CHECK ((status = 'ok') = (price IS NOT NULL))
);
CREATE TRIGGER item_price_daily_append_only BEFORE UPDATE OR DELETE ON item_price_daily
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- baskets (frozen at creation)
CREATE TABLE basket_period (
    world_id       text NOT NULL REFERENCES world,
    period_id      text NOT NULL CHECK (period_id ~ '^\d{4}Q[1-4]$'),
    valid_from     date NOT NULL,
    valid_to       date NOT NULL,
    ref_from       date NOT NULL,
    ref_to         date NOT NULL,
    link_from      date NOT NULL,
    link_to        date NOT NULL,
    method_version text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, period_id),
    CHECK (valid_from <= valid_to AND ref_from <= ref_to AND link_from <= link_to),
    CHECK (ref_to < valid_from AND link_to < valid_from)
);
CREATE TRIGGER basket_period_append_only BEFORE UPDATE OR DELETE ON basket_period
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE basket_item (
    world_id    text NOT NULL,
    period_id   text NOT NULL,
    server_id   text NOT NULL REFERENCES server,
    item_id     bigint NOT NULL REFERENCES item,
    weight      double precision NOT NULL CHECK (weight > 0 AND weight <= 1),
    base_price  double precision NOT NULL CHECK (base_price > 0),
    expenditure double precision NOT NULL CHECK (expenditure >= 0),
    quantity    double precision NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (world_id, period_id, server_id, item_id),
    FOREIGN KEY (world_id, period_id) REFERENCES basket_period
);
CREATE TRIGGER basket_item_append_only BEFORE UPDATE OR DELETE ON basket_item
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Chain-link factor: index level of a series at the link point of each basket period.
CREATE TABLE basket_link (
    world_id   text NOT NULL,
    period_id  text NOT NULL,
    series_id  integer NOT NULL REFERENCES index_series,
    link_value double precision NOT NULL CHECK (link_value > 0),
    PRIMARY KEY (world_id, period_id, series_id),
    FOREIGN KEY (world_id, period_id) REFERENCES basket_period
);
CREATE TRIGGER basket_link_append_only BEFORE UPDATE OR DELETE ON basket_link
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------------------------------------------------------------- index values (vintaged)
CREATE TABLE index_value (
    series_id      integer NOT NULL REFERENCES index_series,
    day            date NOT NULL,
    vintage        integer NOT NULL CHECK (vintage >= 1),
    value          double precision CHECK (value > 0),
    coverage       double precision NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
    n_items        integer NOT NULL CHECK (n_items >= 0),
    status         text NOT NULL CHECK (status IN ('ok', 'partial', 'insufficient')),
    period_id      text NOT NULL,
    method_version text NOT NULL,
    input_hash     char(16) NOT NULL,
    reason         text NOT NULL CHECK (reason IN ('initial', 'late_data', 'source_revision', 'method_change')),
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, day, vintage),
    CHECK ((status = 'insufficient') = (value IS NULL))
);
CREATE TRIGGER index_value_append_only BEFORE UPDATE OR DELETE ON index_value
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Latest vintage per key: what the API serves unless the caller asks for an as-of view.
CREATE VIEW index_value_current AS
SELECT DISTINCT ON (series_id, day) *, (vintage > 1) AS revised
FROM index_value ORDER BY series_id, day, vintage DESC;

CREATE VIEW item_price_current AS
SELECT DISTINCT ON (server_id, item_id, day) *, (vintage > 1) AS revised
FROM item_price_daily ORDER BY server_id, item_id, day, vintage DESC;

-- ---------------------------------------------------------------- patch events
CREATE TABLE patch_event (
    world_id    text NOT NULL REFERENCES world,
    patch_id    text NOT NULL CHECK (patch_id ~ '^[A-Za-z0-9._-]{1,64}$'),
    released_at timestamptz NOT NULL,
    version     text,
    title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    notes       text NOT NULL CHECK (length(notes) <= 100000),
    tags        text[] NOT NULL DEFAULT '{}',
    is_major    boolean NOT NULL DEFAULT false,
    source      text NOT NULL CHECK (source IN ('rss', 'api', 'synthetic')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (world_id, patch_id)
);
-- serves: timeline range queries and keyset pagination (released_at, patch_id)
CREATE INDEX patch_event_timeline ON patch_event (world_id, released_at, patch_id);

-- ---------------------------------------------------------------- analytics (recomputable)
CREATE TABLE inflation_rate (
    series_id   integer NOT NULL REFERENCES index_series,
    day         date NOT NULL,
    window_days integer NOT NULL CHECK (window_days IN (7, 30, 90, 365)),
    rate        double precision NOT NULL,
    annualized  double precision NOT NULL,
    PRIMARY KEY (series_id, window_days, day)
);

CREATE TABLE shock (
    series_id  integer NOT NULL REFERENCES index_series,
    day        date NOT NULL,
    log_change double precision NOT NULL,
    robust_z   double precision NOT NULL,
    direction  text NOT NULL CHECK (direction IN ('up', 'down')),
    PRIMARY KEY (series_id, day)
);

CREATE TABLE shock_attribution (
    series_id  integer NOT NULL,
    day        date NOT NULL,
    world_id   text NOT NULL,
    patch_id   text NOT NULL,
    lag_hours  double precision NOT NULL CHECK (lag_hours >= 0),
    relevance  double precision NOT NULL CHECK (relevance >= 0 AND relevance <= 1),
    rank       integer NOT NULL CHECK (rank >= 1),
    PRIMARY KEY (series_id, day, patch_id),
    FOREIGN KEY (series_id, day) REFERENCES shock ON DELETE CASCADE,
    FOREIGN KEY (world_id, patch_id) REFERENCES patch_event ON DELETE CASCADE
);
CREATE INDEX shock_attribution_patch ON shock_attribution (world_id, patch_id);

CREATE TABLE patch_impact (
    world_id   text NOT NULL,
    patch_id   text NOT NULL,
    series_id  integer NOT NULL REFERENCES index_series,
    log_change double precision NOT NULL,
    robust_z   double precision NOT NULL,
    PRIMARY KEY (world_id, patch_id, series_id),
    FOREIGN KEY (world_id, patch_id) REFERENCES patch_event ON DELETE CASCADE
);

CREATE TABLE manipulation_event (
    server_id  text NOT NULL REFERENCES server,
    item_id    bigint NOT NULL REFERENCES item,
    day        date NOT NULL,
    kind       text NOT NULL CHECK (kind IN ('extreme_listing', 'hampel_reject', 'thin_market_spike')),
    severity   double precision NOT NULL CHECK (severity >= 0),
    n_obs      integer NOT NULL CHECK (n_obs >= 0),
    thin       boolean NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (server_id, item_id, day, kind)
);
-- serves: newest-first keyset pagination of the integrity feed per server
CREATE INDEX manipulation_event_feed ON manipulation_event (server_id, day DESC, item_id, kind);

CREATE TABLE money_flow (
    server_id text NOT NULL REFERENCES server,
    day       date NOT NULL,
    kind      text NOT NULL CHECK (kind IN ('sink_sales_tax', 'sink_broker_fee', 'faucet_bounty')),
    amount    double precision NOT NULL CHECK (amount >= 0),
    method    text NOT NULL,
    PRIMARY KEY (server_id, day, kind)
);

-- ---------------------------------------------------------------- API auth + idempotency
CREATE TABLE api_key (
    key_id     text PRIMARY KEY CHECK (key_id ~ '^[a-z0-9-]{1,40}$'),
    key_hash   char(64) NOT NULL UNIQUE,
    scopes     text[] NOT NULL CHECK (scopes <@ ARRAY['patches:write']::text[]),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE idempotency_key (
    key_id          text NOT NULL REFERENCES api_key,
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 8 AND 128),
    request_hash    char(64) NOT NULL,
    status_code     integer NOT NULL,
    response        jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, idempotency_key)
);

-- ---------------------------------------------------------------- least privilege
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gs_pipeline_role') THEN
        CREATE ROLE gs_pipeline_role NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gs_api_role') THEN
        CREATE ROLE gs_api_role NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO gs_pipeline_role, gs_api_role;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gs_pipeline_role, gs_api_role;
-- pipeline: reference upserts, append to published tables, replace recomputable analytics
GRANT INSERT, UPDATE ON world, server, division, item, activity, activity_rate, activity_yield,
    data_quality_daily, ingest_run TO gs_pipeline_role;
GRANT INSERT ON index_series, raw_manifest, item_price_daily, basket_period, basket_item, basket_link,
    index_value, patch_event TO gs_pipeline_role;
GRANT INSERT, UPDATE, DELETE ON inflation_rate, shock, shock_attribution, patch_impact,
    manipulation_event, money_flow TO gs_pipeline_role;
GRANT USAGE ON SEQUENCE index_series_series_id_seq TO gs_pipeline_role;
-- api: read everything published; write only patch notes and its own idempotency records
GRANT INSERT ON patch_event, idempotency_key TO gs_api_role;
REVOKE SELECT ON api_key FROM gs_pipeline_role;
