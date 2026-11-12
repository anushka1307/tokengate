"""Metrics: what is counted, that labels stay bounded and private, and /metrics protection."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import SecretStr
from starlette.types import Message

from app.api.sse import EventStreamResponse
from app.config import Settings
from app.main import create_app
from app.providers.base import ProviderRequest, ProviderResponse, ProviderUsage, StreamEvent
from app.state import get_state

URL = "/v1/chat/completions"
TOKEN = "test-metrics-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def chat(**fields: Any) -> dict[str, Any]:
    return {"model": "mock-1", "messages": [{"role": "user", "content": "hello"}], **fields}


def sample(app: FastAPI, name: str, **labels: str) -> float:
    value = get_state(app).metrics.registry.get_sample_value(name, labels)
    return 0.0 if value is None else value


async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


@pytest.fixture
def app() -> FastAPI:
    return create_app(Settings(env="test", metrics_token=SecretStr(TOKEN)))


@pytest.fixture
async def http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async for c in running(app):
        yield c


# ---------------------------------------------------------------- what gets counted


async def test_completion_is_counted(app: FastAPI, http: httpx.AsyncClient) -> None:
    usage = (await http.post(URL, json=chat())).json()["usage"]

    route = {"route": URL}
    assert sample(app, "tokengate_requests_total", **route, method="POST", status_class="2xx") == 1
    assert sample(app, "tokengate_request_duration_seconds_count", **route) == 1
    assert sample(app, "tokengate_gateway_overhead_seconds_count", **route) == 1
    labels = {"provider": "mock", "model": "mock-1"}
    assert sample(app, "tokengate_provider_requests_total", **labels, outcome="success") == 1
    assert sample(app, "tokengate_provider_wait_seconds_count", provider="mock") == 1
    assert sample(app, "tokengate_tokens_total", **labels, kind="prompt") == usage["prompt_tokens"]
    assert (
        sample(app, "tokengate_tokens_total", **labels, kind="completion")
        == usage["completion_tokens"]
    )
    assert sample(app, "tokengate_inflight_requests") == 0


async def test_stream_is_counted_even_without_include_usage(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    whole = (await http.post(URL, json=chat())).json()["usage"]
    resp = await http.post(URL, json=chat(stream=True))
    assert resp.status_code == 200
    labels = {"provider": "mock", "model": "mock-1"}
    assert sample(app, "tokengate_provider_requests_total", **labels, outcome="success") == 2
    assert sample(app, "tokengate_tokens_total", **labels, kind="completion") == (
        2 * whole["completion_tokens"]
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_provider_failure_is_counted_by_kind(stream: bool) -> None:
    app = create_app(Settings(env="test", mock_error_rate=1.0))
    async for c in running(app):
        assert (await c.post(URL, json=chat(stream=stream))).status_code == 502
    labels = {"provider": "mock", "model": "mock-1"}
    assert sample(app, "tokengate_provider_requests_total", **labels, outcome="upstream_error") == 1
    assert (
        sample(app, "tokengate_requests_total", route=URL, method="POST", status_class="5xx") == 1
    )


async def test_mid_stream_failure_is_counted() -> None:
    app = create_app(Settings(env="test", mock_mid_stream_error_rate=1.0))
    async for c in running(app):
        await c.post(URL, json=chat(stream=True))
    labels = {"provider": "mock", "model": "mock-1"}
    assert sample(app, "tokengate_provider_requests_total", **labels, outcome="upstream_error") == 1


class EndlessProvider:
    name = "endless"

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("x", "stop", ProviderUsage(1, 1))

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent, None]:
        while True:
            yield StreamEvent(text_delta=" token")
            await asyncio.sleep(0.01)


async def test_client_hang_up_is_counted_as_cancelled() -> None:
    app = create_app(
        Settings(env="test", model_routes={"endless": "endless/x"}),
        providers={"endless": EndlessProvider()},
    )
    body = json.dumps(chat(model="endless", stream=True)).encode()
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    async with LifespanManager(app) as manager:
        await asyncio.wait_for(manager.app(scope, receive, send), timeout=5)
    labels = {"provider": "endless", "model": "endless"}
    assert sample(app, "tokengate_provider_requests_total", **labels, outcome="cancelled") == 1
    assert sample(app, "tokengate_inflight_requests") == 0


async def test_stream_cleanup_runs_even_if_body_never_started() -> None:
    started = False
    closed: list[str] = []

    async def body() -> AsyncGenerator[bytes, None]:
        nonlocal started
        started = True
        yield b"data: x\n\n"

    async def on_close() -> None:
        closed.append("provider closed")

    async def receive() -> Message:
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        raise OSError("client already gone")  # fails on the very first send (headers)

    response = EventStreamResponse(body(), on_close=on_close)
    await response({"type": "http"}, receive, send)
    assert started is False
    assert closed == ["provider closed"]


# ---------------------------------------------------------------- labels stay bounded


async def test_unknown_paths_and_methods_collapse_to_other(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    for i in range(5):
        await http.get(f"/scanner/probe-{i}")
    await http.request("BREW", "/healthz")

    text = (await http.get("/metrics", headers=AUTH)).text
    assert "probe-" not in text
    assert "BREW" not in text
    assert (
        sample(app, "tokengate_requests_total", route="other", method="GET", status_class="4xx")
        == 5
    )
    assert (
        sample(
            app, "tokengate_requests_total", route="/healthz", method="other", status_class="4xx"
        )
        == 1
    )


async def test_prompts_never_reach_metrics(http: httpx.AsyncClient) -> None:
    canary = "CANARY-metrics-9d1e"
    await http.post(
        URL, json={"model": "mock-1", "messages": [{"role": "user", "content": canary}]}
    )
    await http.post(URL, json={"model": canary, "messages": [{"role": "user", "content": "x"}]})
    text = (await http.get("/metrics", headers=AUTH)).text
    assert canary not in text


async def test_build_info(app: FastAPI, http: httpx.AsyncClient) -> None:
    text = (await http.get("/metrics", headers=AUTH)).text
    assert 'tokengate_build_info{version="0.1.0"} 1.0' in text


# ---------------------------------------------------------------- /metrics protection


async def test_metrics_requires_the_token(http: httpx.AsyncClient) -> None:
    assert (await http.get("/metrics")).status_code == 401
    wrong = {"Authorization": "Bearer not-the-token-at-all-xxxx"}
    assert (await http.get("/metrics", headers=wrong)).status_code == 401
    assert (await http.get("/metrics", headers={"Authorization": TOKEN})).status_code == 401
    ok = await http.get("/metrics", headers=AUTH)
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")


async def test_metrics_open_locally_without_a_token() -> None:
    async for c in running(create_app(Settings(env="local"))):
        assert (await c.get("/metrics")).status_code == 200


async def test_metrics_hidden_in_prod_without_a_token() -> None:
    async for c in running(create_app(Settings(env="prod"))):
        resp = await c.get("/metrics")
    assert resp.status_code == 404
    assert resp.json()["error"]["type"] == "not_found_error"


def test_token_is_not_exposed_in_settings_repr() -> None:
    settings = Settings(env="test", metrics_token=SecretStr(TOKEN))
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings.model_dump())


def test_short_tokens_are_rejected() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(env="test", metrics_token=SecretStr("short"))
