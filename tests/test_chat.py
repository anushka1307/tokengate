"""POST /v1/chat/completions over HTTP (in-process), plus a raw-ASGI client-disconnect test."""

import asyncio
import io
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from starlette.types import Message

from app.config import Settings
from app.main import create_app
from app.providers.base import ProviderRequest, ProviderResponse, ProviderUsage, StreamEvent
from app.telemetry.logging import configure_logging
from app.telemetry.middleware import OVERHEAD_HEADER, REQUEST_ID_HEADER

URL = "/v1/chat/completions"


def chat(**fields: Any) -> dict[str, Any]:
    return {"model": "mock-1", "messages": [{"role": "user", "content": "hello"}], **fields}


async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async for c in client_for(create_app(Settings(env="test"))):
        yield c


def sse_payloads(body: str) -> list[Any]:
    """Parse an SSE body into JSON payloads; the [DONE] marker becomes the string "[DONE]"."""
    out: list[Any] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        assert block.startswith("data: "), block
        data = block[len("data: ") :]
        out.append(data if data == "[DONE]" else json.loads(data))
    return out


def streamed_text(payloads: list[Any]) -> str:
    return "".join(
        p["choices"][0]["delta"].get("content") or ""
        for p in payloads
        if isinstance(p, dict) and p.get("choices")
    )


# ---------------------------------------------------------------- non-streaming


async def test_completion_shape_and_headers(http: httpx.AsyncClient) -> None:
    resp = await http.post(URL, json=chat())
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["id"].startswith("chatcmpl-")
    assert data["model"] == "mock-1"  # the alias, not the provider's model name
    (choice,) = data["choices"]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].startswith("Mock answer")
    assert choice["finish_reason"] == "stop"
    usage = data["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

    assert resp.headers["X-Provider"] == "mock"
    assert resp.headers["X-Cache"] == "miss"
    assert REQUEST_ID_HEADER in resp.headers


async def test_same_question_same_answer(http: httpx.AsyncClient) -> None:
    first = (await http.post(URL, json=chat())).json()["choices"][0]["message"]["content"]
    second = (await http.post(URL, json=chat())).json()["choices"][0]["message"]["content"]
    assert first == second


async def test_output_is_always_capped() -> None:
    app = create_app(Settings(env="test", max_completion_tokens=10))
    async for c in client_for(app):
        # No max_tokens in the request: the configured cap still applies.
        data = (await c.post(URL, json=chat())).json()
        assert data["usage"]["completion_tokens"] <= 10
        small = (await c.post(URL, json=chat(max_completion_tokens=3))).json()
        assert small["usage"]["completion_tokens"] == 3
        assert small["choices"][0]["finish_reason"] == "length"


async def test_provider_failure_is_a_502_without_details() -> None:
    async for c in client_for(create_app(Settings(env="test", mock_error_rate=1.0))):
        resp = await c.post(URL, json=chat())
    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "upstream_error"
    assert error["type"] == "api_error"
    assert "simulated" not in resp.text  # upstream detail stays in our logs only


async def test_overhead_excludes_provider_latency() -> None:
    delay_ms = 200
    app = create_app(Settings(env="test", mock_first_token_delay_ms=delay_ms))
    async for c in client_for(app):
        for stream in (False, True):
            resp = await c.post(URL, json=chat(stream=stream))
            assert resp.status_code == 200
            # Loose bound so slow CI machines don't flake; the provider alone takes 200 ms.
            assert float(resp.headers[OVERHEAD_HEADER]) < delay_ms / 2


# ---------------------------------------------------------------- request rules are wired in


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        (chat(model="gpt-5-expensive"), 400, "model_not_found"),
        (chat(tools=[{"type": "function"}]), 400, "unsupported_parameter"),
        (chat(temperature=7), 400, "invalid_value"),
    ],
)
async def test_request_rules_apply(
    http: httpx.AsyncClient, payload: dict[str, Any], status: int, code: str
) -> None:
    resp = await http.post(URL, json=payload)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


async def test_not_ready_before_startup() -> None:
    app = create_app(Settings(env="test"))  # lifespan never runs: no router yet
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post(URL, json=chat())
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "not_ready"


