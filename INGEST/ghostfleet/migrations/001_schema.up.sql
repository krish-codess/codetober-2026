CREATE EXTENSION IF NOT EXISTS postgis;

-- Reference data: ITU Maritime Identification Digits. Loaded from ghostfleet/data/mid.csv by `migrate`.
CREATE TABLE mid_countries (
    mid     smallint PRIMARY KEY CHECK (mid BETWEEN 200 AND 799),
    iso2    char(2) NOT NULL,
    country text NOT NULL
);

CREATE TABLE vessels (
    mmsi              integer PRIMARY KEY CHECK (mmsi BETWEEN 200000000 AND 799999999),
    -- Not a foreign key: real feeds contain MMSIs under unallocated MIDs, and those vessels still move.
    mid               smallint GENERATED ALWAYS AS ((mmsi / 1000000)::smallint) STORED,
    imo               integer CHECK (imo BETWEEN 1000000 AND 9999999),
    name              text CHECK (name <> ''),
    callsign          text CHECK (callsign <> ''),
    ship_type         smallint CHECK (ship_type BETWEEN 0 AND 255),
    length_m          smallint CHECK (length_m > 0),
    beam_m            smallint CHECK (beam_m > 0),
    draught_m         real CHECK (draught_m > 0),
    destination       text,
    first_seen        timestamptz NOT NULL DEFAULT now(),
    static_updated_at timestamptz
);

-- Geography, not a projected CRS: the fleet is global, and no single projection keeps distances
-- honest from Singapore to Svalbard. SRID 4326 is explicit in the type; distances are geodesic metres.
CREATE TABLE positions (
    mmsi        integer NOT NULL REFERENCES vessels (mmsi),
    ts          timestamptz NOT NULL,
    geog        geography(Point, 4326) NOT NULL,
    sog         real CHECK (sog BETWEEN 0 AND 102.2),
    cog         real CHECK (cog >= 0 AND cog < 360),
    heading     smallint CHECK (heading BETWEEN 0 AND 359),
    nav_status  smallint CHECK (nav_status BETWEEN 0 AND 15),
    msg_type    smallint NOT NULL CHECK (msg_type IN (1, 2, 3, 18, 19, 27)),
    source      text NOT NULL,
    received_at timestamptz NOT NULL,
    ingested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- The idempotency key. Replays and overlapping providers collapse here.
    PRIMARY KEY (mmsi, ts)
);
CREATE INDEX positions_geog_gist ON positions USING gist (geog);
CREATE INDEX positions_ts_brin ON positions USING brin (ts) WITH (pages_per_range = 32);

-- One row per vessel, so the live map never has to find "latest per MMSI" in the big table.
CREATE TABLE vessel_latest (
    mmsi       integer PRIMARY KEY REFERENCES vessels (mmsi) ON DELETE CASCADE,
    ts         timestamptz NOT NULL,
    -- Geometry here, on purpose: the only spatial question asked of this table is "what is inside
    -- this map viewport", a planar lon/lat box. A geography box cannot span more than a hemisphere.
    geom       geometry(Point, 4326) NOT NULL,
    sog        real,
    cog        real,
    heading    smallint,
    nav_status smallint,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX vessel_latest_geom_gist ON vessel_latest USING gist (geom);
CREATE INDEX vessel_latest_updated_at ON vessel_latest (updated_at);

-- Empirical reception map: for each grid cell, in how many hours of the window was anything heard?
CREATE TABLE coverage_cells (
    cx           integer NOT NULL,
    cy           integer NOT NULL,
    cell_deg     real NOT NULL CHECK (cell_deg > 0),
    hours_heard  integer NOT NULL CHECK (hours_heard >= 0),
    hours_window integer NOT NULL CHECK (hours_window > 0),
    vessels      integer NOT NULL CHECK (vessels >= 0),
    reliability  real NOT NULL CHECK (reliability BETWEEN 0 AND 1),
    geom         geometry(Polygon, 4326) NOT NULL,
    computed_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (cx, cy),
    CHECK (hours_heard <= hours_window)
);
CREATE INDEX coverage_cells_geom_gist ON coverage_cells USING gist (geom);

CREATE TABLE dark_events (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mmsi              integer NOT NULL REFERENCES vessels (mmsi),
    started_at        timestamptz NOT NULL,
    ended_at          timestamptz,
    start_geog        geography(Point, 4326) NOT NULL,
    end_geog          geography(Point, 4326),
    duration_s        integer NOT NULL CHECK (duration_s > 0),
    distance_m        double precision CHECK (distance_m >= 0),
    implied_speed_kn  real CHECK (implied_speed_kn >= 0),
    start_sog         real,
    start_reliability real NOT NULL CHECK (start_reliability BETWEEN 0 AND 1),
    end_reliability   real CHECK (end_reliability BETWEEN 0 AND 1),
    neighbours_heard  integer NOT NULL CHECK (neighbours_heard >= 0),
    classification    text NOT NULL CHECK (classification IN ('dark', 'coverage_gap', 'position_jump')),
    impossible_speed  boolean NOT NULL DEFAULT false,
    score             real NOT NULL CHECK (score BETWEEN 0 AND 100),
    reasons           jsonb NOT NULL,
    detected_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (mmsi, started_at),
    CHECK (ended_at > started_at),
    -- An event is either still open (no reappearance yet) or fully closed, never half of each.
    CHECK ((ended_at IS NULL) = (end_geog IS NULL)),
    CHECK ((ended_at IS NULL) = (distance_m IS NULL))
);
CREATE INDEX dark_events_rank ON dark_events (score DESC, id DESC);

-- Dead letters, persisted. `cid` traces back to the raw archive line (or the Kafka record) at fault.
CREATE TABLE quarantine (
    cid            text NOT NULL,
    stage          text NOT NULL CHECK (stage IN ('decode', 'validate', 'sink')),
    reason         text NOT NULL,
    source         text NOT NULL,
    payload        text NOT NULL,
    received_at    timestamptz NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    replayed_at    timestamptz,
    PRIMARY KEY (cid, stage)
);
CREATE INDEX quarantine_reason ON quarantine (reason, quarantined_at);
