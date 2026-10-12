"""Experiment data, quarantine, model runs, scores and policies.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dataset (
            id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9_]{1,40}$'),
            outcome text NOT NULL CHECK (outcome IN ('visit', 'conversion', 'spend')),
            created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE raw_file (
            sha256 text PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
            dataset_id integer NOT NULL REFERENCES dataset,
            filename text NOT NULL,
            bytes bigint NOT NULL CHECK (bytes >= 0),
            rows_total integer NOT NULL,
            rows_loaded integer NOT NULL CHECK (rows_loaded >= 0),
            rows_quarantined integer NOT NULL CHECK (rows_quarantined >= 0),
            ingested_at timestamptz NOT NULL DEFAULT now(),
            -- every raw line is either loaded or quarantined, never silently dropped
            CHECK (rows_total = rows_loaded + rows_quarantined)
        );

        CREATE TABLE unit (
            dataset_id integer NOT NULL REFERENCES dataset,
            unit_id text NOT NULL,
            raw_sha text NOT NULL REFERENCES raw_file,
            line_no integer NOT NULL CHECK (line_no >= 2),
            treatment boolean NOT NULL,
            outcome double precision NOT NULL CHECK (outcome >= 0),
            assigned_at timestamptz,
            recency smallint CHECK (recency BETWEEN 1 AND 12),
            history numeric(10, 2) CHECK (history >= 0),
            mens boolean,
            womens boolean,
            newbie boolean,
            zip_code text CHECK (zip_code IN ('Surburban', 'Urban', 'Rural')),
            channel text CHECK (channel IN ('Web', 'Phone', 'Multichannel')),
            true_uplift double precision CHECK (true_uplift BETWEEN -1 AND 1),
            PRIMARY KEY (dataset_id, unit_id),
            UNIQUE (raw_sha, line_no)
        );

        CREATE TABLE quarantine (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            raw_sha text NOT NULL REFERENCES raw_file,
            line_no integer NOT NULL CHECK (line_no >= 2),
            unit_id text,
            reason text NOT NULL,
            raw jsonb NOT NULL,
            UNIQUE (raw_sha, line_no)
        );

        CREATE TABLE experiment (
            id uuid PRIMARY KEY,
            dataset_id integer NOT NULL REFERENCES dataset,
            status text NOT NULL CHECK (status IN ('running', 'complete', 'failed', 'retired')),
            data_version text NOT NULL,
            code_version text NOT NULL,
            params jsonb NOT NULL,
            split_strategy text CHECK (split_strategy IN ('temporal', 'hash')),
            audit jsonb,
            timings jsonb,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            CHECK (status = 'running' OR finished_at IS NOT NULL)
        );
        -- serves "the latest complete experiment for this dataset", the lookup behind every read endpoint
        CREATE INDEX experiment_serving ON experiment (dataset_id, finished_at DESC) WHERE status = 'complete';

        CREATE TABLE model_run (
            id uuid PRIMARY KEY,
            experiment_id uuid NOT NULL REFERENCES experiment ON DELETE CASCADE,
            learner text NOT NULL,
            status text NOT NULL CHECK (status IN ('complete', 'failed')),
            error text,
            is_champion boolean NOT NULL DEFAULT false,
            artifact_file text,
            artifact_sha256 text CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
            fit_seconds double precision CHECK (fit_seconds >= 0),
            metrics jsonb,
            curves jsonb,
            profiles jsonb,
            UNIQUE (experiment_id, learner),
            CHECK ((status = 'failed' AND error IS NOT NULL) OR (status = 'complete' AND metrics IS NOT NULL)),
            CHECK (NOT is_champion OR status = 'complete')
        );
        CREATE UNIQUE INDEX model_run_one_champion ON model_run (experiment_id) WHERE is_champion;

        CREATE TABLE decile (
            run_id uuid NOT NULL REFERENCES model_run ON DELETE CASCADE,
            decile smallint NOT NULL CHECK (decile BETWEEN 1 AND 10),
            n_treated integer NOT NULL CHECK (n_treated >= 0),
            n_control integer NOT NULL CHECK (n_control >= 0),
            mean_predicted double precision NOT NULL,
            observed_uplift double precision NOT NULL,
            ci_low double precision NOT NULL,
            ci_high double precision NOT NULL,
            true_uplift double precision,
            PRIMARY KEY (run_id, decile),
            CHECK (ci_low <= observed_uplift AND observed_uplift <= ci_high)
        );

        CREATE TABLE segment (
            experiment_id uuid NOT NULL REFERENCES experiment ON DELETE CASCADE,
            definition text NOT NULL,
            n_treated integer NOT NULL CHECK (n_treated > 0),
            n_control integer NOT NULL CHECK (n_control > 0),
            uplift double precision NOT NULL,
            ci_low double precision NOT NULL,
            ci_high double precision NOT NULL,
            p_adjusted double precision NOT NULL CHECK (p_adjusted BETWEEN 0 AND 1),
            verdict text NOT NULL CHECK (verdict IN ('helps', 'hurts', 'unclear')),
            PRIMARY KEY (experiment_id, definition)
        );

        CREATE TABLE score (
            run_id uuid NOT NULL REFERENCES model_run ON DELETE CASCADE,
            dataset_id integer NOT NULL,
            unit_id text NOT NULL,
            split text NOT NULL CHECK (split IN ('train', 'val', 'test')),
            uplift double precision NOT NULL,
            PRIMARY KEY (run_id, unit_id),
            FOREIGN KEY (dataset_id, unit_id) REFERENCES unit
        );
        -- serves cursor pagination and the policy export: top-k by uplift within a run and split
        CREATE INDEX score_ranked ON score (run_id, split, uplift, unit_id);

        CREATE TABLE policy (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES model_run,
            idempotency_key text NOT NULL UNIQUE,
            budget numeric(14, 2) NOT NULL CHECK (budget >= 0),
            cost_per_treatment numeric(10, 4) NOT NULL CHECK (cost_per_treatment > 0),
            value_per_outcome numeric(12, 4) NOT NULL CHECK (value_per_outcome > 0),
            population integer NOT NULL CHECK (population >= 0),
            n_targeted integer NOT NULL CHECK (n_targeted >= 0),
            min_uplift double precision,
            evaluation jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            -- the budget guarantee, held by the database and not only by the code that computes it
            CHECK (n_targeted <= population AND n_targeted * cost_per_treatment <= budget)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE policy, score, segment, decile, model_run, experiment, quarantine, unit, raw_file, dataset")