# ---------------------------------------------------------------- streaming


async def test_stream_format_and_text_matches_non_stream(http: httpx.AsyncClient) -> None:
    whole = (await http.post(URL, json=chat())).json()
    resp = await http.post(URL, json=chat(stream=True))

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["X-Provider"] == "mock"
    payloads = sse_payloads(resp.text)
    assert payloads[-1] == "[DONE]"
    chunks = payloads[:-1]
    assert all(p["object"] == "chat.completion.chunk" for p in chunks)
    assert len({p["id"] for p in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert streamed_text(chunks) == whole["choices"][0]["message"]["content"]
    assert all(p.get("usage") is None for p in chunks)  # no usage unless asked for


async def test_stream_usage_chunk_only_when_requested(http: httpx.AsyncClient) -> None:
    whole = (await http.post(URL, json=chat())).json()
    resp = await http.post(URL, json=chat(stream=True, stream_options={"include_usage": True}))
    chunks = sse_payloads(resp.text)[:-1]
    usage_chunk = chunks[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == whole["usage"]


async def test_stream_failure_before_first_token_is_a_real_502() -> None:
    async for c in client_for(create_app(Settings(env="test", mock_error_rate=1.0))):
        resp = await c.post(URL, json=chat(stream=True))
    assert resp.status_code == 502
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "upstream_error"


async def test_stream_failure_mid_way_sends_error_event_and_no_done() -> None:
    app = create_app(Settings(env="test", mock_mid_stream_error_rate=1.0))
    async for c in client_for(app):
        resp = await c.post(URL, json=chat(stream=True))
    assert resp.status_code == 200  # already committed before the failure
    payloads = sse_payloads(resp.text)
    assert "[DONE]" not in payloads
    assert payloads[-1] == {
        "error": {
            "message": "The model provider returned an error.",
            "type": "api_error",
            "param": None,
            "code": "upstream_error",
        }
    }
    assert streamed_text(payloads[:-1])  # some text arrived before the failure


# ---------------------------------------------------------------- client disconnect


class EndlessProvider:
    """Streams slowly forever and records whether it was closed."""

    name = "endless"

    def __init__(self) -> None:
        self.yielded = 0
        self.closed = False

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse("x", "stop", ProviderUsage(1, 1))

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent, None]:
        try:
            while True:
                self.yielded += 1
                yield StreamEvent(text_delta=" token")
                await asyncio.sleep(0.01)
        finally:
            self.closed = True


async def test_client_disconnect_stops_the_provider_stream() -> None:
    provider = EndlessProvider()
    app = create_app(
        Settings(env="test", model_routes={"endless": "endless/x"}),
        providers={"endless": provider},
    )
    body = json.dumps(chat(model="endless", stream=True)).encode()
    first_chunk_sent = asyncio.Event()
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        await first_chunk_sent.wait()  # the client hangs up once the stream has started
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},  # the version Starlette ignores
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": URL,
        "raw_path": URL.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    async with LifespanManager(app) as manager:
        # Without disconnect handling this would never finish (the provider is endless).
        await asyncio.wait_for(manager.app(scope, receive, send), timeout=5)

    assert provider.closed
    assert provider.yielded < 100


async def test_complete_stream_then_close_is_not_logged_as_a_hang_up(
    http: httpx.AsyncClient, log_output: io.StringIO
) -> None:
    # httpx's test transport reports "disconnect" once the response is finished, like an SDK
    # closing the connection right after [DONE]. That must not count as a mid-stream hang-up.
    resp = await http.post(URL, json=chat(stream=True))
    assert resp.status_code == 200
    assert "client_disconnected_mid_stream" not in log_output.getvalue()


async def test_real_hang_up_is_logged(log_output: io.StringIO) -> None:
    provider = EndlessProvider()
    app = create_app(
        Settings(env="test", model_routes={"endless": "endless/x"}),
        providers={"endless": provider},
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
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }
    async with LifespanManager(app) as manager:
        configure_logging("INFO", stream=log_output)  # app startup reset logging; capture again
        await asyncio.wait_for(manager.app(scope, receive, send), timeout=5)
    assert "client_disconnected_mid_stream" in log_output.getvalue()
