"""Error envelope, status codes, and request limits, exercised over HTTP through the real app."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.limits import enforce_request_limits
from app.api.schemas import ChatCompletionRequest
from app.config import Settings
from app.main import create_app
from app.telemetry.middleware import REQUEST_ID_HEADER

CANARY = "CANARY-PROMPT-7f3a"


def build_app(**overrides: Any) -> FastAPI:
    settings = Settings(env="test", **overrides)
    app = create_app(settings)

    # Test-only route standing in for /v1/chat/completions until step 1.4.
    @app.post("/test/chat")
    async def chat(body: ChatCompletionRequest) -> dict[str, bool]:
        enforce_request_limits(body, settings)
        return {"ok": True}

    @app.get("/test/boom")
    async def boom() -> None:
        raise RuntimeError(f"internal detail {CANARY}")

    return app


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(max_messages=3, max_input_chars=50, max_completion_tokens=100)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def body(**fields: Any) -> dict[str, Any]:
    return {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}], **fields}


def assert_openai_error(resp: httpx.Response, status: int) -> dict[str, Any]:
    assert resp.status_code == status
    payload = resp.json()
    assert set(payload) == {"error"}
    error: dict[str, Any] = payload["error"]
    assert set(error) == {"message", "type", "param", "code"}
    assert isinstance(error["message"], str) and error["message"]
    return error


async def test_valid_request_passes(http: httpx.AsyncClient) -> None:
    resp = await http.post("/test/chat", json=body())
    assert resp.status_code == 200


async def test_invalid_json(http: httpx.AsyncClient) -> None:
    resp = await http.post(
        "/test/chat", content=b"{not json", headers={"content-type": "application/json"}
    )
    error = assert_openai_error(resp, 400)
    assert error["code"] == "invalid_json"
    assert error["type"] == "invalid_request_error"


async def test_missing_body(http: httpx.AsyncClient) -> None:
    error = assert_openai_error(await http.post("/test/chat"), 400)
    assert error["code"] == "missing_required_parameter"


@pytest.mark.parametrize(
    ("payload", "param", "code"),
    [
        (body(temperature=5), "temperature", "invalid_value"),
        ({"messages": [{"role": "user", "content": "hi"}]}, "model", "missing_required_parameter"),
        (body(tools=[{"type": "function"}]), "tools", "unsupported_parameter"),
        (body(n=3), "n", "unsupported_parameter"),
        (
            body(messages=[{"role": "tool", "content": "x", "tool_call_id": "1"}]),
            "messages[0].role",
            "unsupported_parameter",
        ),
        (
            body(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]),
            "messages[0].content",
            "unsupported_parameter",
        ),
        (body(stream_options={"include_usage": True}), "stream_options", "invalid_value"),
        # pydantic's union-branch names ("str", "list[...]") must not leak into `param`.
        (
            body(messages=[{"role": "user", "content": 12345}]),
            "messages[0].content",
            "invalid_value",
        ),
        (
            # Fails both union branches; pydantic lists the "str" branch first, so we report
            # the content field itself (previously leaked as "messages[0].content.str").
            body(messages=[{"role": "user", "content": [{"type": "text"}]}]),
            "messages[0].content",
            "invalid_value",
        ),
    ],
)
async def test_validation_errors_name_the_param(
    http: httpx.AsyncClient, payload: dict[str, Any], param: str, code: str
) -> None:
    error = assert_openai_error(await http.post("/test/chat", json=payload), 400)
    assert error["param"] == param
    assert error["code"] == code


@pytest.mark.parametrize(
    "payload",
    [
        body(messages=[{"role": "user", "content": CANARY}], temperature="hot"),
        body(model=CANARY * 20),  # the invalid value itself is the secret
        body(messages=[{"role": "user", "content": CANARY * 10}]),  # over max_input_chars
        body(model=CANARY),  # not on the allowlist
    ],
)
async def test_errors_never_echo_request_content(
    http: httpx.AsyncClient, payload: dict[str, Any]
) -> None:
    resp = await http.post("/test/chat", json=payload)
    assert resp.status_code == 400
    assert CANARY not in resp.text


@pytest.mark.parametrize(
    ("payload", "param", "code"),
    [
        (body(model="gpt-5-expensive"), "model", "model_not_found"),
        (body(messages=[{"role": "user", "content": "x"}] * 4), "messages", "invalid_value"),
        (
            body(messages=[{"role": "user", "content": "x" * 51}]),
            "messages",
            "context_length_exceeded",
        ),
        (body(max_completion_tokens=101), "max_completion_tokens", "invalid_value"),
        (body(max_tokens=101), "max_completion_tokens", "invalid_value"),
    ],
)
async def test_configured_limits(
    http: httpx.AsyncClient, payload: dict[str, Any], param: str, code: str
) -> None:
    error = assert_openai_error(await http.post("/test/chat", json=payload), 400)
    assert error["param"] == param
    assert error["code"] == code


async def test_limits_at_the_boundary_pass(http: httpx.AsyncClient) -> None:
    payload = body(
        messages=[{"role": "user", "content": "x" * 25}] * 2,  # exactly 50 chars, 2 messages
        max_completion_tokens=100,
    )
    resp = await http.post("/test/chat", json=payload)
    assert resp.status_code == 200


async def test_not_found_and_method_not_allowed_use_openai_shape(http: httpx.AsyncClient) -> None:
    assert assert_openai_error(await http.get("/nope"), 404)["type"] == "not_found_error"
    assert_openai_error(await http.get("/test/chat"), 405)


async def test_unhandled_exception_returns_generic_500(http: httpx.AsyncClient) -> None:
    resp = await http.get("/test/boom")
    error = assert_openai_error(resp, 500)
    assert error["message"] == "Internal server error."
    assert CANARY not in resp.text


async def test_error_responses_keep_request_id(http: httpx.AsyncClient) -> None:
    resp = await http.post("/test/chat", json=body(temperature=9))
    assert resp.status_code == 400
    assert REQUEST_ID_HEADER in resp.headers
