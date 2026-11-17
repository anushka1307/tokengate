"""Daily token budgets per tenant: reserve before the model call, settle after (D41-D44).

1. ``reserve``: set aside the estimated input plus the output limit (shrunk to fit what's left)
   on the tenant's counter for today (UTC). Refused when nothing fits: 429 insufficient_quota.
2. ``settle``: once the call ends, replace the reservation with the real usage. A provider
   failure before any output refunds everything. When real usage is unknown (a cancelled or
   broken stream), the reservation is kept: the gateway charges the safe amount.

Failure policy: budgets FAIL CLOSED. If Redis can't be reached, the request is refused with 503
before any provider is called, and Redis is skipped for ``cooldown_s`` so the refusals are fast.
"""

import asyncio
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.telemetry.metrics import Metrics

log = structlog.get_logger("tokengate.budget")

_SCRIPT = (Path(__file__).parent / "reserve.lua").read_text(encoding="utf-8")

# A day's counter is kept a little past the day so late settlements still find it.
COUNTER_TTL_S = 48 * 3600

_REDIS_ERRORS = (RedisError, OSError, TimeoutError)


class BudgetExhausted(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"daily budget used up; resets in {retry_after_s} s")
        self.retry_after_s = retry_after_s


class BudgetUnavailable(Exception):
    """Redis couldn't be asked, so the budget can't be checked."""


@dataclass(slots=True)
class Reservation:
    """Tokens set aside for one request. Settle exactly once; later calls do nothing."""

    ledger: "BudgetLedger"
    counter_key: str
    input_tokens: int
    output_tokens: int  # the (possibly shrunk) output limit to send to the provider
    _settled: bool = field(default=False, repr=False)

    @property
    def reserved(self) -> int:
        return self.input_tokens + self.output_tokens

    async def settle(self, actual_tokens: int | None) -> None:
        """Charge ``actual_tokens`` instead of the reservation. 0 refunds it all; None keeps it."""
        if self._settled:
            return
        self._settled = True
        if actual_tokens is None:
            return  # usage unknown: keep the full reservation
        await self.ledger.adjust(self.counter_key, actual_tokens - self.reserved)


class BudgetLedger:
    def __init__(
        self,
        client: Redis,
        *,
        key_prefix: str,
        timeout_s: float,
        cooldown_s: float,
        metrics: Metrics,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._script = client.register_script(_SCRIPT)
        self._prefix = key_prefix
        self._timeout_s = timeout_s
        self._cooldown_s = cooldown_s
        self._metrics = metrics
        self._now = now
        self._clock = clock
        self._skip_until = 0.0

    def counter_key(self, tenant_id: uuid.UUID, day: str) -> str:
        # The day comes from the gateway's clock (NTP-synced on Cloud Run), so the key can be
        # declared up front, as Redis Cluster requires. A few ms of skew at midnight is harmless.
        return f"{self._prefix}budget:{{{tenant_id}}}:{day}"

    async def reserve(
        self, tenant_id: uuid.UUID, daily_budget: int, input_tokens: int, max_output_tokens: int
    ) -> Reservation:
        if self._clock() < self._skip_until:
            self._metrics.budget_decisions.labels(result="skipped_cooldown").inc()
            raise BudgetUnavailable("budget store cooling down after a failure")
        now = self._now()
        key = self.counter_key(tenant_id, now.strftime("%Y-%m-%d"))
        try:
            async with asyncio.timeout(self._timeout_s):
                result = await self._script(
                    keys=[key],
                    args=[daily_budget, input_tokens, max_output_tokens, COUNTER_TTL_S],
                )
        except _REDIS_ERRORS as exc:
            self._skip_until = self._clock() + self._cooldown_s
            self._metrics.budget_decisions.labels(result="redis_error").inc()
            log.warning(
                "budget_unavailable", error=type(exc).__name__, refuse_for_s=self._cooldown_s
            )
            raise BudgetUnavailable(type(exc).__name__) from exc

        if int(result[0]) == 0:
            self._metrics.budget_decisions.labels(result="exhausted").inc()
            raise BudgetExhausted(seconds_until_utc_midnight(now))
        self._metrics.budget_decisions.labels(result="allowed").inc()
        return Reservation(self, key, input_tokens, int(result[1]))

    async def adjust(self, counter_key: str, delta: int) -> None:
        """Apply a settlement. Failures are logged and counted, never raised: the request is
        already answered, and a failed refund only overcharges the tenant (the safe side)."""
        if delta == 0:
            return  # nothing to change: saves a Redis command
        try:
            async with asyncio.timeout(self._timeout_s):
                await self._client.incrby(counter_key, delta)
        except _REDIS_ERRORS as exc:
            self._metrics.budget_settle_errors.inc()
            log.warning("budget_settle_failed", error=type(exc).__name__, delta=delta)


def seconds_until_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((tomorrow - now).total_seconds()))
