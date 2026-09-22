"""Model-defined schema (``create_all``).

As of the migrations cutover (Slice 1.5), production boot no longer calls this:
Alembic (see ``app.migrate``) owns schema evolution, versioned and auditable.
``create_schema`` remains as the authoritative model-defined schema — used by
the migration-equivalence test (which proves the Alembic baseline reproduces it
exactly) and by local tooling. The temporary boot-time column shim that lived
here has been removed now that equivalence is proven.
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
