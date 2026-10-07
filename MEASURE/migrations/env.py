"""Alembic environment. Migrations are hand-written SQL; there is no ORM metadata to autogenerate from."""

from alembic import context
from sqlalchemy import create_engine

from wrapped import config

url = config.load().migrate_database_url.replace("postgresql://", "postgresql+psycopg://", 1)

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
