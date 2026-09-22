"""Alembic ownership of the schema — the migrations cutover (Slice 1.5).

Boot calls :func:`run_migrations`. It is safe on three kinds of database:

* **A pre-Alembic production database** (built by the old ``create_all`` +
  boot-time shim): it already has every table and column, so we *adopt* it —
  stamp the baseline revision, writing no DDL and touching no data.
* **A fresh database** (new environment): no tables yet, so we *upgrade* to
  head, creating everything from the versioned migrations.
* **An already-migrated database**: ``upgrade head`` is a no-op until a new
  revision exists.

The choice is made by inspection, so the same code is forward-safe and
idempotent at deployment level. On Postgres a session-level advisory lock
serialises concurrent callers (worker and api booting together) so exactly one
performs the work. Alembic runs synchronously (its native mode); boot code
calls it in a worker thread.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import create_engine, inspect, pool

from alembic import command
from alembic.config import Config
from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = str(_ROOT / "alembic.ini")
_SCRIPT_LOCATION = str(_ROOT / "alembic")
# Arbitrary, app-specific advisory-lock id so concurrent boots serialise.
_LOCK_KEY = 727_201


def sync_url(url: str) -> str:
    """Map the app's async DSN to the sync driver Alembic uses."""
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg2://" + url[len("postgresql+asyncpg://"):]
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url[len("sqlite+aiosqlite://"):]
    return url


def _config(url: str) -> Config:
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("script_location", _SCRIPT_LOCATION)
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _read_revision(url: str) -> str | None:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    finally:
        engine.dispose()


def run_migrations_sync() -> dict[str, str]:
    """Adopt-or-upgrade the schema. Returns {action, revision}.

    The advisory lock lives on its own dedicated connection so concurrent boots
    serialise; Alembic runs on its own connection (its native mode), so the two
    never contend for a transaction on the same connection — the failure mode
    that only surfaces on Postgres.
    """
    url = sync_url(get_settings().database_url)
    is_pg = url.startswith("postgresql")

    lock_engine = None
    lock_conn = None
    try:
        if is_pg:
            lock_engine = create_engine(url, poolclass=pool.NullPool)
            lock_conn = lock_engine.connect()
            # Session-level lock, held until this connection closes.
            lock_conn.exec_driver_sql("SELECT pg_advisory_lock(%(k)s)", {"k": _LOCK_KEY})
            lock_conn.commit()

        # Decide adopt vs upgrade on a short-lived connection of its own.
        probe = create_engine(url, poolclass=pool.NullPool)
        try:
            with probe.connect() as conn:
                insp = inspect(conn)
                has_version = insp.has_table("alembic_version")
                has_person = insp.has_table("person")
        finally:
            probe.dispose()

        cfg = _config(url)  # no shared connection: Alembic uses its own engine
        if not has_version and has_person:
            # Existing, pre-Alembic schema: adopt it without any DDL.
            command.stamp(cfg, "head")
            action = "adopted"
        else:
            # Fresh or already-migrated: apply migrations (no-op if current).
            command.upgrade(cfg, "head")
            action = "upgraded"

        revision = _read_revision(url)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%(k)s)", {"k": _LOCK_KEY}
                )
                lock_conn.commit()
            except Exception as exc:  # noqa: BLE001 - unlock is best-effort
                logger.warning("migrate.unlock_failed", error=str(exc))
            lock_conn.close()
        if lock_engine is not None:
            lock_engine.dispose()

    logger.info("migrate.done", action=action, revision=revision or "")
    return {"action": action, "revision": revision or ""}


async def run_migrations() -> dict[str, str]:
    """Async entry point — runs the sync migration in a thread."""
    return await asyncio.to_thread(run_migrations_sync)
