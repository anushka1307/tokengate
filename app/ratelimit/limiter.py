"""Per-key and per-tenant token buckets in Redis (docs/DECISIONS.md, D37-D40).

Each API key has its own bucket, and all of a tenant's keys share a tenant bucket. A bucket
holds one minute of requests (``capacity = rpm``) and refills at ``rpm / 60`` tokens a second,
so a client can burst a full minute's allowance and then continues at the steady rate.

Both buckets are checked and charged by one Lua script (token_bucket.lua), so the check is
atomic across every gateway instance.

Failure policy: rate limiting FAILS OPEN. If Redis is slow, down or returns an error, the request
is allowed, the problem is logged and counted, and Redis is skipped for ``cooldown_s`` so an
outage doesn't add a timeout to every request.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import redis.asyncio as redis
import structlog
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError

from app.config import Settings
from app.telemetry.metrics import Metrics

log = structlog.get_logger("tokengate.ratelimit")

_SCRIPT = (Path(__file__).parent / "token_bucket.lua").read_text(encoding="utf-8")

LimitedBy = Literal["key", "tenant"]


def create_redis(settings: Settings) -> redis.Redis:
    """A Redis client with short timeouts and NO retries: a failure should fail open at once."""
    if settings.redis_url is None:  # pragma: no cover - callers check first
        raise ValueError("redis_url is not set")
    return redis.Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_connect_timeout_s,
        socket_timeout=settings.redis_timeout_s,
        retry=Retry(NoBackoff(), 0),
        retry_on_timeout=False,
        client_name="tokengate",
    )


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    # Whole requests left right now (the smaller of the two buckets). None when Redis wasn't
    # consulted, so the gateway doesn't claim a number it doesn't know.
    remaining: int | None
    retry_after_s: int  # 0 when allowed; otherwise whole seconds, at least 1
    limited_by: LimitedBy | None = None


_FAIL_OPEN = RateLimitDecision(allowed=True, remaining=None, retry_after_s=0)


@dataclass(frozen=True, slots=True)
class Limits:
    """The two buckets to charge. Rates are requests per minute."""

    tenant_id: uuid.UUID
    key_id: uuid.UUID
    key_rpm: int
    tenant_rpm: int


class RateLimiter:
    def __init__(
        self,
        client: redis.Redis,
        *,
        key_prefix: str,
        cooldown_s: float,
        timeout_s: float,
        metrics: Metrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._script = client.register_script(_SCRIPT)
        self._prefix = key_prefix
        self._cooldown_s = cooldown_s
        self._timeout_s = timeout_s
        self._metrics = metrics
        self._clock = clock
        self._skip_until = 0.0

    def bucket_keys(self, limits: Limits) -> tuple[str, str]:
        # {tenant_id} is a Redis Cluster hash tag: both keys land on the same shard, which a
        # multi-key script requires. Harmless on a single Redis.
        tag = f"{self._prefix}rl:{{{limits.tenant_id}}}"
        return f"{tag}:key:{limits.key_id}", f"{tag}:tenant"

    async def check(self, limits: Limits) -> RateLimitDecision:
        if self._clock() < self._skip_until:
            self._metrics.ratelimit_decisions.labels(result="skipped_cooldown").inc()
            return _FAIL_OPEN
        try:
            # The socket timeouts should fire first; this is a backstop so no Redis problem can
            # hold a request longer than the configured budget.
            async with asyncio.timeout(self._timeout_s):
                result = await self._script(
                    keys=list(self.bucket_keys(limits)),
                    args=[
                        limits.key_rpm / 60,
                        limits.key_rpm,
                        limits.tenant_rpm / 60,
                        limits.tenant_rpm,
                    ],
                )
        except (RedisError, OSError, TimeoutError) as exc:
            self._skip_until = self._clock() + self._cooldown_s
            self._metrics.ratelimit_decisions.labels(result="redis_error").inc()
            # Logged once per failure, then quiet for the cooldown: no log flood during outages.
            log.warning(
                "ratelimit_unavailable",
                error=type(exc).__name__,
                fail_open_for_s=self._cooldown_s,
            )
            return _FAIL_OPEN

        allowed, remaining, retry_after_ms, limited_by = (int(v) for v in result)
        if allowed:
            self._metrics.ratelimit_decisions.labels(result="allowed").inc()
            return RateLimitDecision(allowed=True, remaining=remaining, retry_after_s=0)
        which: LimitedBy = "tenant" if limited_by == 2 else "key"
        self._metrics.ratelimit_decisions.labels(result=f"limited_{which}").inc()
        return RateLimitDecision(
            allowed=False,
            remaining=0,
            retry_after_s=max(1, -(-retry_after_ms // 1000)),  # round up, never 0
            limited_by=which,
        )
