import io
from collections.abc import AsyncIterator

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
