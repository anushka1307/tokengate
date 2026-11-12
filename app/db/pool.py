"""The shared Postgres connection pool (docs/DECISIONS.md, D30-D31).

Every database call is bounded twice: a client-side timeout (asyncpg) and Postgres's own
``statement_timeout``, so a slow query can't hold a request (or a connection) indefinitely.
"""

import asyncpg
import structlog

from app.config import Settings

log = structlog.get_logger("tokengate.db")


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create the pool without connecting: a database outage must not stop the app booting."""
    if settings.database_url is None:  # pragma: no cover - callers check first
        raise ValueError("database_url is not set")
    query_timeout_ms = int(settings.db_query_timeout_s * 1000)
    return await asyncpg.create_pool(
        dsn=settings.database_url.get_secret_value(),
        min_size=settings.db_pool_min_size,
        init_size=settings.db_pool_min_size,  # no connections opened at startup when 0
        max_size=settings.db_pool_max_size,
        max_inactive_connection_lifetime=300.0,
        timeout=settings.db_connect_timeout_s,  # connecting
        command_timeout=settings.db_query_timeout_s,  # each query, client side
        server_settings={
            "application_name": "tokengate",
            "statement_timeout": str(query_timeout_ms),  # each query, enforced by Postgres
            "idle_in_transaction_session_timeout": str(query_timeout_ms * 5),
        },
    )


async def is_healthy(pool: asyncpg.Pool, timeout_s: float) -> bool:
    """True if a connection can be acquired and answer ``SELECT 1`` within ``timeout_s``."""
    try:
        async with pool.acquire(timeout=timeout_s) as conn:
            return bool(await conn.fetchval("SELECT 1", timeout=timeout_s) == 1)
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        # The class name only: messages can include host names or connection details.
        log.warning("database_unhealthy", error=type(exc).__name__)
        return False
