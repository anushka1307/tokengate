import io
import os
import uuid
from collections.abc import AsyncIterator

# Tests run without API-key auth unless a test turns it on explicitly (auth="required").
# "disabled" is refused in prod by Settings, so this can't leak into a real deployment.
os.environ["TOKENGATE_AUTH"] = "disabled"

import asyncpg
import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.config import Settings
from app.main import create_app
from app.telemetry.logging import configure_logging


@pytest.fixture
def settings() -> Settings:
    # Explicit kwargs take priority over any TOKENGATE_* vars in the developer's shell.
    return Settings(env="test", log_level="INFO")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired straight into the ASGI app, with real startup/shutdown."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


@pytest.fixture
def log_output(app: FastAPI) -> io.StringIO:
    """Capture JSON log lines. Depends on ``app`` so it runs after create_app's own setup."""
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    return buffer


TEST_DB_URL = os.environ.get("TOKENGATE_TEST_DATABASE_URL")
REQUIRE_DB = os.environ.get("TOKENGATE_REQUIRE_DB_TESTS") == "1"


@pytest.fixture
async def pg() -> AsyncIterator[str]:
    """A DSN whose search_path is a fresh, empty schema (dropped afterwards)."""
    if not TEST_DB_URL:
        message = "set TOKENGATE_TEST_DATABASE_URL to run database tests"
        if REQUIRE_DB:
            pytest.fail(message)
        pytest.skip(message)
    schema = f"test_{uuid.uuid4().hex[:12]}"  # generated, never user input
    admin = await asyncpg.connect(TEST_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        sep = "&" if "?" in TEST_DB_URL else "?"
        yield f"{TEST_DB_URL}{sep}search_path={schema}"
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
