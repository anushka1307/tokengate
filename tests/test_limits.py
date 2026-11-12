"""BodySizeLimitMiddleware, tested at the raw ASGI level (so we control chunks and headers)
and once through the full app."""

from typing import Any

import httpx
from starlette.types import Message, Receive, Scope, Send

from app.api.limits import BodySizeLimitMiddleware
from app.config import Settings
from app.main import create_app
from app.telemetry.middleware import REQUEST_ID_HEADER

LIMIT = 100


class Downstream:
    """Records the body it receives, then answers 200."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.after_body: list[Message] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        data = b""
        while True:
            message = await receive()
            data += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.bodies.append(data)
        self.after_body.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def run(
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]] | None = None,
    disconnect_after: int | None = None,
) -> tuple[Downstream, list[Message]]:
    downstream = Downstream()
    middleware = BodySizeLimitMiddleware(downstream, max_bytes=LIMIT)
    messages: list[Message] = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    if disconnect_after is not None:
        messages = messages[:disconnect_after]
    queue = iter(messages)

    async def receive() -> Message:
        return next(queue, {"type": "http.disconnect"})

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers or [],
        "query_string": b"",
    }
    await middleware(scope, receive, send)
    return downstream, sent


def status_of(sent: list[Message]) -> int | None:
    return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)


async def test_body_under_limit_is_replayed_intact() -> None:
    downstream, sent = await run([b"a" * 40, b"b" * 60])  # exactly LIMIT, in two chunks
    assert status_of(sent) == 200
    assert downstream.bodies == [b"a" * 40 + b"b" * 60]
    # After the body, receive() passes through (streaming responses rely on this).
    assert downstream.after_body == [{"type": "http.disconnect"}]


async def test_declared_length_over_limit_rejected_without_reading() -> None:
    downstream, sent = await run([b"x"], headers=[(b"content-length", b"5000")])
    assert status_of(sent) == 413
    assert downstream.bodies == []


async def test_chunked_body_over_limit_rejected() -> None:
    # No Content-Length header (chunked transfer): must count while reading.
    downstream, sent = await run([b"x" * 60, b"x" * 60])
    assert status_of(sent) == 413
    assert downstream.bodies == []


async def test_lying_content_length_rejected() -> None:
    downstream, sent = await run([b"x" * 150], headers=[(b"content-length", b"10")])
    assert status_of(sent) == 413
    assert downstream.bodies == []


async def test_malformed_content_length_rejected() -> None:
    # b"\xb2" is "²" and b"\xb9" is "¹" in latin-1: str.isdigit() says yes, int() crashes.
    # Regression test for a bug found in self-review (was a 500, must be a 400).
    for bad in (b"-1", b"abc", b"1" * 40, b"\xb2", b"1\xb9", b" 12", b"12 ", b""):
        downstream, sent = await run([b"{}"], headers=[(b"content-length", bad)])
        assert status_of(sent) == 400
        assert downstream.bodies == []


async def test_client_disconnect_mid_upload_sends_nothing() -> None:
    downstream, sent = await run([b"x" * 10, b"x" * 10], disconnect_after=1)
    assert sent == []
    assert downstream.bodies == []


async def test_413_through_full_app_has_openai_shape_and_request_id() -> None:
    app = create_app(Settings(env="test", max_body_bytes=LIMIT))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.post("/healthz", content=b"x" * (LIMIT + 1))
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "request_too_large"
    assert REQUEST_ID_HEADER in resp.headers
