"""Drop payload_cards_run_type_idx: the evidence did not support it.

It was added for the superlative-distribution query (count by card_type for one run). Measured on
200,000 payloads / 1.16 million payload cards, the planner never chose it: a run is all, or with a
previous run loaded half, of the table, so a sequential scan is the right plan, and the query took
the same ~1.1 s with the index and without. An index nothing reads only slows the publish COPY.
See docs/evidence/explain-analyze.md.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("DROP INDEX payload_cards_run_type_idx")


def downgrade() -> None:
    op.execute("CREATE INDEX payload_cards_run_type_idx ON payload_cards (run_id, card_type)")
