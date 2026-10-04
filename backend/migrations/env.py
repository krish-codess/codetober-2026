import os

from alembic import context
from sqlalchemy import create_engine

# Migrations run as the owner role; the services use the restricted DATABASE_URL.
url = os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
