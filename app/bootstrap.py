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


async def create_schema() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Early builds created person.telegram_id as INTEGER; Telegram user ids
        # exceed the 32-bit range. create_all won't alter an existing column, so
        # widen it here. Idempotent — a BIGINT column altered to BIGINT is a
        # no-op. Postgres only; SQLite has no fixed integer width.
        if conn.dialect.name == "postgresql":
            try:
                await conn.execute(
                    text("ALTER TABLE person ALTER COLUMN telegram_id TYPE BIGINT")
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("schema.widen_telegram_id_failed", error=str(exc))
    logger.info("schema.ready")