"""App factory and lifespan.

Run locally:  uv run uvicorn app.main:create_app --factory --no-access-log
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from app import __version__, health
from app.api.errors import install_error_handlers
from app.api.limits import BodySizeLimitMiddleware
from app.config import Settings
from app.state import AppState, get_state
from app.telemetry.logging import configure_logging
from app.telemetry.middleware import RequestContextMiddleware

log = structlog.get_logger("tokengate")


def _build_http_client(settings: Settings) -> httpx.AsyncClient:
    """One pooled client per process, shared by all provider adapters."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.http_connect_timeout_s,
            read=settings.http_read_timeout_s,
            write=settings.http_write_timeout_s,
            pool=settings.http_pool_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = get_state(app)
    state.http_client = _build_http_client(state.settings)
    state.ready = True
    log.info("startup_complete", env=state.settings.env, version=__version__)
    try:
        yield
    finally:
        # uvicorn calls this after it has stopped accepting connections and in-flight requests
        # have finished (bounded by --timeout-graceful-shutdown). Release resources last.
        state.ready = False
        log.info("shutdown_started")
        await state.http_client.aclose()
        state.http_client = None
        log.info("shutdown_complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    configure_logging(settings.log_level)

    is_prod = settings.env == "prod"
    app = FastAPI(
        title="TokenGate",
        version=__version__,
        lifespan=lifespan,
        # Don't publish the API schema or interactive docs from production.
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    app.state.tg = AppState(settings=settings)
    app.include_router(health.router)
    install_error_handlers(app)
    # Starlette runs the LAST-added middleware outermost. Request context must wrap the size
    # limit so that 413 responses still get a request ID and an access-log line.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    return app
