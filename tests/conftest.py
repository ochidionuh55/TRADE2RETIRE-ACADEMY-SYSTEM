"""Test fixtures — an isolated SQLite database per test.

Each test gets a fresh file-backed SQLite database wired through the app's own
engine, so services and models run exactly as they do in production (minus the
Postgres-only column patches, which the fresh schema doesn't need).
"""

from __future__ import annotations

import pytest

from app import db as db_module
from app.config import get_settings


@pytest.fixture()
async def db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 't2r_test.db'}"
    monkeypatch.setenv("T2R__DATABASE_URL", url)
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None

    # Tests run against the real migration path (Alembic upgrade to head), so
    # they exercise exactly what production boot does.
    from app.migrate import run_migrations

    await run_migrations()
    yield

    engine = db_module._engine
    if engine is not None:
        await engine.dispose()
    db_module._engine = None
    db_module._sessionmaker = None
    get_settings.cache_clear()
