"""First-run bootstrap: create the schema.

Deliberately minimal — ``create_all`` is enough for the MVP. When the model
stabilises this is where an Alembic migration would take over.
"""

from __future__ import annotations

# Import models so they register on the metadata before create_all.
from app import models  # noqa: F401
from app.db import Base, engine
from app.logging import get_logger

logger = get_logger(__name__)


async def create_schema() -> None:
    async with engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("schema.ready")
