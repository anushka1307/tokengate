"""Enforce rate limits on the chat endpoint, after authentication (docs/DECISIONS.md, D37-D40)."""

import structlog
from fastapi import Depends, Request

from app.api.errors import APIError
from app.auth.dependency import Principal, authenticate
from app.ratelimit.limiter import Limits, RateLimitDecision
from app.state import get_state

log = structlog.get_logger("tokengate.ratelimit")


async def enforce_rate_limit(
    request: Request,
    principal: Principal | None = Depends(authenticate),  # noqa: B008 - FastAPI dependency
) -> RateLimitDecision | None:
    """FastAPI dependency. Raises a 429 when either bucket is empty.

    Returns None when there is nothing to limit: auth disabled (local dev and tests only) or no
    Redis configured (refused in prod).
    """
    limiter = get_state(request.app).rate_limiter
    if principal is None or limiter is None:
        return None
    decision = await limiter.check(
        Limits(
            tenant_id=principal.tenant_id,
            key_id=principal.key_id,
            key_rpm=principal.requests_per_minute,
            tenant_rpm=principal.tenant_requests_per_minute,
        )
    )
    if decision.allowed:
        return decision

    which = "API key" if decision.limited_by == "key" else "organization"
    log.info("rate_limited", limited_by=decision.limited_by, retry_after_s=decision.retry_after_s)
    raise APIError(
        429,
        f"Rate limit reached for this {which}. Retry after {decision.retry_after_s} s.",
        code="rate_limit_exceeded",
        headers={
            "Retry-After": str(decision.retry_after_s),
            "X-RateLimit-Remaining": "0",
        },
    )
