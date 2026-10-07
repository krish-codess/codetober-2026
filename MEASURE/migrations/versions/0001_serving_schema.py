"""Serving schema: generation runs, per-user payloads, the cards in them, views and shares.

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE generation_runs (
            run_id             uuid PRIMARY KEY,
            year               smallint NOT NULL CHECK (year BETWEEN 2008 AND 2100),
            status             text NOT NULL CHECK (status IN ('loading', 'ready')),
            source_fingerprint text NOT NULL,
            population         integer NOT NULL CHECK (population >= 0),
            user_count         integer NOT NULL DEFAULT 0 CHECK (user_count >= 0),
            started_at         timestamptz NOT NULL DEFAULT now(),
            finished_at        timestamptz,
            CHECK ((status = 'ready') = (finished_at IS NOT NULL))
        );

        -- Which run is being served for a year. Publishing and rolling back are both a one-row update.
        CREATE TABLE active_runs (
            year            smallint PRIMARY KEY,
            run_id          uuid NOT NULL REFERENCES generation_runs (run_id),
            previous_run_id uuid REFERENCES generation_runs (run_id),
            activated_at    timestamptz NOT NULL DEFAULT now(),
            CHECK (previous_run_id IS DISTINCT FROM run_id)
        );

        CREATE TABLE card_types (
            card_type text PRIMARY KEY,
            family    text NOT NULL,
            shareable boolean NOT NULL
        );

        CREATE TABLE wrapped_payloads (
            run_id  uuid NOT NULL REFERENCES generation_runs (run_id) ON DELETE CASCADE,
            user_id bigint NOT NULL CHECK (user_id > 0),
            login   text NOT NULL CHECK (login <> ''),
            tier    text NOT NULL CHECK (tier IN ('full', 'light', 'minimal')),
            payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
            PRIMARY KEY (run_id, user_id)
        );

        -- One row per card in a payload, so "which superlatives did people get" is a GROUP BY, not a JSON scan.
        CREATE TABLE payload_cards (
            run_id    uuid NOT NULL,
            user_id   bigint NOT NULL,
            position  smallint NOT NULL CHECK (position >= 0),
            card_type text NOT NULL REFERENCES card_types (card_type),
            PRIMARY KEY (run_id, user_id, position),
            UNIQUE (run_id, user_id, card_type),
            FOREIGN KEY (run_id, user_id) REFERENCES wrapped_payloads (run_id, user_id) ON DELETE CASCADE
        );
        -- Serves the superlative-distribution query (count by card_type for one run) as an index-only scan.
        CREATE INDEX payload_cards_run_type_idx ON payload_cards (run_id, card_type);

        -- First time a user saw a card. The primary key makes recording a view idempotent.
        CREATE TABLE card_views (
            user_id         bigint NOT NULL CHECK (user_id > 0),
            year            smallint NOT NULL,
            card_type       text NOT NULL REFERENCES card_types (card_type),
            first_viewed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, year, card_type)
        );

        -- A share is a snapshot of exactly one card. The public share page reads this row and nothing else,
        -- so it cannot leak the rest of the payload. source_run_id is deliberately not a foreign key:
        -- the snapshot must outlive the run it was taken from.
        CREATE TABLE shares (
            share_id      text PRIMARY KEY CHECK (length(share_id) >= 22),
            user_id       bigint NOT NULL CHECK (user_id > 0),
            year          smallint NOT NULL,
            card_type     text NOT NULL REFERENCES card_types (card_type),
            login         text NOT NULL,
            card          jsonb NOT NULL CHECK (jsonb_typeof(card) = 'object'),
            source_run_id uuid NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, year, card_type)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE shares;
        DROP TABLE card_views;
        DROP TABLE payload_cards;
        DROP TABLE wrapped_payloads;
        DROP TABLE card_types;
        DROP TABLE active_runs;
        DROP TABLE generation_runs;
        """
    )
