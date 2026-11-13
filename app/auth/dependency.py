"""Authenticate ``Authorization: Bearer tg_...`` on the chat endpoint (docs/DECISIONS.md, D35).

Every failure returns the same 401, so a caller can't learn whether a key exists, is revoked,
or belongs to a disabled tenant. The real reason goes to logs and the
``tokengate_auth_failures_total{reason}`` metric only.

Check order matters: the secret is verified before revocation/disabled status is looked at, so
only someone holding the real key can tell that key's status apart from "invalid".
"""

import uuid
from dataclasses import dataclass
from typing import Literal

import asyncpg
import structlog
from fastapi import Request

from app.api.errors import APIError
from app.auth.keys import matches, parse_key
from app.state import get_state

log = structlog.get_logger("tokengate.auth")

FailureReason = Literal[
    "missing", "malformed", "unknown", "wrong_secret", "revoked", "tenant_disabled"
]


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. Later steps use the limits for rate limiting and budgets."""

    tenant_id: uuid.UUID
    tenant_name: str
    key_id: uuid.UUID
    key_prefix: str
    requests_per_minute: int  # the tighter of the key's and the tenant's limit
    tenant_requests_per_minute: int
    daily_token_budget: int


def _reject(request: Request, reason: FailureReason) -> APIError:
    get_state(request.app).metrics.auth_failures.labels(reason=reason).inc()
    log.info("auth_failed", reason=reason)
    return APIError(
        401,
        "Invalid or missing API key. Send it as 'Authorization: Bearer <key>'.",
        code="invalid_api_key",
        error_type="authentication_error",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate(request: Request) -> Principal | None:
    """FastAPI dependency. Returns None only when auth is disabled (local dev and tests)."""
    state = get_state(request.app)
    if state.settings.auth == "disabled":
        return None
    if state.key_store is None or state.settings.api_key_pepper is None:
        raise APIError(503, "The gateway is not ready.", code="not_ready", error_type="api_error")

    header = request.headers.get("authorization")
    if not header:
        raise _reject(request, "missing")
    scheme, _, token = header.partition(" ")
    parsed = parse_key(token.strip()) if scheme.lower() == "bearer" else None
    if parsed is None:
        raise _reject(request, "malformed")
    prefix, secret = parsed

    try:
        record = await state.key_store.lookup(prefix)
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        # Fail closed: if we can't check the key, nobody gets in.
        log.warning("auth_lookup_failed", error=type(exc).__name__)
        raise APIError(
            503,
            "Authentication is temporarily unavailable.",
            code="auth_unavailable",
            error_type="api_error",
        ) from exc

    if record is None:
        raise _reject(request, "unknown")
    if not matches(
        secret, state.settings.api_key_pepper.get_secret_value().encode(), record.key_hash
    ):
        raise _reject(request, "wrong_secret")
    if record.key_revoked:
        raise _reject(request, "revoked")
    if record.tenant_disabled:
        raise _reject(request, "tenant_disabled")

    # The prefix is public; the secret never reaches a log line.
    structlog.contextvars.bind_contextvars(tenant=record.tenant_name, key_prefix=record.prefix)
    rpm = record.tenant_requests_per_minute
    if record.key_requests_per_minute is not None:
        rpm = min(rpm, record.key_requests_per_minute)
    return Principal(
        tenant_id=record.tenant_id,
        tenant_name=record.tenant_name,
        key_id=record.key_id,
        key_prefix=record.prefix,
        requests_per_minute=rpm,
        tenant_requests_per_minute=record.tenant_requests_per_minute,
        daily_token_budget=record.daily_token_budget,
    )
