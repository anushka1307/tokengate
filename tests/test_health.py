import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.state import get_state


async def test_healthz_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_ready_after_startup(client: httpx.AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


async def test_readyz_not_ready_before_startup(app: FastAPI) -> None:
    # No LifespanManager: startup never ran.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready"}


def _is_closed(client: httpx.AsyncClient) -> bool:
    # A function call isn't narrowed by mypy, unlike reading the property twice.
    return client.is_closed


async def test_shutdown_marks_not_ready_and_closes_http_client(app: FastAPI) -> None:
    async with LifespanManager(app):
        client = get_state(app).http_client
        assert client is not None
        assert not _is_closed(client)

    state = get_state(app)
    assert state.ready is False
    assert state.http_client is None
    assert _is_closed(client)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 503


async def test_healthz_does_not_depend_on_readiness(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
