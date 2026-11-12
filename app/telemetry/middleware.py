"""Per-request context: request ID, gateway-overhead timing, and the access log.

Written as pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware`` so it adds no extra
task per request and doesn't interfere with streaming responses (see docs/DECISIONS.md, D6).
"""

import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.telemetry.metrics import Metrics, route_label

REQUEST_ID_HEADER = "X-Request-Id"
OVERHEAD_HEADER = "X-Gateway-Overhead-Ms"

# Client-supplied IDs are untrusted: accept only a short, log-safe charset (see D8).
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_REQUEST_ID_HEADER_RAW = REQUEST_ID_HEADER.lower().encode("latin-1")

_access_log = structlog.get_logger("tokengate.access")


@dataclass(slots=True)
class RequestTiming:
    """Wall-clock time for one request, split into provider wait vs. everything else."""

    start: float
    provider_wait_s: float = 0.0

    def total_ms(self, now: float | None = None) -> float:
        return ((now if now is not None else time.perf_counter()) - self.start) * 1000

    def overhead_ms(self, now: float | None = None) -> float:
        """Time the gateway itself added: total minus time spent waiting on providers (D7)."""
        return max(0.0, self.total_ms(now) - self.provider_wait_s * 1000)


_current_timing: ContextVar[RequestTiming | None] = ContextVar("tokengate_timing", default=None)


@contextmanager
def track_provider_wait() -> Iterator[None]:
    """Wrap every await on an upstream provider so its time is excluded from gateway overhead.

    Usage: ``with track_provider_wait(): resp = await provider.complete(req)``
    """
    timing = _current_timing.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing.provider_wait_s += time.perf_counter() - started


def current_provider_wait_s() -> float:
    """Provider wait accumulated so far in the current request (0 outside a request)."""
    timing = _current_timing.get()
    return timing.provider_wait_s if timing is not None else 0.0


# HTTP allows arbitrary method names; keep the metrics label to a fixed set.
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == _REQUEST_ID_HEADER_RAW:
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.fullmatch(candidate) else None
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics | None = None) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        timing = RequestTiming(start=time.perf_counter())
        status_code = 500  # if the app raises before responding, the server returns a 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                # For streamed responses this is overhead up to the first byte only (D7);
                # the full figure is in the access log below.
                headers[OVERHEAD_HEADER] = f"{timing.overhead_ms():.2f}"
            await send(message)

        timing_token = _current_timing.set(timing)
        # Start from a clean logging context: handlers may bind more fields (model, provider),
        # and those must not leak into the next request served by the same task.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        if self.metrics is not None:
            self.metrics.inflight.inc()
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            now = time.perf_counter()
            _access_log.info(
                "request",
                method=scope["method"],
                path=scope["path"],  # no query string: it may carry secrets
                status=status_code,
                duration_ms=round(timing.total_ms(now), 2),
                overhead_ms=round(timing.overhead_ms(now), 2),
            )
            if self.metrics is not None:
                self._record(scope, status_code, timing, now)
            structlog.contextvars.clear_contextvars()
            _current_timing.reset(timing_token)

    def _record(self, scope: Scope, status_code: int, timing: RequestTiming, now: float) -> None:
        assert self.metrics is not None  # noqa: S101 - only called when metrics are set
        route = route_label(scope["path"])
        method = scope["method"] if scope["method"] in _KNOWN_METHODS else "other"
        self.metrics.inflight.dec()
        self.metrics.requests.labels(
            route=route, method=method, status_class=f"{status_code // 100}xx"
        ).inc()
        self.metrics.request_duration.labels(route=route).observe(timing.total_ms(now) / 1000)
        self.metrics.gateway_overhead.labels(route=route).observe(timing.overhead_ms(now) / 1000)
