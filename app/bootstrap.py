"""First-run bootstrap: create the schema.

Deliberately minimal — ``create_all`` is enough for the MVP. When the model
stabilises this is where an Alembic migration would take over.
"""

from __future__ import annotations

from sqlalchemy import text

# Import models so they register on the metadata before create_all.
from app import models  # noqa: F401
from app.db import Base, engine
from app.logging import get_logger

logger = get_logger(__name__)


# ⚠️ TEMPORARY COMPATIBILITY SHIM — NOT the schema-management strategy.
#
# create_all only creates *missing tables*; it never alters an existing one, so
# a new column on a pre-existing table is patched here on boot. This is a
# deliberate prototype crutch to keep early deploys moving. It is scheduled to
# be REMOVED in the migrations cutover: once Alembic owns schema evolution,
# every change becomes a versioned, auditable migration and this list goes away.
# Until then: additive only (never a drop/alter-away), Postgres-only (SQLite
# tests build the current schema fresh), and each patch guarded so a boot never
# fails on one already applied.
_COLUMN_PATCHES: tuple[str, ...] = (
    # Telegram user ids exceed the 32-bit signed range.
    "ALTER TABLE person ALTER COLUMN telegram_id TYPE BIGINT",
    # Demo-data isolation (Increment 2): existing rows default to non-demo.
    "ALTER TABLE person ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE cohort ADD COLUMN IF NOT EXISTS is_demo BOOLEAN NOT NULL DEFAULT false",
)


async def create_schema() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            for patch in _COLUMN_PATCHES:
                try:
                    await conn.execute(text(patch))
                except Exception as exc:  # noqa: BLE001 - defensive, never fatal
                    logger.warning("schema.column_patch_failed", patch=patch, error=str(exc))
    logger.info("schema.ready")
