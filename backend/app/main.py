from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.annotation import page_router as annotation_page_router
from app.api.annotation import router as annotation_router
from app.api.health import router as health_router
from app.api.retrieval import router as retrieval_router
from app.api.runs import router as runs_router
from app.api.sources import router as sources_router
from app.core.config import get_settings
from app.core.db import close_database
from app.core.logging import configure_logging
from app.core.queue import close_run_queue
from app.core.redis import close_redis
from app.core.trace import TraceIdMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    yield
    await close_run_queue()
    await close_redis()
    await close_database()


def create_app() -> FastAPI:
    app = FastAPI(title="WorkPilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TraceIdMiddleware)
    app.include_router(annotation_page_router)
    app.include_router(annotation_router)
    app.include_router(health_router)
    app.include_router(retrieval_router)
    app.include_router(runs_router)
    app.include_router(sources_router)
    return app


app = create_app()
