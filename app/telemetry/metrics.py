"""Prometheus metrics and the protected ``/metrics`` endpoint (docs/DECISIONS.md, D20-D21).

Label rule: every label has a small, fixed set of values. Never a prompt, user ID, tenant name or
raw URL. Unknown paths are reported as route="other", so a scanner hitting random URLs can't
create unbounded time series (and private data never reaches dashboards).
"""

import hmac

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.errors import APIError

KNOWN_ROUTES = frozenset({"/v1/chat/completions", "/healthz", "/readyz", "/metrics"})

# Gateway overhead should be single-digit milliseconds, so the buckets are fine-grained there.
_OVERHEAD_BUCKETS = (0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


def route_label(path: str) -> str:
    return path if path in KNOWN_ROUTES else "other"


class Metrics:
    """One registry per app instance (not the global default), so tests stay isolated."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry

        self.requests = Counter(
            "tokengate_requests_total",
            "HTTP requests by route, method and status class (2xx/4xx/5xx).",
            ["route", "method", "status_class"],
            registry=r,
        )
        self.request_duration = Histogram(
            "tokengate_request_duration_seconds",
            "Wall-clock request duration, including the full stream for SSE responses.",
            ["route"],
            buckets=_DURATION_BUCKETS,
            registry=r,
        )
        self.gateway_overhead = Histogram(
            "tokengate_gateway_overhead_seconds",
            "Time the gateway itself added: duration minus time waiting on providers.",
            ["route"],
            buckets=_OVERHEAD_BUCKETS,
            registry=r,
        )
        self.inflight = Gauge(
            "tokengate_inflight_requests", "HTTP requests currently being handled.", registry=r
        )
        self.provider_requests = Counter(
            "tokengate_provider_requests_total",
            "Calls to model providers by outcome "
            "(success, upstream_error, timeout, unavailable, cancelled).",
            ["provider", "model", "outcome"],
            registry=r,
        )
        self.provider_wait = Histogram(
            "tokengate_provider_wait_seconds",
            "Time spent waiting on a provider within one request.",
            ["provider"],
            buckets=_DURATION_BUCKETS,
            registry=r,
        )
        self.tokens = Counter(
            "tokengate_tokens_total",
            "Tokens reported by providers (MockProvider reports word counts).",
            ["provider", "model", "kind"],
            registry=r,
        )
        self.auth_failures = Counter(
            "tokengate_auth_failures_total",
            "Rejected API keys by reason (missing, malformed, unknown, wrong_secret, revoked, "
            "tenant_disabled). Clients always see the same 401.",
            ["reason"],
            registry=r,
        )
        self.ratelimit_decisions = Counter(
            "tokengate_ratelimit_decisions_total",
            "Rate-limit checks by result (allowed, limited_key, limited_tenant, redis_error, "
            "skipped_cooldown). Redis problems fail open: the request is allowed.",
            ["result"],
            registry=r,
        )
        build_info = Gauge(
            "tokengate_build_info", "Always 1; labels identify the build.", ["version"], registry=r
        )
        build_info.labels(version=__version__).set(1)


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    # Imported here to avoid a cycle (app.state imports this module's Metrics).
    from app.state import get_state

    state = get_state(request.app)
    token = state.settings.metrics_token
    if token is None:
        if state.settings.env == "prod":
            # No token configured in production: behave as if the page doesn't exist.
            raise StarletteHTTPException(status_code=404, detail="Not Found")
    else:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {token.get_secret_value()}"
        # Constant-time comparison so the token can't be guessed byte by byte from timings.
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise APIError(401, "Missing or invalid metrics token.", code="invalid_api_key")
    return Response(generate_latest(state.metrics.registry), media_type=CONTENT_TYPE_LATEST)
