"""The official openai SDK talking to the real gateway app (in-process, no network, no cost)."""

from collections.abc import AsyncIterator

import httpx2
import openai
import pytest
from asgi_lifespan import LifespanManager
from openai.types.chat import ChatCompletionMessageParam

from app.config import Settings
from app.main import create_app

QUESTION: list[ChatCompletionMessageParam] = [
    {"role": "user", "content": "What is a token bucket?"}
]


async def sdk_for(settings: Settings) -> AsyncIterator[openai.AsyncOpenAI]:
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        http_client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=manager.app))
        client = openai.AsyncOpenAI(
            api_key="test-key-not-real",  # the gateway has no auth until Phase 2
            base_url="http://testserver/v1",
            http_client=http_client,
            max_retries=0,
        )
        async with client:
            yield client


@pytest.fixture
async def sdk() -> AsyncIterator[openai.AsyncOpenAI]:
    async for client in sdk_for(Settings(env="test")):
        yield client


async def test_sdk_completion(sdk: openai.AsyncOpenAI) -> None:
    result = await sdk.chat.completions.create(model="mock-1", messages=QUESTION)
    assert result.model == "mock-1"
    assert result.choices[0].message.content
    assert result.choices[0].finish_reason == "stop"
    assert result.usage is not None and result.usage.completion_tokens > 0


async def test_sdk_streaming_matches_completion(sdk: openai.AsyncOpenAI) -> None:
    whole = await sdk.chat.completions.create(model="mock-1", messages=QUESTION)
    stream = await sdk.chat.completions.create(
        model="mock-1",
        messages=QUESTION,
        stream=True,
        stream_options={"include_usage": True},
    )
    parts: list[str] = []
    usage = None
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
        if chunk.usage is not None:
            usage = chunk.usage
    assert "".join(parts) == whole.choices[0].message.content
    assert usage is not None and whole.usage is not None
    assert usage.total_tokens == whole.usage.total_tokens


async def test_sdk_sees_gateway_headers(sdk: openai.AsyncOpenAI) -> None:
    raw = await sdk.chat.completions.with_raw_response.create(model="mock-1", messages=QUESTION)
    assert raw.headers["x-provider"] == "mock"
    assert raw.headers["x-cache"] == "miss"
    assert "x-request-id" in raw.headers


async def test_sdk_gets_typed_error_for_unsupported_feature(sdk: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as info:
        await sdk.chat.completions.create(
            model="mock-1",
            messages=QUESTION,
            response_format={"type": "json_object"},
        )
    assert info.value.code == "unsupported_parameter"


async def test_sdk_gets_typed_error_for_unknown_model(sdk: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as info:
        await sdk.chat.completions.create(model="gpt-5-expensive", messages=QUESTION)
    assert info.value.code == "model_not_found"


async def test_sdk_gets_server_error_when_provider_fails() -> None:
    async for client in sdk_for(Settings(env="test", mock_error_rate=1.0)):
        with pytest.raises(openai.InternalServerError) as info:
            await client.chat.completions.create(model="mock-1", messages=QUESTION)
        assert info.value.status_code == 502
