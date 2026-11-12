"""Schema rules, plus contract tests against the official openai SDK (no network: MockTransport)."""

import json
from typing import Any

import httpx2
import openai
import pydantic
import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat import ChatCompletionChunk as SDKChunk

from app.api.schemas import (
    AssistantMessage,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    ChunkDelta,
    Usage,
)

USER_MSG = {"role": "user", "content": "hi"}


def parse(**fields: Any) -> ChatCompletionRequest:
    body: dict[str, Any] = {"model": "mock-1", "messages": [USER_MSG], **fields}
    return ChatCompletionRequest.model_validate(body)


def error_types(exc: pydantic.ValidationError) -> set[str]:
    return {e["type"] for e in exc.errors()}


# ---------------------------------------------------------------- accepted


def test_minimal_request_and_defaults() -> None:
    req = parse()
    assert req.stream is False
    assert req.temperature is None
    assert req.requested_max_tokens is None
    assert req.messages[0].text == "hi"


def test_all_supported_parameters() -> None:
    req = parse(
        temperature=0,
        top_p=0.9,
        max_completion_tokens=100,
        stop=["\n", "END"],
        seed=7,
        presence_penalty=-1.5,
        frequency_penalty=2,
        user="tenant-user-1",
        n=1,
        logprobs=False,
        stream=True,
        stream_options={"include_usage": True},
    )
    assert req.stream_options is not None and req.stream_options.include_usage
    assert req.requested_max_tokens == 100


def test_all_supported_roles_and_text_parts() -> None:
    req = ChatCompletionRequest.model_validate(
        {
            "model": "mock-1",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "developer", "content": "no markdown"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
                },
                {"role": "assistant", "content": "ok", "name": "bot"},
            ],
        }
    )
    assert req.messages[2].text == "ab"
    assert req.input_chars == len("be brief") + len("no markdown") + 2 + 2 + len("bot")


def test_unknown_fields_are_ignored_not_kept() -> None:
    req = parse(store=True, metadata={"k": "v"}, some_future_field=123)
    dumped = req.model_dump()
    assert "store" not in dumped
    assert "some_future_field" not in dumped


def test_max_tokens_alias() -> None:
    assert parse(max_tokens=50).requested_max_tokens == 50
    assert parse(max_tokens=50, max_completion_tokens=50).requested_max_tokens == 50


# ---------------------------------------------------------------- rejected


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": [{"type": "function", "function": {"name": "f"}}]},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"logprobs": True},
        {"n": 2},
        {"audio": {"voice": "x"}},
        {"web_search_options": {"search_context_size": "low"}},
    ],
)
def test_unsupported_features_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        parse(**fields)
    assert error_types(info.value) == {"unsupported_parameter"}


def test_falsy_unsupported_fields_are_harmless() -> None:
    parse(tools=[], logprobs=False, n=1, web_search_options=None)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "result", "tool_call_id": "x"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]},
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]},
    ],
)
def test_unsupported_message_features_are_rejected(message: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        ChatCompletionRequest.model_validate({"model": "mock-1", "messages": [message]})
    assert error_types(info.value) == {"unsupported_parameter"}


@pytest.mark.parametrize(
    "fields",
    [
        {"temperature": 2.1},
        {"top_p": -0.1},
        {"max_completion_tokens": 0},
        {"presence_penalty": 3},
        {"stop": ["a", "b", "c", "d", "e"]},
        {"stream_options": {"include_usage": True}},  # without stream=true
        {"max_tokens": 10, "max_completion_tokens": 20},
        {"messages": []},
        {"model": ""},
        {"model": "m" * 129},
        {"user": "u" * 257},
    ],
)
def test_invalid_values_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        parse(**fields)


def test_message_without_content_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        ChatCompletionRequest.model_validate({"model": "mock-1", "messages": [{"role": "user"}]})


# ---------------------------------------------------------------- SDK contract


def _sample_response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1_700_000_000,
        model="mock-1",
        choices=[Choice(index=0, message=AssistantMessage(content="hello"), finish_reason="stop")],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )


def _sdk_client(captured: list[dict[str, Any]]) -> openai.OpenAI:
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
        return httpx2.Response(200, json=_sample_response().model_dump(mode="json"))

    return openai.OpenAI(
        api_key="test-key-not-real",
        base_url="http://gateway.test/v1",
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )


def test_requests_built_by_the_openai_sdk_parse() -> None:
    captured: list[dict[str, Any]] = []
    client = _sdk_client(captured)

    result = client.chat.completions.create(
        model="mock-1",
        messages=[
            {"role": "developer", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ],
        temperature=0,
        max_completion_tokens=64,
        stop=["END"],
        seed=1,
        user="u1",
    )

    req = ChatCompletionRequest.model_validate(captured[0])
    assert req.model == "mock-1"
    assert req.messages[1].text == "hello"
    assert req.requested_max_tokens == 64
    # ...and the SDK accepts our response model.
    assert result.choices[0].message.content == "hello"
    assert result.usage is not None and result.usage.total_tokens == 4


def test_response_models_validate_as_sdk_types() -> None:
    sdk = ChatCompletion.model_validate(_sample_response().model_dump(mode="json"))
    assert sdk.object == "chat.completion"
    assert sdk.choices[0].finish_reason == "stop"

    first = ChatCompletionChunk(
        id="c1",
        created=1,
        model="mock-1",
        choices=[ChunkChoice(index=0, delta=ChunkDelta(role="assistant", content="he"))],
    )
    last = ChatCompletionChunk(
        id="c1",
        created=1,
        model="mock-1",
        choices=[],
        usage=Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    sdk_first = SDKChunk.model_validate(first.model_dump(mode="json"))
    sdk_last = SDKChunk.model_validate(last.model_dump(mode="json"))
    assert sdk_first.choices[0].delta.content == "he"
    assert sdk_last.usage is not None and sdk_last.usage.total_tokens == 5
