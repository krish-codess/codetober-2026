"""Initial schema.

Revision ID: 0001
Revises:
"""

import os
import re

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = (
    "taxonomy_versions", "taxonomy_nodes", "ingest_batches", "raw_feedback", "quarantine",
    "split_groups", "feedback", "embeddings", "model_versions", "annotations", "labels",
    "predictions", "node_metrics", "jobs", "api_tokens",
)  # fmt: skip

SCHEMA = r"""
-- ---------------------------------------------------------------------------------------------
-- Taxonomy. Every change is one row in taxonomy_versions; nodes are never deleted, only retired,
-- so historical labels and model versions keep resolving.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE taxonomy_versions (
    id               integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    op               text        NOT NULL CHECK (op IN ('init','add','rename','merge','split','move','retire')),
    params           jsonb       NOT NULL DEFAULT '{}',
    idempotency_key  text        NOT NULL UNIQUE,
    actor            text        NOT NULL,
    labels_remapped  integer     NOT NULL DEFAULT 0 CHECK (labels_remapped >= 0),
    labels_flagged   integer     NOT NULL DEFAULT 0 CHECK (labels_flagged >= 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE taxonomy_nodes (
    id               integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id        integer REFERENCES taxonomy_nodes(id) ON DELETE RESTRICT,
    name             text     NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9_]{0,62}$'),
    title            text     NOT NULL CHECK (btrim(title) <> ''),
    depth            smallint NOT NULL CHECK (depth BETWEEN 1 AND 6),   -- derived by trigger
    path             text     NOT NULL,                                 -- derived by trigger
    created_version  integer  NOT NULL REFERENCES taxonomy_versions(id),
    retired_version  integer  REFERENCES taxonomy_versions(id),
    CHECK (parent_id IS DISTINCT FROM id),
    CHECK ((parent_id IS NULL) = (depth = 1)),
    CHECK (retired_version IS NULL OR retired_version >= created_version)
);
CREATE UNIQUE INDEX taxonomy_nodes_active_path ON taxonomy_nodes (path) WHERE retired_version IS NULL;
CREATE INDEX taxonomy_nodes_parent ON taxonomy_nodes (parent_id);

CREATE FUNCTION taxonomy_nodes_derive() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p taxonomy_nodes%ROWTYPE;
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.depth := 1;
        NEW.path := NEW.name;
    ELSE
        SELECT * INTO STRICT p FROM taxonomy_nodes WHERE id = NEW.parent_id;
        IF p.retired_version IS NOT NULL AND NEW.retired_version IS NULL THEN
            RAISE EXCEPTION 'parent node "%" is retired', p.path USING ERRCODE = 'check_violation';
        END IF;
        IF TG_OP = 'UPDATE' AND (p.id = OLD.id OR starts_with(p.path, OLD.path || '/')) THEN
            RAISE EXCEPTION 'moving "%" under "%" would create a cycle', OLD.path, p.path
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.depth := p.depth + 1;
        NEW.path := p.path || '/' || NEW.name;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.retired_version IS NOT NULL AND OLD.retired_version IS NULL
       AND EXISTS (SELECT 1 FROM taxonomy_nodes c WHERE c.parent_id = NEW.id AND c.retired_version IS NULL) THEN
        RAISE EXCEPTION 'node "%" still has active children', OLD.path USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER taxonomy_nodes_derive BEFORE INSERT OR UPDATE OF parent_id, name, retired_version
    ON taxonomy_nodes FOR EACH ROW EXECUTE FUNCTION taxonomy_nodes_derive();

-- A rename or move re-derives the whole subtree: touching `name` re-fires the BEFORE trigger.
CREATE FUNCTION taxonomy_nodes_cascade() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE taxonomy_nodes SET name = name WHERE parent_id = NEW.id;
    RETURN NULL;
END $$;
CREATE TRIGGER taxonomy_nodes_cascade AFTER UPDATE OF parent_id, name ON taxonomy_nodes
    FOR EACH ROW WHEN (OLD.path IS DISTINCT FROM NEW.path) EXECUTE FUNCTION taxonomy_nodes_cascade();

-- ---------------------------------------------------------------------------------------------
-- Ingestion. raw_feedback is append-only; every raw row ends up in exactly one of
-- feedback or quarantine (asserted by the data tests).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE ingest_batches (
    id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         text        NOT NULL,
    file_name      text        NOT NULL,
    file_sha256    char(64)    NOT NULL UNIQUE,          -- idempotency: a file is ingested once
    n_records      integer     NOT NULL DEFAULT 0 CHECK (n_records >= 0),
    n_accepted     integer     NOT NULL DEFAULT 0 CHECK (n_accepted >= 0),
    n_quarantined  integer     NOT NULL DEFAULT 0 CHECK (n_quarantined >= 0),
    n_repaired     integer     NOT NULL DEFAULT 0 CHECK (n_repaired >= 0),
    n_late         integer     NOT NULL DEFAULT 0 CHECK (n_late >= 0),
    started_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (n_accepted + n_quarantined = n_records)
);

CREATE TABLE raw_feedback (
    batch_id     integer     NOT NULL REFERENCES ingest_batches(id),
    line_no      integer     NOT NULL CHECK (line_no > 0),
    payload      bytea       NOT NULL,                    -- exact bytes received, even invalid UTF-8
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, line_no)
);
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END $$;
CREATE TRIGGER raw_feedback_immutable BEFORE UPDATE OR DELETE ON raw_feedback
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER raw_feedback_no_truncate BEFORE TRUNCATE ON raw_feedback
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

CREATE TABLE quarantine (
    batch_id    integer NOT NULL,
    line_no     integer NOT NULL,
    reason      text    NOT NULL CHECK (reason IN (
        'bad_encoding','malformed_json','bad_field','missing_text','empty_text','too_long',
        'bad_timestamp','duplicate_delivery','conflicting_duplicate','split_conflict')),
    detail      text    NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, line_no),
    FOREIGN KEY (batch_id, line_no) REFERENCES raw_feedback(batch_id, line_no)
);

-- One row per split group. The composite FK from feedback makes it impossible for a group
-- (e.g. the translations of one sentence) to straddle pool and test: leakage is a constraint error.
CREATE TABLE split_groups (
    group_key  text PRIMARY KEY,
    split      text NOT NULL CHECK (split IN ('pool','test')),
    UNIQUE (group_key, split)
);

CREATE TABLE feedback (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id      integer  NOT NULL,
    line_no       integer  NOT NULL,
    source        text     NOT NULL CHECK (source ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
    external_id   text     NOT NULL CHECK (char_length(external_id) BETWEEN 1 AND 200),
    text          text     NOT NULL CHECK (char_length(text) BETWEEN 1 AND 4000),  -- characters
    text_sha256   char(64) NOT NULL,                      -- of the normalised text
    lang          text     CHECK (lang ~ '^[a-z]{2,3}$'), -- ISO 639; NULL = not supplied
    created_at    timestamptz,                            -- event time (UTC); NULL = not supplied
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    is_late       boolean  NOT NULL DEFAULT false,
    group_key     text     NOT NULL,
    split         text     NOT NULL,
    duplicate_of  bigint   REFERENCES feedback(id),
    UNIQUE (source, external_id),
    UNIQUE (batch_id, line_no),
    FOREIGN KEY (batch_id, line_no) REFERENCES raw_feedback(batch_id, line_no),
    FOREIGN KEY (group_key, split) REFERENCES split_groups(group_key, split),
    CHECK (duplicate_of IS DISTINCT FROM id)
);
-- Serves duplicate detection at ingest (docs/explain/feedback_text_sha256.txt).
CREATE INDEX feedback_text_sha256 ON feedback (text_sha256);

CREATE TABLE embeddings (
    feedback_id  bigint   PRIMARY KEY REFERENCES feedback(id) ON DELETE CASCADE,
    model        text     NOT NULL,
    dim          smallint NOT NULL CHECK (dim > 0),
    vec          bytea    NOT NULL,                       -- little-endian float32[dim], L2-normalised
    CHECK (octet_length(vec) = dim * 4)
);

-- ---------------------------------------------------------------------------------------------
-- Models. The artifact lives in the row: data hash, code version, params and weights are one
-- unit, and a database backup is a complete backup.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE model_versions (
    id                 integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status             text        NOT NULL CHECK (status IN ('candidate','active','rejected','archived')),
    taxonomy_version   integer     NOT NULL REFERENCES taxonomy_versions(id),
    n_labeled          integer     NOT NULL CHECK (n_labeled >= 0),
    train_data_sha256  char(64)    NOT NULL,
    code_version       text        NOT NULL,
    embed_model        text        NOT NULL,
    params             jsonb       NOT NULL,
    metrics            jsonb       NOT NULL,
    gate               jsonb       NOT NULL,
    artifact           bytea       NOT NULL,              -- .npz, loaded with allow_pickle=False
    artifact_sha256    char(64)    NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX model_versions_one_active ON model_versions ((true)) WHERE status = 'active';

-- ---------------------------------------------------------------------------------------------
-- Labels. An annotation row means "a person (or the gold source) has looked at this item";
-- zero label rows under it is a valid answer ("no category applies").
-- ---------------------------------------------------------------------------------------------
CREATE TABLE annotations (
    feedback_id       bigint PRIMARY KEY REFERENCES feedback(id) ON DELETE CASCADE,
    annotator         text        NOT NULL,
    source            text        NOT NULL CHECK (source IN ('human','simulated','gold')),
    taxonomy_version  integer     NOT NULL REFERENCES taxonomy_versions(id),
    model_version_id  integer     REFERENCES model_versions(id),   -- model that made the suggestion
    annotated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE labels (
    feedback_id    bigint  NOT NULL REFERENCES annotations(feedback_id) ON DELETE CASCADE,
    node_id        integer NOT NULL REFERENCES taxonomy_nodes(id),
    review_reason  text,                 -- non-NULL = flagged for targeted relabelling
    PRIMARY KEY (feedback_id, node_id)
);
-- Serves taxonomy operations and per-node counts (docs/explain/labels_node.txt).
CREATE INDEX labels_node ON labels (node_id);
-- Serves the targeted-relabel queue (docs/explain/labels_review.txt).
CREATE INDEX labels_review ON labels (feedback_id) WHERE review_reason IS NOT NULL;

-- Hierarchical consistency, enforced where no application bug can bypass it:
-- inserting a label inserts its parent (recursively, via this same trigger);
-- deleting a label deletes the labels of its children.
CREATE FUNCTION labels_close_up() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n taxonomy_nodes%ROWTYPE;
BEGIN
    SELECT * INTO STRICT n FROM taxonomy_nodes WHERE id = NEW.node_id;
    IF n.retired_version IS NOT NULL THEN
        RAISE EXCEPTION 'cannot label with retired node "%"', n.path USING ERRCODE = 'check_violation';
    END IF;
    IF n.parent_id IS NOT NULL THEN
        INSERT INTO labels (feedback_id, node_id) VALUES (NEW.feedback_id, n.parent_id)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER labels_close_up AFTER INSERT ON labels FOR EACH ROW EXECUTE FUNCTION labels_close_up();

CREATE FUNCTION labels_close_down() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM labels l USING taxonomy_nodes c
    WHERE l.feedback_id = OLD.feedback_id AND l.node_id = c.id AND c.parent_id = OLD.node_id;
    RETURN NULL;
END $$;
CREATE TRIGGER labels_close_down AFTER DELETE ON labels FOR EACH ROW EXECUTE FUNCTION labels_close_down();

-- ---------------------------------------------------------------------------------------------
-- Serving state
-- ---------------------------------------------------------------------------------------------
CREATE TABLE predictions (
    feedback_id       bigint  PRIMARY KEY REFERENCES feedback(id) ON DELETE CASCADE,
    model_version_id  integer NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    node_ids          integer[] NOT NULL,     -- candidate nodes, most probable first
    probs             real[]    NOT NULL,     -- calibrated marginal probability per candidate
    confidence        real NOT NULL CHECK (confidence BETWEEN 0 AND 1),  -- P(predicted set exactly right)
    uncertainty       real NOT NULL CHECK (uncertainty >= 0),            -- hierarchical entropy, nats
    priority          real NOT NULL,          -- labelling-queue order (diversified uncertainty)
    CHECK (cardinality(node_ids) = cardinality(probs))
);
-- Serves the labelling queue: keyset pagination on (priority DESC, feedback_id)
-- (docs/explain/predictions_queue.txt).
CREATE INDEX predictions_queue ON predictions (priority DESC, feedback_id);

CREATE TABLE node_metrics (
    model_version_id  integer NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    node_id           integer NOT NULL REFERENCES taxonomy_nodes(id),
    lang              text    NOT NULL,       -- ISO code, or 'all'
    tp                integer NOT NULL CHECK (tp >= 0),
    fp                integer NOT NULL CHECK (fp >= 0),
    fn                integer NOT NULL CHECK (fn >= 0),
    PRIMARY KEY (model_version_id, node_id, lang)
);

CREATE TABLE jobs (
    id               integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text        NOT NULL CHECK (kind IN ('retrain','embed_score')),
    status           text        NOT NULL DEFAULT 'queued'
                                 CHECK (status IN ('queued','running','succeeded','failed')),
    idempotency_key  text        NOT NULL UNIQUE,
    requested_by     text        NOT NULL,
    progress         real        NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    stage            text        NOT NULL DEFAULT 'queued',
    attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error            text,
    result           jsonb,
    requested_at     timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    CHECK ((status IN ('succeeded','failed')) = (finished_at IS NOT NULL))
);
-- Serves the worker's claim query (docs/explain/jobs_queued.txt).
CREATE INDEX jobs_queued ON jobs (requested_at) WHERE status = 'queued';

CREATE TABLE api_tokens (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text     NOT NULL UNIQUE,
    token_sha256  char(64) NOT NULL UNIQUE,   -- the token itself is never stored
    role          text     NOT NULL CHECK (role IN ('viewer','annotator','admin')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)
    # Least privilege: the services connect as a role that can read/write rows but cannot change
    # the schema, and cannot rewrite history in raw_feedback even if the trigger were dropped.
    user, password = os.environ.get("APP_DB_USER"), os.environ.get("APP_DB_PASSWORD")
    if user and password:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user):
            raise ValueError("APP_DB_USER must be a plain lowercase identifier")
        quoted_pw = password.replace("'", "''")  # DDL cannot take bind parameters
        op.execute(
            f"""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{user}') THEN
                    CREATE ROLE {user} LOGIN PASSWORD '{quoted_pw}';
                END IF;
            END $$"""
        )
        op.execute(f"GRANT USAGE ON SCHEMA public TO {user}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {user}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON raw_feedback FROM {user}")
        op.execute(f"REVOKE DELETE ON taxonomy_versions FROM {user}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for fn in ("labels_close_down", "labels_close_up", "forbid_mutation", "taxonomy_nodes_cascade",
               "taxonomy_nodes_derive"):  # fmt: skip
        op.execute(f"DROP FUNCTION IF EXISTS {fn}() CASCADE")
    user = os.environ.get("APP_DB_USER")
    if user and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", user):
        op.execute(f"DROP OWNED BY {user}")
        op.execute(f"DROP ROLE IF EXISTS {user}")
