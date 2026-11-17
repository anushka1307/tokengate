"""Daily token budgets: reserve and settle against real Redis, fail-closed, and the HTTP path.

Needs TOKENGATE_TEST_REDIS_URL (and TOKENGATE_TEST_DATABASE_URL for the HTTP tests).
"""

import asyncio
import io
import json
import math
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import httpx2
import openai
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import SecretStr
from redis.asyncio import Redis

from app.admin.cli import run as admin
from app.auth.keys import KEY_PATTERN
from app.budget.estimate import PER_MESSAGE_OVERHEAD, estimate_input_tokens, estimate_text_tokens
from app.budget.ledger import (
    COUNTER_TTL_S,
    BudgetExhausted,
    BudgetLedger,
    BudgetUnavailable,
    seconds_until_utc_midnight,
)
from app.config import Settings
from app.db.migrate import load_migrations, migrate
from app.main import create_app
from app.ratelimit.limiter import create_redis
from app.state import get_state
from app.telemetry.metrics import Metrics

PEPPER = "budget-test-pepper-0123456789-abcdefghij"
URL = "/v1/chat/completions"
CHAT = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}
QUESTION: list[ChatCompletionMessageParam] = [{"role": "user", "content": "hi"}]
NOON = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def result_count(metrics: Metrics, result: str) -> float:
    value = metrics.registry.get_sample_value(
        "tokengate_budget_decisions_total", {"result": result}
    )
    return value or 0.0


@dataclass
class Bench:
    ledger: BudgetLedger
    client: Redis
    metrics: Metrics
    clock: FakeClock

    async def used(self, tenant: uuid.UUID, day: str = "2026-11-15") -> int:
        value = await self.client.get(self.ledger.counter_key(tenant, day))
        return int(value or 0)


def make_ledger(url: str, prefix: str, now: Callable[[], datetime] = lambda: NOON) -> Bench:
    settings = Settings(env="test", redis_url=SecretStr(url))
    client = create_redis(settings)
    metrics, clock = Metrics(), FakeClock()
    ledger = BudgetLedger(
        client,
        key_prefix=prefix,
        timeout_s=settings.redis_connect_timeout_s + settings.redis_timeout_s,
        cooldown_s=5,
        metrics=metrics,
        now=now,
        clock=clock,
    )
    return Bench(ledger, client, metrics, clock)


@pytest.fixture
async def bench(redis_url: str, redis_prefix: str) -> AsyncIterator[Bench]:
    b = make_ledger(redis_url, redis_prefix)
    yield b
    await b.client.aclose()


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("hi", 1),
        ("hello", 2),  # 5 letters: rounds up
        ("hello world", 4),  # (10 letters + 3 per run * 2 runs) / 4
        ("a a a", 3),  # every one-letter word costs a token
        ("!?!?", 4),  # every symbol costs a token
        ("漢字", 4),  # 6 UTF-8 bytes * 2/3
        ("😀", 3),  # 4 bytes * 2/3, rounded up
        ("é", 2),  # 2 bytes * 2/3, rounded up
        ("   \n\t ", 0),  # whitespace is free
    ],
)
def test_text_estimate_by_kind_of_text(text: str, expected: int) -> None:
    assert estimate_text_tokens(text) == expected


ATTACKS = {
    "one-letter words": " ".join(["a"] * 500),
    "symbols": "!?" * 500,
    "chinese": "漢字テスト" * 200,
    "emoji": "😀🎉" * 500,
    "digits": " ".join(str(i % 10) for i in range(500)),
}


@pytest.mark.parametrize("text", ATTACKS.values(), ids=ATTACKS.keys())
def test_cheap_attacks_are_never_estimated_below_the_old_rule_or_the_mock(text: str) -> None:
    estimate = estimate_text_tokens(text)
    assert estimate >= math.ceil(len(text) / 4)  # never laxer than the old 4-chars rule
    assert estimate >= len(text.split())  # MockProvider's count (words)
    non_space = sum(not c.isspace() for c in text)
    if "a a" in text or not text.isascii():
        # one token per visible character at least, which is what tokenizers charge here
        assert estimate >= non_space


def test_input_estimate_adds_per_message_overhead_and_is_never_zero() -> None:
    assert estimate_input_tokens([]) == 1
    assert estimate_input_tokens([""]) == PER_MESSAGE_OVERHEAD
    assert estimate_input_tokens(["hi", "hi"]) == 2 * (PER_MESSAGE_OVERHEAD + 1)


