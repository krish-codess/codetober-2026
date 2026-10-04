CREATE TABLE runs (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key TEXT        NOT NULL UNIQUE,
    subject         TEXT        NOT NULL,
    engine          TEXT        NOT NULL CHECK (engine IN ('duckdb', 'postgres')),
    seed            BIGINT      NOT NULL,
    max_examples    INTEGER     NOT NULL CHECK (max_examples > 0),
    status          TEXT        NOT NULL DEFAULT 'running'
                                CHECK (status IN ('running', 'completed', 'failed')),
    git_sha         TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ CHECK (finished_at >= started_at),
    examples        INTEGER     CHECK (examples >= 0),
    rows_generated  BIGINT      CHECK (rows_generated >= 0),
    duration_ms     INTEGER     CHECK (duration_ms >= 0),
    candidates      INTEGER     CHECK (candidates >= 0),
    candidates_held INTEGER     CHECK (candidates_held BETWEEN 0 AND candidates),
    gate_ok         BOOLEAN,
    error           TEXT,
    -- a finished run has its results; a running one has none yet
    CHECK ((status = 'running') = (finished_at IS NULL)),
    CHECK (status <> 'completed' OR (examples IS NOT NULL AND gate_ok IS NOT NULL)),
    CHECK (status <> 'failed' OR error IS NOT NULL)
);

CREATE TABLE properties (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject     TEXT NOT NULL,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('declared', 'discovered')),
    description TEXT NOT NULL,
    UNIQUE (subject, name)
);

CREATE TABLE property_results (
    run_id      BIGINT  NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    property_id BIGINT  NOT NULL REFERENCES properties (id),
    passed      INTEGER NOT NULL CHECK (passed >= 0),
    failed      INTEGER NOT NULL CHECK (failed >= 0),
    vacuous     INTEGER NOT NULL CHECK (vacuous >= 0),
    status      TEXT    NOT NULL CHECK (status IN ('held', 'falsified', 'vacuous')),
    confidence  REAL    NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    known_bug   TEXT,
    PRIMARY KEY (run_id, property_id),
    CHECK ((status = 'falsified') = (failed > 0)),
    CHECK (status <> 'falsified' OR confidence = 0)
);

-- No secondary index on (property_id, run_id): every query reaches a property's history
-- through its newest runs, which the primary key already serves. Measured and rejected
-- in docs/explain_analyze.md (Q1).

CREATE TABLE failures (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id          BIGINT  NOT NULL,
    property_id     BIGINT  NOT NULL,
    -- JSON, not JSONB: JSONB reorders keys, and the viewer shows tables and columns in
    -- schema order. Nothing queries inside the document.
    minimal_dataset JSON    NOT NULL CHECK (json_typeof(minimal_dataset) = 'object'),
    minimal_rows    INTEGER NOT NULL CHECK (minimal_rows >= 0),
    shrunk          BOOLEAN NOT NULL,
    shrink_calls    INTEGER NOT NULL CHECK (shrink_calls >= 0),
    shrink_ms       INTEGER NOT NULL CHECK (shrink_ms >= 0),
    UNIQUE (run_id, property_id),
    FOREIGN KEY (run_id, property_id) REFERENCES property_results (run_id, property_id)
        ON DELETE CASCADE
);

-- Serves the failure viewer filtered to one property, keyset-paginated by id DESC.
-- Without it a rarely-failing property means walking the whole primary key backwards:
-- 7 buffers instead of 291 on 72k rows, docs/explain_analyze.md (Q2).
CREATE INDEX failures_property_id_idx ON failures (property_id, id DESC);
