"""POST /v1/chat/completions: the OpenAI-compatible front door.

Request path (Phase 1): validate (schemas) -> limits -> route alias to provider -> call provider
-> package the answer in OpenAI's shape. Auth, rate limits, cache and failover slot in later.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import structlog
from fastapi import APIRouter, Depends, Request, Response
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
from app.auth.dependency import Principal, authenticate
from app.providers.base import (
    Provider,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderUsage,
    StreamEvent,
)
from app.ratelimit.dependency import enforce_rate_limit
from app.ratelimit.limiter import RateLimitDecision
from app.state import get_state
from app.telemetry.metrics import Metrics
from app.telemetry.middleware import current_provider_wait_s, track_provider_wait

router = APIRouter(tags=["chat"])


@dataclass(slots=True)
class _CallRecorder:
    """Records one provider call's outcome, wait time and tokens in metrics, exactly once."""

    metrics: Metrics
    provider: str
    model: str  # the alias: bounded by the configured routes
    _done: bool = False

    def finish(self, outcome: str, usage: ProviderUsage | None = None) -> None:
        if self._done:
            return
        self._done = True
        m = self.metrics
        m.provider_requests.labels(self.provider, self.model, outcome).inc()
        m.provider_wait.labels(self.provider).observe(current_provider_wait_s())
        if usage is not None:
            m.tokens.labels(self.provider, self.model, "prompt").inc(usage.prompt_tokens)
            m.tokens.labels(self.provider, self.model, "completion").inc(usage.completion_tokens)


log = structlog.get_logger("tokengate.chat")

_PROVIDER_ERRORS: dict[str, tuple[int, str, str]] = {
    # kind -> (HTTP status, code, message). Says whose problem it is, never upstream details.
    "upstream_error": (502, "upstream_error", "The model provider returned an error."),
    "timeout": (504, "upstream_timeout", "The model provider timed out."),
    "unavailable": (503, "upstream_unavailable", "No model provider is available right now."),
}


@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    principal: Principal | None = Depends(authenticate),  # noqa: B008 - FastAPI dependency
    # Runs after authenticate (FastAPI reuses the same Principal); raises 429 when limited.
    rate_limit: RateLimitDecision | None = Depends(enforce_rate_limit),  # noqa: B008
) -> Response:
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
    if rate_limit is not None and rate_limit.remaining is not None:
        headers["X-RateLimit-Remaining"] = str(rate_limit.remaining)
    structlog.contextvars.bind_contextvars(model=body.model, provider=provider.name)
    recorder = _CallRecorder(state.metrics, provider.name, body.model)

    if body.stream:
        include_usage = body.stream_options is not None and body.stream_options.include_usage
        return await _start_stream(
            provider,
            provider_request,
            completion_id,
            created,
            body.model,
            include_usage,
            headers,
            recorder,
        )

    try:
        with track_provider_wait():
            result = await provider.complete(provider_request)
    except ProviderError as exc:
        recorder.finish(exc.kind)
        raise _provider_failure(exc) from exc
    recorder.finish("success", result.usage)

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
    recorder: _CallRecorder,
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
            recorder.finish(exc.kind)
            raise _provider_failure(exc) from exc
        if isinstance(exc, StopAsyncIteration):
            recorder.finish("upstream_error")
            raise APIError(
                502,
                "The model provider returned an empty stream.",
                code="upstream_error",
                error_type="api_error",
            ) from exc
        recorder.finish("cancelled")
        raise

    body = _sse_body(first, events, completion_id, created, model, include_usage, recorder)

    async def cleanup() -> None:
        # Runs even if the body never started (e.g. the client vanished before headers went
        # out), when closing the unstarted body would not reach its own cleanup code.
        await events.aclose()
        recorder.finish("cancelled")  # no-op if the stream already recorded its outcome

    return EventStreamResponse(body, headers=headers, on_close=cleanup)


async def _sse_body(
    first: StreamEvent,
    events: AsyncGenerator[StreamEvent, None],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
    recorder: _CallRecorder,
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

    # Unless the stream finishes or the provider fails, the client went away (cancelled).
    outcome = "cancelled"
    usage: ProviderUsage | None = None
    try:
        yield chunk(ChunkDelta(role="assistant", content=""))
        event: StreamEvent | None = first
        while event is not None:
            if event.usage is not None:
                usage = event.usage
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
                outcome = exc.kind
                failure = _provider_failure(exc)
                yield sse_event(
                    error_body(failure.message, error_type=failure.error_type, code=failure.code)
                )
                return
        outcome = "success"
        yield DONE
    finally:
        await events.aclose()
        recorder.finish(outcome, usage)


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
