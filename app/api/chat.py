"""POST /v1/chat/completions: the OpenAI-compatible front door.

Request path (Phase 1): validate (schemas) -> limits -> route alias to provider -> call provider
-> package the answer in OpenAI's shape. Auth, rate limits, cache and failover slot in later.
"""

import time
import uuid
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.api.errors import APIError, error_body
from app.api.limits import enforce_request_limits
from app.api.schemas import (
    AssistantMessage,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    ChunkDelta,
    FinishReason,
    Usage,
)
from app.api.sse import DONE, EventStreamResponse, sse_event
from app.providers.base import (
    Provider,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderUsage,
    StreamEvent,
)
from app.state import get_state
from app.telemetry.middleware import track_provider_wait

router = APIRouter(tags=["chat"])
log = structlog.get_logger("tokengate.chat")

_PROVIDER_ERRORS: dict[str, tuple[int, str, str]] = {
    # kind -> (HTTP status, code, message). Says whose problem it is, never upstream details.
    "upstream_error": (502, "upstream_error", "The model provider returned an error."),
    "timeout": (504, "upstream_timeout", "The model provider timed out."),
    "unavailable": (503, "upstream_unavailable", "No model provider is available right now."),
}


@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(body: ChatCompletionRequest, request: Request) -> Response:
    state = get_state(request.app)
    if state.model_router is None:
        raise APIError(503, "The gateway is not ready.", code="not_ready", error_type="api_error")

    enforce_request_limits(body, state.settings)
    provider, route = state.model_router.resolve(body.model)
    provider_request = to_provider_request(
        body, route.target_model, state.settings.max_completion_tokens
    )
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    headers = {"X-Provider": provider.name, "X-Cache": "miss"}
    structlog.contextvars.bind_contextvars(model=body.model, provider=provider.name)

    if body.stream:
        include_usage = body.stream_options is not None and body.stream_options.include_usage
        return await _start_stream(
            provider, provider_request, completion_id, created, body.model, include_usage, headers
        )

    try:
        with track_provider_wait():
            result = await provider.complete(provider_request)
    except ProviderError as exc:
        raise _provider_failure(exc) from exc

    response = ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=body.model,
        choices=[
            Choice(
                index=0,
                message=AssistantMessage(content=result.text),
                finish_reason=result.finish_reason,
            )
        ],
        usage=_usage(result.usage),
    )
    return JSONResponse(response.model_dump(mode="json"), headers=headers)


def to_provider_request(
    body: ChatCompletionRequest, target_model: str, max_tokens_cap: int
) -> ProviderRequest:
    requested = body.requested_max_tokens
    if isinstance(body.stop, str):
        stop: tuple[str, ...] = (body.stop,)
    else:
        stop = tuple(body.stop or ())
    return ProviderRequest(
        target_model=target_model,
        messages=tuple(ProviderMessage(role=m.role, content=m.text) for m in body.messages),
        # Always bounded: if the client sets no limit, the configured cap applies.
        max_output_tokens=min(requested, max_tokens_cap) if requested else max_tokens_cap,
        temperature=body.temperature,
        top_p=body.top_p,
        stop=stop,
        seed=body.seed,
    )


async def _start_stream(
    provider: Provider,
    provider_request: ProviderRequest,
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
    headers: dict[str, str],
) -> Response:
    events = provider.stream(provider_request)
    # Wait for the first event BEFORE sending "200 OK", so a provider that fails immediately
    # still produces a real error status instead of a broken stream (D17).
    try:
        with track_provider_wait():
            first = await anext(events)
    except BaseException as exc:
        await events.aclose()
        if isinstance(exc, ProviderError):
            raise _provider_failure(exc) from exc
        if isinstance(exc, StopAsyncIteration):
            raise APIError(
                502,
                "The model provider returned an empty stream.",
                code="upstream_error",
                error_type="api_error",
            ) from exc
        raise

    body = _sse_body(first, events, completion_id, created, model, include_usage)
    return EventStreamResponse(body, headers=headers)


async def _sse_body(
    first: StreamEvent,
    events: AsyncGenerator[StreamEvent, None],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
) -> AsyncGenerator[bytes, None]:
    def chunk(
        delta: ChunkDelta | None = None,
        finish_reason: FinishReason | None = None,
        usage: Usage | None = None,
    ) -> bytes:
        choices: list[ChunkChoice] = []
        if delta is not None:
            choices = [ChunkChoice(index=0, delta=delta, finish_reason=finish_reason)]
        payload = ChatCompletionChunk(
            id=completion_id, created=created, model=model, choices=choices, usage=usage
        )
        return sse_event(payload.model_dump(mode="json"))

    try:
        yield chunk(ChunkDelta(role="assistant", content=""))
        event: StreamEvent | None = first
        while event is not None:
            if event.text_delta:
                yield chunk(ChunkDelta(content=event.text_delta))
            if event.finish_reason is not None:
                yield chunk(ChunkDelta(), finish_reason=event.finish_reason)
            if event.usage is not None and include_usage:
                # OpenAI's convention: a final chunk with empty choices and the usage totals.
                yield chunk(usage=_usage(event.usage))
            try:
                with track_provider_wait():
                    event = await anext(events)
            except StopAsyncIteration:
                event = None
            except ProviderError as exc:
                # "200 OK" is already sent; report the failure inside the stream and stop.
                failure = _provider_failure(exc)
                yield sse_event(
                    error_body(failure.message, error_type=failure.error_type, code=failure.code)
                )
                return
        yield DONE
    finally:
        await events.aclose()


def _usage(usage: ProviderUsage) -> Usage:
    return Usage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


def _provider_failure(exc: ProviderError) -> APIError:
    status, code, message = _PROVIDER_ERRORS[exc.kind]
    log.warning(
        "provider_error",
        provider=exc.provider,
        kind=exc.kind,
        retryable=exc.retryable,
        detail=str(exc),
    )
    return APIError(status, message, code=code, error_type="api_error")