def test_estimate_is_fast_enough_to_run_on_the_event_loop() -> None:
    worst = "😀" * 200_000  # the default max_input_chars, in the slowest kind of text
    start = time.perf_counter()
    estimate_text_tokens(worst)
    # About 2 ms on an M-series laptop; the bound is loose so slow CI runners don't flake.
    assert time.perf_counter() - start < 0.05


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 15, 23, 59, 30, tzinfo=UTC), 30),
        (datetime(2026, 11, 15, 0, 0, 0, tzinfo=UTC), 86_400),
        (datetime(2026, 11, 15, 23, 59, 59, 900_000, tzinfo=UTC), 1),  # rounds up, never 0
        (datetime(2026, 12, 31, 12, 0, 0, tzinfo=UTC), 43_200),  # across a year boundary
    ],
)
def test_seconds_until_utc_midnight(now: datetime, expected: int) -> None:
    assert seconds_until_utc_midnight(now) == expected


# ---------------------------------------------------------------- the ledger, real Redis


async def test_reserve_sets_aside_input_plus_output(bench: Bench) -> None:
    tenant = uuid.uuid4()
    r = await bench.ledger.reserve(tenant, daily_budget=1000, input_tokens=10, max_output_tokens=50)
    assert (r.input_tokens, r.output_tokens, r.reserved) == (10, 50, 60)
    assert await bench.used(tenant) == 60
    assert result_count(bench.metrics, "allowed") == 1


async def test_output_limit_shrinks_to_fit_the_remaining_budget(bench: Bench) -> None:
    tenant = uuid.uuid4()
    r = await bench.ledger.reserve(tenant, 100, input_tokens=10, max_output_tokens=4096)
    assert r.output_tokens == 90  # not 4096: the model can't run past the budget
    assert await bench.used(tenant) == 100


async def test_refused_when_not_even_one_output_token_fits(bench: Bench) -> None:
    tenant = uuid.uuid4()
    await bench.ledger.reserve(tenant, 100, input_tokens=10, max_output_tokens=89)  # 99 used
    with pytest.raises(BudgetExhausted) as info:
        await bench.ledger.reserve(tenant, 100, input_tokens=1, max_output_tokens=10)
    assert info.value.retry_after_s == 12 * 3600  # NOON -> midnight UTC
    assert await bench.used(tenant) == 99  # a refusal reserves nothing
    assert result_count(bench.metrics, "exhausted") == 1


async def test_settle_charges_real_usage_and_only_once(bench: Bench) -> None:
    tenant = uuid.uuid4()
    r = await bench.ledger.reserve(tenant, 1000, input_tokens=10, max_output_tokens=90)
    await r.settle(37)
    assert await bench.used(tenant) == 37
    await r.settle(500)  # a second settle is ignored
    assert await bench.used(tenant) == 37


async def test_settle_with_zero_refunds_and_none_keeps(bench: Bench) -> None:
    tenant = uuid.uuid4()
    refunded = await bench.ledger.reserve(tenant, 1000, input_tokens=10, max_output_tokens=90)
    await refunded.settle(0)  # provider failed before answering
    assert await bench.used(tenant) == 0
    kept = await bench.ledger.reserve(tenant, 1000, input_tokens=10, max_output_tokens=90)
    await kept.settle(None)  # usage unknown (cancelled stream): keep the safe amount
    assert await bench.used(tenant) == 100


async def test_usage_above_the_estimate_is_still_charged(bench: Bench) -> None:
    tenant = uuid.uuid4()
    r = await bench.ledger.reserve(tenant, 1000, input_tokens=5, max_output_tokens=5)
    await r.settle(25)  # the input estimate was low: the real count wins
    assert await bench.used(tenant) == 25


async def test_concurrent_reservations_never_exceed_the_budget(bench: Bench) -> None:
    # 20 requests at once, each needing 10 tokens, against a budget of 50: exactly 5 fit.
    tenant = uuid.uuid4()

    async def attempt() -> bool:
        try:
            await bench.ledger.reserve(tenant, 50, input_tokens=9, max_output_tokens=1)
        except BudgetExhausted:
            return False
        return True

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(results) == 5
    assert await bench.used(tenant) == 50


