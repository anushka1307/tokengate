"""Rate limiting: the token-bucket script against real Redis, fail-open, and the 429 path.

Needs TOKENGATE_TEST_REDIS_URL (and TOKENGATE_TEST_DATABASE_URL for the HTTP tests), e.g.
    TOKENGATE_TEST_REDIS_URL=redis://localhost:6379/15
"""

import asyncio
import io
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import asyncpg
import httpx
import httpx2
import openai
import pytest
import redis.asyncio as redis
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import SecretStr

from app.admin.cli import run as admin
from app.auth.keys import KEY_PATTERN
from app.config import Settings
from app.db.migrate import load_migrations, migrate
from app.main import create_app
from app.ratelimit.limiter import Limits, RateLimiter, create_redis
from app.state import get_state
from app.telemetry.logging import configure_logging
from app.telemetry.metrics import Metrics

PEPPER = "ratelimit-test-pepper-0123456789-abcdef"
URL = "/v1/chat/completions"
CHAT = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}
QUESTION: list[ChatCompletionMessageParam] = [{"role": "user", "content": "hi"}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limits(key_rpm: int, tenant_rpm: int, tenant: uuid.UUID | None = None) -> Limits:
    return Limits(
        tenant_id=tenant or uuid.uuid4(),
        key_id=uuid.uuid4(),
        key_rpm=key_rpm,
        tenant_rpm=tenant_rpm,
    )


def decisions(metrics: Metrics, result: str) -> float:
    value = metrics.registry.get_sample_value(
        "tokengate_ratelimit_decisions_total", {"result": result}
    )
    return value or 0.0


@dataclass
class Bench:
    limiter: RateLimiter
    client: redis.Redis
    metrics: Metrics
    clock: FakeClock


def settings_for(url: str, **overrides: object) -> Settings:
    return Settings(env="test", redis_url=SecretStr(url), **overrides)  # type: ignore[arg-type]


def make_limiter(settings: Settings, prefix: str, metrics: Metrics, clock: FakeClock) -> Bench:
    client = create_redis(settings)
    limiter = RateLimiter(
        client,
        key_prefix=prefix,
        cooldown_s=5,
        timeout_s=settings.redis_connect_timeout_s + settings.redis_timeout_s,
        metrics=metrics,
        clock=clock,
    )
    return Bench(limiter, client, metrics, clock)


@pytest.fixture
async def bench(redis_url: str, redis_prefix: str) -> AsyncIterator[Bench]:
    b = make_limiter(settings_for(redis_url), redis_prefix, Metrics(), FakeClock())
    yield b
    await b.client.aclose()


# ---------------------------------------------------------------- the script, real Redis


async def test_allows_a_full_bucket_then_limits(bench: Bench) -> None:
    lim = limits(key_rpm=5, tenant_rpm=100)
    remaining = []
    for _ in range(5):
        d = await bench.limiter.check(lim)
        assert d.allowed
        remaining.append(d.remaining)
    assert remaining == [4, 3, 2, 1, 0]
    refused = await bench.limiter.check(lim)
    assert not refused.allowed
    assert refused.limited_by == "key"
    assert refused.remaining == 0
    assert decisions(bench.metrics, "allowed") == 5
    assert decisions(bench.metrics, "limited_key") == 1


async def test_exactly_capacity_requests_win_under_concurrency(bench: Bench) -> None:
    # 50 simultaneous checks against a bucket of 10: the script is atomic, so exactly 10 pass.
    lim = limits(key_rpm=10, tenant_rpm=1000)
    results = await asyncio.gather(*(bench.limiter.check(lim) for _ in range(50)))
    assert sum(d.allowed for d in results) == 10


async def test_keys_have_separate_buckets(bench: Bench) -> None:
    tenant = uuid.uuid4()
    a, b = limits(2, 100, tenant), limits(2, 100, tenant)
    assert (await bench.limiter.check(a)).allowed
    assert (await bench.limiter.check(a)).allowed
    assert not (await bench.limiter.check(a)).allowed
    assert (await bench.limiter.check(b)).allowed  # key B is unaffected by key A


async def test_tenant_bucket_is_shared_and_refusals_charge_neither_bucket(bench: Bench) -> None:
    tenant = uuid.uuid4()
    a, b = limits(100, 3, tenant), limits(100, 3, tenant)
    for _ in range(3):
        assert (await bench.limiter.check(a)).allowed
    refused = await bench.limiter.check(b)  # B never used its own bucket, but the tenant is empty
    assert not refused.allowed
    assert refused.limited_by == "tenant"
    assert decisions(bench.metrics, "limited_tenant") == 1

    # Both-or-neither: B's refused request did not take a token from B's own bucket.
    b_key, _ = bench.limiter.bucket_keys(b)
    b_tokens = float(await bench.client.hget(b_key, "tokens") or 0)
    assert b_tokens > 99.99


async def test_bucket_refills_at_the_configured_rate(bench: Bench) -> None:
    lim = limits(key_rpm=600, tenant_rpm=6000)  # 10 tokens a second
    # Drain until refused. Not a fixed 600 calls: tokens keep refilling while we drain.
    for _ in range(1000):
        refused = await bench.limiter.check(lim)
        if not refused.allowed:
            break
    assert not refused.allowed
    assert refused.retry_after_s == 1  # well under a second, rounded up and never 0
    await asyncio.sleep(0.25)  # about 2.5 tokens come back
    assert (await bench.limiter.check(lim)).allowed


async def test_retry_after_matches_the_refill_rate(bench: Bench) -> None:
    lim = limits(key_rpm=2, tenant_rpm=100)  # one token every 30 s
    await bench.limiter.check(lim)
    await bench.limiter.check(lim)
    refused = await bench.limiter.check(lim)
    assert 29 <= refused.retry_after_s <= 30


async def test_buckets_expire_once_they_would_be_full(bench: Bench) -> None:
    lim = limits(key_rpm=60, tenant_rpm=120)
    await bench.limiter.check(lim)
    key_bucket, tenant_bucket = bench.limiter.bucket_keys(lim)
    # One token used: full again after 1 s (key) / 0.5 s (tenant), plus 1 s of slack.
    assert 0 < await bench.client.pttl(key_bucket) <= 2000
    assert 0 < await bench.client.pttl(tenant_bucket) <= 1500


async def test_bucket_keys_share_a_cluster_hash_tag(bench: Bench) -> None:
    lim = limits(1, 1)
    key_bucket, tenant_bucket = bench.limiter.bucket_keys(lim)
    tag = f"{{{lim.tenant_id}}}"
    assert tag in key_bucket and tag in tenant_bucket


# ---------------------------------------------------------------- failing open


async def test_redis_down_fails_open_then_skips_redis_during_cooldown() -> None:
    metrics, clock = Metrics(), FakeClock()
    b = make_limiter(settings_for("redis://127.0.0.1:1/0"), "tg:", metrics, clock)
    try:
        first = await b.limiter.check(limits(1, 1))
        assert first.allowed and first.remaining is None
        assert decisions(metrics, "redis_error") == 1

        clock.now += 4.9  # still cooling down: Redis isn't even tried
        assert (await b.limiter.check(limits(1, 1))).allowed
        assert decisions(metrics, "skipped_cooldown") == 1
        assert decisions(metrics, "redis_error") == 1

        clock.now += 0.2  # cooldown over: tries again (and fails again)
        assert (await b.limiter.check(limits(1, 1))).allowed
        assert decisions(metrics, "redis_error") == 2
    finally:
        await b.client.aclose()


async def test_a_redis_that_never_answers_costs_at_most_the_timeout() -> None:
    # A server that accepts connections and then says nothing: the worst kind of outage.
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()  # until the client gives up and closes
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    metrics = Metrics()
    settings = settings_for(f"redis://127.0.0.1:{port}/0", redis_timeout_s=0.2)
    b = make_limiter(settings, "tg:", metrics, FakeClock())
    try:
        started = time.perf_counter()
        decision = await b.limiter.check(limits(1, 1))
        elapsed = time.perf_counter() - started
    finally:
        await b.client.aclose()
        server.close()
        await server.wait_closed()
    assert decision.allowed
    assert elapsed < 0.7  # 0.2 s read timeout; the 0.7 s backstop is never reached
    assert decisions(metrics, "redis_error") == 1


# ---------------------------------------------------------------- HTTP, real Postgres + Redis


@dataclass
class Gateway:
    app: FastAPI
    http: httpx.AsyncClient
    key: str


async def start_gateway(pg: str, redis_url: str, prefix: str, rpm: int) -> tuple[Settings, str]:
    conn = await asyncpg.connect(pg)
    await migrate(conn, load_migrations())
    await conn.close()
    settings = Settings(
        env="test",
        auth="required",
        database_url=SecretStr(pg),
        api_key_pepper=SecretStr(PEPPER),
        redis_url=SecretStr(redis_url),
        redis_key_prefix=prefix,
    )
    for argv in (
        ["create-tenant", "--name", "acme", "--rpm", str(rpm), "--daily-tokens", "1000"],
        ["create-key", "--tenant", "acme", "--name", "ci"],
    ):
        out = io.StringIO()
        assert await admin(argv, settings, out) == 0, out.getvalue()
    match = KEY_PATTERN.search(out.getvalue())
    assert match
    return settings, match[0]


@pytest.fixture
async def gateway(pg: str, redis_url: str, redis_prefix: str) -> AsyncIterator[Gateway]:
    settings, key = await start_gateway(pg, redis_url, redis_prefix, rpm=3)
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        configure_logging("INFO", stream=io.StringIO())
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            yield Gateway(app, http, key)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_429_with_retry_after_once_the_bucket_is_empty(gateway: Gateway) -> None:
    seen = []
    for _ in range(3):
        resp = await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key))
        assert resp.status_code == 200
        seen.append(resp.headers["x-ratelimit-remaining"])
    assert seen == ["2", "1", "0"]

    resp = await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key))
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["retry-after"]) <= 20  # 3 rpm: a token every 20 s
    assert resp.headers["x-ratelimit-remaining"] == "0"
    error = resp.json()["error"]
    assert error["code"] == "rate_limit_exceeded"
    assert error["type"] == "rate_limit_error"

    # Health and metrics are never rate limited.
    assert (await gateway.http.get("/healthz")).status_code == 200
    assert (await gateway.http.get("/readyz")).status_code == 200


