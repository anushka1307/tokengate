"""App factory and lifespan.

Run locally:  uv run python -m app          (reads PORT; see app/__main__.py)
         or:  uv run uvicorn app.main:create_app --factory --no-access-log
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from app import __version__, health
from app.api import chat
from app.api.errors import install_error_handlers
from app.api.limits import BodySizeLimitMiddleware
from app.auth.store import KeyStore
from app.config import Settings
from app.db.pool import create_pool
from app.providers.base import Provider
from app.providers.mock import MockConfig, MockProvider
from app.providers.router import ModelRouter
from app.ratelimit.limiter import RateLimiter, create_redis
from app.state import AppState, get_state
from app.telemetry import metrics
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


def build_providers(settings: Settings) -> dict[str, Provider]:
    """Every provider this process can route to. Real adapters (Gemini, Groq) join in Phase 3."""
    mock = MockProvider(
        MockConfig(
            first_token_delay_s=settings.mock_first_token_delay_ms / 1000,
            per_token_delay_s=settings.mock_per_token_delay_ms / 1000,
            error_rate=settings.mock_error_rate,
            mid_stream_error_rate=settings.mock_mid_stream_error_rate,
            seed=settings.mock_seed,
        )
    )
    return {mock.name: mock}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    state = get_state(app)
    state.http_client = _build_http_client(state.settings)
    if state.settings.database_url is not None:
        state.db_pool = await create_pool(state.settings)
        state.key_store = KeyStore(
            state.db_pool,
            ttl_s=state.settings.auth_cache_ttl_s,
            max_entries=state.settings.auth_cache_max_entries,
        )
    if state.settings.redis_url is not None:
        # Connects lazily, so a Redis outage at boot doesn't stop startup (rate limits fail open).
        state.redis = create_redis(state.settings)
        state.rate_limiter = RateLimiter(
            state.redis,
            key_prefix=state.settings.redis_key_prefix,
            cooldown_s=state.settings.ratelimit_cooldown_s,
            timeout_s=state.settings.redis_connect_timeout_s + state.settings.redis_timeout_s,
            metrics=state.metrics,
        )
    else:
        log.warning("ratelimit_disabled", reason="TOKENGATE_REDIS_URL is not set")
    state.providers = dict(state.provider_overrides or build_providers(state.settings))
    # Raises (and so stops startup) if a route points at a provider that doesn't exist.
    state.model_router = ModelRouter(state.settings.model_routes, state.providers)
    state.ready = True
    log.info("startup_complete", env=state.settings.env, version=__version__)
    try:
        yield
    finally:
        # uvicorn calls this after it has stopped accepting connections and in-flight requests
        # have finished (bounded by --timeout-graceful-shutdown). Release resources last.
        state.ready = False
        state.model_router = None
        log.info("shutdown_started")
        await state.http_client.aclose()
        state.http_client = None
        state.key_store = None
        state.rate_limiter = None
        if state.redis is not None:
            try:
                await asyncio.wait_for(state.redis.aclose(), timeout=2)
            except TimeoutError:
                log.warning("redis_close_timed_out")
            state.redis = None
        if state.db_pool is not None:
            # Bounded: don't let a stuck connection eat Cloud Run's 10 s shutdown window.
            try:
                await asyncio.wait_for(state.db_pool.close(), timeout=3)
            except TimeoutError:
                state.db_pool.terminate()
            state.db_pool = None
        log.info("shutdown_complete")


def create_app(
    settings: Settings | None = None, providers: Mapping[str, Provider] | None = None
) -> FastAPI:
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
    state = AppState(settings=settings, provider_overrides=providers)
    app.state.tg = state
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(metrics.router)
    install_error_handlers(app)
    # Starlette runs the LAST-added middleware outermost. Request context must wrap the size
    # limit so that 413 responses still get a request ID and an access-log line.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestContextMiddleware, metrics=state.metrics)
    return app