async def test_tenants_and_days_have_separate_counters(redis_url: str, redis_prefix: str) -> None:
    day = {"now": NOON}
    b = make_ledger(redis_url, redis_prefix, now=lambda: day["now"])
    try:
        a, other = uuid.uuid4(), uuid.uuid4()
        await b.ledger.reserve(a, 10, input_tokens=1, max_output_tokens=9)
        with pytest.raises(BudgetExhausted):
            await b.ledger.reserve(a, 10, input_tokens=1, max_output_tokens=1)
        await b.ledger.reserve(other, 10, input_tokens=1, max_output_tokens=1)  # unaffected

        day["now"] = datetime(2026, 11, 16, 0, 0, 1, tzinfo=UTC)  # a new UTC day
        await b.ledger.reserve(a, 10, input_tokens=1, max_output_tokens=1)
        assert await b.used(a, "2026-11-15") == 10
        assert await b.used(a, "2026-11-16") == 2
    finally:
        await b.client.aclose()


async def test_counters_expire_after_two_days(bench: Bench) -> None:
    tenant = uuid.uuid4()
    await bench.ledger.reserve(tenant, 100, input_tokens=1, max_output_tokens=1)
    ttl = await bench.client.ttl(bench.ledger.counter_key(tenant, "2026-11-15"))
    assert COUNTER_TTL_S - 5 <= ttl <= COUNTER_TTL_S


# ---------------------------------------------------------------- failing closed


async def test_redis_down_refuses_then_skips_redis_during_cooldown() -> None:
    b = make_ledger("redis://127.0.0.1:1/0", "tg:")
    try:
        with pytest.raises(BudgetUnavailable):
            await b.ledger.reserve(uuid.uuid4(), 100, input_tokens=1, max_output_tokens=1)
        assert result_count(b.metrics, "redis_error") == 1

        b.clock.now += 4.9  # still cooling down: refused without trying Redis
        with pytest.raises(BudgetUnavailable):
            await b.ledger.reserve(uuid.uuid4(), 100, input_tokens=1, max_output_tokens=1)
        assert result_count(b.metrics, "skipped_cooldown") == 1
        assert result_count(b.metrics, "redis_error") == 1

        b.clock.now += 0.2
        with pytest.raises(BudgetUnavailable):
            await b.ledger.reserve(uuid.uuid4(), 100, input_tokens=1, max_output_tokens=1)
        assert result_count(b.metrics, "redis_error") == 2
    finally:
        await b.client.aclose()


async def test_a_failed_settlement_is_counted_not_raised(bench: Bench) -> None:
    r = await bench.ledger.reserve(uuid.uuid4(), 100, input_tokens=1, max_output_tokens=9)
    broken = make_ledger("redis://127.0.0.1:1/0", "tg:")
    try:
        r.ledger = broken.ledger  # simulate Redis vanishing between reserve and settle
        await r.settle(3)  # must not raise: the client already has its answer
        value = broken.metrics.registry.get_sample_value("tokengate_budget_settle_errors_total")
        assert value == 1
    finally:
        await broken.client.aclose()


# ---------------------------------------------------------------- HTTP, real Postgres + Redis


@dataclass
class Gateway:
    app: FastAPI
    http: httpx.AsyncClient
    key: str
    tenant: uuid.UUID
    redis: Redis
    prefix: str

    async def used(self) -> int:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        value = await self.redis.get(f"{self.prefix}budget:{{{self.tenant}}}:{day}")
        return int(value or 0)


async def provision(pg: str, settings: Settings, budget: int) -> tuple[str, uuid.UUID]:
    conn = await asyncpg.connect(pg)
    try:
        await migrate(conn, load_migrations())
    finally:
        await conn.close()
    out = io.StringIO()
    argv = ["create-tenant", "--name", "acme", "--rpm", "1000", "--daily-tokens", str(budget)]
    assert await admin(argv, settings, out) == 0, out.getvalue()
    tenant = uuid.UUID(out.getvalue().split("(")[1].rstrip(")\n"))
    out = io.StringIO()
    assert await admin(["create-key", "--tenant", "acme", "--name", "ci"], settings, out) == 0
    match = KEY_PATTERN.search(out.getvalue())
    assert match
    return match[0], tenant


