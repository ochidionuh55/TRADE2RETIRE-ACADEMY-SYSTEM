"""Slice 1.5 — Alembic cutover integrity.

Proves, without ever touching production:
  • the Alembic baseline reproduces the model-defined (create_all) schema exactly;
  • an existing, pre-Alembic database is ADOPTED (stamped) with no DDL and no
    data loss;
  • migration is idempotent (a second run is a no-op);
  • a fresh database is created from the migrations.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from alembic.script import ScriptDirectory
from app import db as db_module
from app.bootstrap import Base, create_schema  # noqa: F401  (Base via bootstrap)
from app.config import get_settings
from app.migrate import _config, run_migrations_sync, sync_url


def _head(url: str) -> str:
    """The current Alembic head revision (moves as slices add migrations)."""
    return ScriptDirectory.from_config(_config(url)).get_current_head()


def _use_db(monkeypatch, path) -> str:
    """Point the app settings at a sqlite file; return its sync URL."""
    async_url = f"sqlite+aiosqlite:///{path}"
    monkeypatch.setenv("T2R__DATABASE_URL", async_url)
    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    return sync_url(async_url)


def _snapshot(sync_engine_url: str) -> dict:
    """Structural schema snapshot (name-insensitive for auto-named objects)."""
    engine = create_engine(sync_engine_url)
    insp = inspect(engine)
    out: dict = {}
    for table in insp.get_table_names():
        if table == "alembic_version":
            continue
        cols = {
            c["name"]: (str(c["type"]), bool(c["nullable"]))
            for c in insp.get_columns(table)
        }
        pk = tuple(insp.get_pk_constraint(table).get("constrained_columns") or [])
        uniques = sorted(
            tuple(sorted(u["column_names"])) for u in insp.get_unique_constraints(table)
        )
        indexes = sorted(
            (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)
        )
        out[table] = {"cols": cols, "pk": pk, "uniques": uniques, "indexes": indexes}
    engine.dispose()
    return out


def test_baseline_matches_create_all_schema(tmp_path, monkeypatch) -> None:
    # A: schema built by running the migrations.
    url_mig = _use_db(monkeypatch, tmp_path / "mig.db")
    result = run_migrations_sync()
    assert result["action"] == "upgraded"
    assert result["revision"] == _head(url_mig)
    migrated = _snapshot(url_mig)

    # B: schema built directly from the models (create_all).
    url_ca = f"sqlite:///{tmp_path / 'createall.db'}"
    ca_engine = create_engine(url_ca)
    Base.metadata.create_all(ca_engine)
    ca_engine.dispose()
    created = _snapshot(url_ca)

    assert set(migrated) == set(created), "table sets differ"
    for table in created:
        assert migrated[table] == created[table], f"schema differs on table {table!r}"


def test_adopts_existing_schema_without_data_loss(tmp_path, monkeypatch) -> None:
    url = _use_db(monkeypatch, tmp_path / "prod_like.db")

    # Simulate a pre-Alembic production DB: schema via create_all, with data.
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    from app.models import Cohort, FridayReport, Person  # local import: after schema

    with Session(engine) as s:
        p = Person(full_name="Existing Student", is_demo=False)
        s.add(p)
        s.add(Cohort(name="Existing Cohort", programme="The Architect"))
        s.flush()
        s.add(
            FridayReport(
                person_id=p.id,
                week_ending=date(2026, 9, 18),
                answers={"lesson": "patience"},
                submitted_at=datetime.now(UTC),
            )
        )
        s.commit()

    def counts() -> tuple[int, int, int]:
        with Session(engine) as s:
            return (
                s.execute(select(func.count()).select_from(Person)).scalar_one(),
                s.execute(select(func.count()).select_from(Cohort)).scalar_one(),
                s.execute(select(func.count()).select_from(FridayReport)).scalar_one(),
            )

    before = counts()
    assert before == (1, 1, 1)

    # First migration run: it must ADOPT (stamp), not recreate.
    result = run_migrations_sync()
    assert result["action"] == "adopted"
    assert result["revision"] == _head(url)
    assert inspect(engine).has_table("alembic_version")
    assert counts() == before, "adoption must preserve every row"

    # Second run: idempotent no-op (now versioned → upgrade head, already current).
    result2 = run_migrations_sync()
    assert result2["action"] == "upgraded"
    assert result2["revision"] == _head(url)
    assert counts() == before

    engine.dispose()


def test_fresh_database_is_created_by_migrations(tmp_path, monkeypatch) -> None:
    url = _use_db(monkeypatch, tmp_path / "fresh.db")
    result = run_migrations_sync()
    assert result["action"] == "upgraded"
    assert result["revision"] == _head(url)
    assert inspect(create_engine(url)).has_table("person")
