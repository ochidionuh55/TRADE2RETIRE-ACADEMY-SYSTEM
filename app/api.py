"""HTTP surface.

For the MVP this is a health endpoint and the schema bootstrap on startup. The
authenticated student/staff/CEO web portal (Phase 2) will consume the same
services layer this bot already uses — one backend, many surfaces.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.migrate import run_migrations

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Alembic owns the schema. The advisory lock serialises with the worker so
    # concurrent boots are safe; adoption/upgrade is idempotent.
    result = await run_migrations()
    logger.info("api.ready", **result)
    yield


app = FastAPI(title="T2R OS", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "t2r-os", "academy": get_settings().academy_name}


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "T2R OS", "docs": "/docs", "health": "/health"}
