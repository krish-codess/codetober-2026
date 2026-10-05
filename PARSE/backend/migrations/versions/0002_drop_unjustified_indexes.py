"""Drop two indexes that EXPLAIN ANALYZE could not justify.

Measured on the seeded database (docs/explain/README.md):
  jobs_queued            0.07 ms with, 0.11 ms without  - the table holds a handful of rows
  taxonomy_nodes_parent  0.05 ms with, 0.05 ms without  - 241 rows fit in three pages
An index that does not make its query faster still costs every write. Recreate them if the
tables grow by orders of magnitude (the downgrade does exactly that).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jobs_queued")
    op.execute("DROP INDEX IF EXISTS taxonomy_nodes_parent")


def downgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS taxonomy_nodes_parent ON taxonomy_nodes (parent_id)")
    op.execute("CREATE INDEX IF NOT EXISTS jobs_queued ON jobs (requested_at) WHERE status = 'queued'")
