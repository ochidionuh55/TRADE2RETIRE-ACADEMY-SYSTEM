"""Alembic environment.

Uses the app's models as the target metadata and, when boot code shares a live
connection via ``config.attributes['connection']`` (see app.migrate), runs
against that connection. Otherwise it builds its own sync engine from the
configured URL — the mode the ``alembic`` CLI uses in development.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.migrate import sync_url

# Import Base with every model registered so autogenerate/target sees them all.
from app.models import Base  # noqa: E402  (import after alembic context is fine)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    # Prefer the app's real DSN unless the CLI explicitly set a dev URL.
    if configured and configured != "sqlite:///dev_alembic.db":
        return configured
    return sync_url(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        _do_run(connectable)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _do_run(connection)
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