@pytest.fixture
def make_gateway(pg: str, redis_url: str, redis_prefix: str) -> Callable[..., Any]:
    async def build(budget: int, **overrides: Any) -> AsyncIterator[Gateway]:
        settings = Settings(
            env="test",
            auth="required",
            database_url=SecretStr(pg),
            api_key_pepper=SecretStr(PEPPER),
            redis_url=SecretStr(redis_url),
            redis_key_prefix=redis_prefix,
            **overrides,
        )
        key, tenant = await provision(pg, settings, budget)
        app = create_app(settings)
        client = Redis.from_url(redis_url)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                yield Gateway(app, http, key, tenant, client, redis_prefix)
        await client.aclose()

    return build


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_counter_matches_the_usage_reported_to_the_client(make_gateway: Any) -> None:
    async for g in make_gateway(budget=100_000):
        resp = await g.http.post(URL, json=CHAT, headers=bearer(g.key))
        assert resp.status_code == 200
        assert await g.used() == resp.json()["usage"]["total_tokens"]


async def test_streamed_usage_is_settled_too(make_gateway: Any) -> None:
    async for g in make_gateway(budget=100_000):
        body = {**CHAT, "stream": True, "stream_options": {"include_usage": True}}
        resp = await g.http.post(URL, json=body, headers=bearer(g.key))
        assert resp.status_code == 200
        usage_line = next(
            line for line in resp.text.splitlines() if '"usage": {' in line or '"usage":{' in line
        )
        assert '"total_tokens"' in usage_line
        total = json.loads(usage_line.removeprefix("data: "))["usage"]["total_tokens"]
        assert await g.used() == total


async def test_small_budget_shrinks_the_answer_then_runs_out(make_gateway: Any) -> None:
    async for g in make_gateway(budget=6):
        resp = await g.http.post(URL, json=CHAT, headers=bearer(g.key))
        assert resp.status_code == 200
        choice = resp.json()["choices"][0]
        assert choice["finish_reason"] == "length"  # cut short to fit the budget
        assert await g.used() <= 6 + 1  # at most the input estimate's error over

        resp = await g.http.post(URL, json=CHAT, headers=bearer(g.key))
        assert resp.status_code == 429
        assert resp.json()["error"] == {
            "message": "This organization's daily token budget is used up. It resets at 00:00 UTC.",
            "type": "insufficient_quota",
            "param": None,
            "code": "insufficient_quota",
        }
        assert 1 <= int(resp.headers["retry-after"]) <= 86_400


async def test_provider_failure_refunds_the_reservation(make_gateway: Any) -> None:
    async for g in make_gateway(budget=100_000, mock_error_rate=1.0):
        assert (await g.http.post(URL, json=CHAT, headers=bearer(g.key))).status_code == 502
        stream = await g.http.post(URL, json={**CHAT, "stream": True}, headers=bearer(g.key))
        assert stream.status_code == 502
        assert await g.used() == 0


async def test_a_stream_that_breaks_midway_keeps_the_reservation(make_gateway: Any) -> None:
    async for g in make_gateway(budget=100_000, mock_mid_stream_error_rate=1.0):
        resp = await g.http.post(URL, json={**CHAT, "stream": True}, headers=bearer(g.key))
        assert resp.status_code == 200  # headers were already sent; the error is in the stream
        assert '"upstream_error"' in resp.text
        # Usage is unknown, so the full reservation stays: input estimate + the 4096 cap.
        assert await g.used() == estimate_input_tokens(["hi"]) + 4096


async def test_one_letter_words_no_longer_overspend_the_budget(make_gateway: Any) -> None:
    """Step 2.5 measured +49% here with the old 4-chars-per-token estimate (docs/RESULTS.md)."""
    budget = 3000
    async for g in make_gateway(budget=budget):
        body = {
            "model": "mock-1",
            "messages": [{"role": "user", "content": " ".join(["a"] * 200)}],
            "max_completion_tokens": 50,
        }
        responses = await asyncio.gather(
            *(g.http.post(URL, json=body, headers=bearer(g.key)) for _ in range(40))
        )
        assert {r.status_code for r in responses} <= {200, 429}
        used = sum(r.json()["usage"]["total_tokens"] for r in responses if r.status_code == 200)
        assert used > 0
        assert used <= budget
        assert await g.used() == used


async def test_openai_sdk_sees_insufficient_quota(make_gateway: Any) -> None:
    async for g in make_gateway(budget=1):  # not even input + one output token fits
        sdk = openai.AsyncOpenAI(
            api_key=g.key,
            base_url="http://t/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=g.app)),
            max_retries=0,
        )
        async with sdk:
            with pytest.raises(openai.RateLimitError) as info:
                await sdk.chat.completions.create(model="mock-1", messages=QUESTION)
        assert info.value.code == "insufficient_quota"
        metrics = get_state(g.app).metrics
        assert result_count(metrics, "exhausted") == 1