async def test_streaming_responses_carry_the_header(gateway: Gateway) -> None:
    resp = await gateway.http.post(URL, json={**CHAT, "stream": True}, headers=bearer(gateway.key))
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-remaining"] == "2"


async def test_openai_sdk_raises_rate_limit_error(gateway: Gateway) -> None:
    sdk = openai.AsyncOpenAI(
        api_key=gateway.key,
        base_url="http://t/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway.app)),
        max_retries=0,  # the SDK would otherwise wait out Retry-After and try again
    )
    async with sdk:
        for _ in range(3):
            await sdk.chat.completions.create(model="mock-1", messages=QUESTION)
        with pytest.raises(openai.RateLimitError) as info:
            await sdk.chat.completions.create(model="mock-1", messages=QUESTION)
    assert info.value.code == "rate_limit_exceeded"


async def test_requests_still_work_when_redis_is_down(pg: str, redis_prefix: str) -> None:
    settings, key = await start_gateway(pg, "redis://127.0.0.1:1/0", redis_prefix, rpm=1)
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            # rpm is 1, yet both pass: rate limiting fails open, and says nothing it can't know.
            for _ in range(2):
                resp = await http.post(URL, json=CHAT, headers=bearer(key))
                assert resp.status_code == 200
                assert "x-ratelimit-remaining" not in resp.headers
            assert (await http.get("/readyz")).status_code == 200  # Redis isn't a readiness check
        metrics = get_state(app).metrics
        assert decisions(metrics, "redis_error") == 1
        assert decisions(metrics, "skipped_cooldown") == 1
