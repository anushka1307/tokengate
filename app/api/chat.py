"""POST /v1/chat/completions: the OpenAI-compatible front door.

Request path: validate (schemas) -> authenticate -> rate limit -> request limits -> route alias to
provider -> reserve daily budget -> call provider -> settle budget -> package the answer in
OpenAI's shape. Cache and failover slot in later.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, replace

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
from app.budget.ledger import (
    BudgetExhausted,
    BudgetUnavailable,
    Reservation,
    estimate_input_tokens,
)
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
    reservation = await _reserve_budget(request, principal, body.input_chars, provider_request)
    if reservation is not None:
        # The output limit may have been shrunk to fit what's left of today's budget.
        provider_request = replace(provider_request, max_output_tokens=reservation.output_tokens)
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
            reservation,
        )

    try:
        with track_provider_wait():
            result = await provider.complete(provider_request)
    except ProviderError as exc:
        recorder.finish(exc.kind)
        await _settle(reservation, 0)  # no answer: refund the whole reservation
        raise _provider_failure(exc) from exc
    recorder.finish("success", result.usage)
    await _settle(reservation, result.usage.total_tokens)

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
    reservation: Reservation | None,
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
            await _settle(reservation, 0)  # nothing was generated: refund
            raise _provider_failure(exc) from exc
        if isinstance(exc, StopAsyncIteration):
            recorder.finish("upstream_error")
            await _settle(reservation, 0)
            raise APIError(
                502,
                "The model provider returned an empty stream.",
                code="upstream_error",
                error_type="api_error",
            ) from exc
        recorder.finish("cancelled")
        raise

    body = _sse_body(
        first, events, completion_id, created, model, include_usage, recorder, reservation
    )

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
    reservation: Reservation | None,
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
        # Settle before [DONE] (not in `finally`), so it never runs while the stream is being
        # torn down. Without usage from the provider, the reservation is kept.
        await _settle(reservation, usage.total_tokens if usage is not None else None)
        outcome = "success"
        yield DONE
    finally:
        await events.aclose()
        recorder.finish(outcome, usage)


async def _reserve_budget(
    request: Request,
    principal: Principal | None,
    input_chars: int,
    provider_request: ProviderRequest,
) -> Reservation | None:
    """Set aside today's tokens for this call. None when budgets are off (local dev only)."""
    ledger = get_state(request.app).budget_ledger
    if principal is None or ledger is None:
        return None
    try:
        return await ledger.reserve(
            principal.tenant_id,
            principal.daily_token_budget,
            estimate_input_tokens(input_chars),
            provider_request.max_output_tokens,
        )
    except BudgetExhausted as exc:
        log.info("budget_exhausted", retry_after_s=exc.retry_after_s)
        raise APIError(
            429,
            "This organization's daily token budget is used up. It resets at 00:00 UTC.",
            code="insufficient_quota",
            error_type="insufficient_quota",
            headers={"Retry-After": str(exc.retry_after_s)},
        ) from exc
    except BudgetUnavailable as exc:
        # Fail closed: without a budget check, no tokens are spent.
        raise APIError(
            503,
            "Budget checks are temporarily unavailable. Please retry shortly.",
            code="budget_unavailable",
            error_type="api_error",
        ) from exc


async def _settle(reservation: Reservation | None, actual_tokens: int | None) -> None:
    if reservation is not None:
        await reservation.settle(actual_tokens)


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
