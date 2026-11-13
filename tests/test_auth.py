"""API keys and authentication: format, fingerprints, cache, the 401 path, and the SDK."""

import io
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

import asyncpg
import httpx
import httpx2
import openai
import pydantic
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import SecretStr

from app.admin.cli import run as admin
from app.auth.keys import (
    KEY_PATTERN,
    GeneratedKey,
    fingerprint,
    generate_key,
    matches,
    parse_key,
)
from app.auth.store import KeyRecord, KeyStore
from app.config import Settings
from app.db.migrate import load_migrations, migrate
from app.main import create_app
from app.state import get_state
from app.telemetry.logging import configure_logging

PEPPER = "test-pepper-0123456789-abcdefghijklmnop"
URL = "/v1/chat/completions"
CHAT = {"model": "mock-1", "messages": [{"role": "user", "content": "hi"}]}
QUESTION: list[ChatCompletionMessageParam] = [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------- key format


def test_generated_keys_have_the_documented_format() -> None:
    key = generate_key()
    assert KEY_PATTERN.fullmatch(key.full)
    assert re.fullmatch(r"tg_[A-Za-z0-9]{12}_[A-Za-z0-9]{43}", key.full)


def test_generated_keys_are_unique() -> None:
    keys = {generate_key().full for _ in range(10_000)}
    assert len(keys) == 10_000


def test_printing_a_key_object_never_shows_the_secret() -> None:
    key = generate_key()
    assert key.secret not in repr(key)
    assert key.secret not in str(key)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sk-openai-style-key",
        "tg_short_" + "a" * 43,
        "tg_" + "a" * 12 + "_" + "a" * 42,  # secret one char short
        "tg_" + "a" * 12 + "_" + "a" * 44,
        "tg_" + "a" * 12 + "_" + "a" * 42 + "!",
        " tg_" + "a" * 12 + "_" + "a" * 43,  # whitespace isn't part of a key
    ],
)
def test_malformed_keys_are_rejected_without_a_lookup(raw: str) -> None:
    assert parse_key(raw) is None


def test_fingerprint_depends_on_the_pepper() -> None:
    secret = generate_key().secret
    a = fingerprint(secret, b"pepper-a" * 4)
    assert len(a) == 32
    assert a == fingerprint(secret, b"pepper-a" * 4)
    assert a != fingerprint(secret, b"pepper-b" * 4)
    assert matches(secret, b"pepper-a" * 4, a)
    assert not matches(secret + "x", b"pepper-a" * 4, a)


# ---------------------------------------------------------------- settings


def test_prod_refuses_disabled_auth() -> None:
    with pytest.raises(pydantic.ValidationError, match="cannot be disabled in prod"):
        Settings(env="prod", auth="disabled")


def test_required_auth_needs_database_and_pepper() -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        Settings(env="local", auth="required")
    assert "TOKENGATE_DATABASE_URL" in str(info.value)
    assert "TOKENGATE_API_KEY_PEPPER" in str(info.value)


def test_short_pepper_rejected_and_pepper_never_printed() -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(env="test", api_key_pepper=SecretStr("short"))
    settings = Settings(env="test", api_key_pepper=SecretStr(PEPPER))
    assert PEPPER not in repr(settings)


# ---------------------------------------------------------------- cache (no database)


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


class FakePool:
    """Stands in for asyncpg.Pool: returns rows from a dict and counts queries."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.queries = 0

    async def fetchrow(self, query: str, prefix: str) -> dict[str, Any] | None:
        self.queries += 1
        return self.rows.get(prefix)


def row(prefix: str, revoked: bool = False) -> dict[str, Any]:
    return {
        "key_id": uuid.uuid4(),
        "prefix": prefix,
        "key_hash": b"\x00" * 32,
        "key_revoked": revoked,
        "key_requests_per_minute": None,
        "tenant_id": uuid.uuid4(),
        "tenant_name": "acme",
        "tenant_disabled": False,
        "tenant_requests_per_minute": 60,
        "daily_token_budget": 1000,
    }


def store(pool: FakePool, clock: FakeClock, ttl: float = 60, max_entries: int = 100) -> KeyStore:
    return KeyStore(cast(asyncpg.Pool, pool), ttl_s=ttl, max_entries=max_entries, clock=clock)


async def test_cache_hits_skip_the_database() -> None:
    pool, clock = FakePool({"aaaaaaaaaaaa": row("aaaaaaaaaaaa")}), FakeClock()
    s = store(pool, clock)
    for _ in range(5):
        assert isinstance(await s.lookup("aaaaaaaaaaaa"), KeyRecord)
    assert pool.queries == 1


async def test_unknown_keys_are_cached_too() -> None:
    pool, clock = FakePool({}), FakeClock()
    s = store(pool, clock)
    for _ in range(100):
        assert await s.lookup("zzzzzzzzzzzz") is None
    assert pool.queries == 1  # a flood of the same bad key is one database query


async def test_revocation_is_seen_after_the_ttl() -> None:
    pool, clock = FakePool({"aaaaaaaaaaaa": row("aaaaaaaaaaaa")}), FakeClock()
    s = store(pool, clock, ttl=60)
    assert not (await s.lookup("aaaaaaaaaaaa")).key_revoked  # type: ignore[union-attr]
    pool.rows["aaaaaaaaaaaa"] = row("aaaaaaaaaaaa", revoked=True)  # revoked in the database

    clock.now += 59  # still cached: the documented trade-off (D33)
    assert not (await s.lookup("aaaaaaaaaaaa")).key_revoked  # type: ignore[union-attr]
    clock.now += 2  # TTL passed
    assert (await s.lookup("aaaaaaaaaaaa")).key_revoked  # type: ignore[union-attr]


async def test_cache_size_is_bounded() -> None:
    pool, clock = FakePool({}), FakeClock()
    s = store(pool, clock, max_entries=10)
    for i in range(1000):
        await s.lookup(f"k{i:011d}")
    assert len(s._cache) == 10


async def test_ttl_zero_disables_the_cache() -> None:
    pool, clock = FakePool({"aaaaaaaaaaaa": row("aaaaaaaaaaaa")}), FakeClock()
    s = store(pool, clock, ttl=0)
    for _ in range(3):
        await s.lookup("aaaaaaaaaaaa")
    assert pool.queries == 3


# ---------------------------------------------------------------- HTTP, real Postgres


@dataclass
class Gateway:
    app: FastAPI
    http: httpx.AsyncClient
    key: str
    settings: Settings
    logs: io.StringIO


async def admin_ok(settings: Settings, *argv: str) -> str:
    out = io.StringIO()
    assert await admin(list(argv), settings, out) == 0, out.getvalue()
    return out.getvalue()


@pytest.fixture
async def gateway(pg: str) -> AsyncIterator[Gateway]:
    conn = await asyncpg.connect(pg)
    await migrate(conn, load_migrations())
    await conn.close()
    settings = Settings(
        env="test",
        auth="required",
        database_url=SecretStr(pg),
        api_key_pepper=SecretStr(PEPPER),
        auth_cache_ttl_s=0,  # see revocations immediately in these tests
    )
    await admin_ok(
        settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "1000"
    )
    output = await admin_ok(settings, "create-key", "--tenant", "acme", "--name", "ci")
    match = KEY_PATTERN.search(output)
    assert match
    app = create_app(settings)
    logs = io.StringIO()
    async with LifespanManager(app) as manager:
        configure_logging("INFO", stream=logs)
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            yield Gateway(app, http, match[0], settings, logs)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def failures(g: Gateway, reason: str) -> float:
    value = get_state(g.app).metrics.registry.get_sample_value(
        "tokengate_auth_failures_total", {"reason": reason}
    )
    return value or 0.0


async def test_valid_key_is_accepted_and_tenant_is_logged(gateway: Gateway) -> None:
    resp = await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key))
    assert resp.status_code == 200
    access = [line for line in gateway.logs.getvalue().splitlines() if '"request"' in line]
    assert '"tenant": "acme"' in access[-1]
    assert f'"key_prefix": "{parse_key(gateway.key)[0]}"' in access[-1]  # type: ignore[index]


async def test_full_key_never_appears_in_logs(gateway: Gateway) -> None:
    secret = parse_key(gateway.key)[1]  # type: ignore[index]
    await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key))
    await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key[:-1] + "X"))
    assert secret not in gateway.logs.getvalue()
    assert secret[:-1] not in gateway.logs.getvalue()


def other_secret_same_prefix(key: str) -> str:
    prefix, _ = parse_key(key)  # type: ignore[misc]
    return f"tg_{prefix}_{generate_key().secret}"


@pytest.mark.parametrize(
    ("make_headers", "reason"),
    [
        (lambda key: {}, "missing"),
        (lambda key: {"Authorization": f"Basic {key}"}, "malformed"),
        (lambda key: {"Authorization": "Bearer sk-not-a-tokengate-key"}, "malformed"),
        (lambda key: {"Authorization": f"Bearer {key[:-1]}"}, "malformed"),
        (lambda key: bearer(generate_key().full), "unknown"),
        (lambda key: bearer(other_secret_same_prefix(key)), "wrong_secret"),
    ],
)
async def test_every_failure_looks_the_same_to_the_client(
    gateway: Gateway, make_headers: Any, reason: str
) -> None:
    resp = await gateway.http.post(URL, json=CHAT, headers=make_headers(gateway.key))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {
        "error": {
            "message": "Invalid or missing API key. Send it as 'Authorization: Bearer <key>'.",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    assert failures(gateway, reason) == 1


async def test_revoked_key_and_disabled_tenant_are_rejected(gateway: Gateway) -> None:
    prefix = parse_key(gateway.key)[0]  # type: ignore[index]
    await admin_ok(gateway.settings, "revoke-key", "--prefix", prefix)
    assert (await gateway.http.post(URL, json=CHAT, headers=bearer(gateway.key))).status_code == 401
    assert failures(gateway, "revoked") == 1

    second = KEY_PATTERN.search(
        await admin_ok(gateway.settings, "create-key", "--tenant", "acme", "--name", "two")
    )
    assert second
    assert (await gateway.http.post(URL, json=CHAT, headers=bearer(second[0]))).status_code == 200
    await admin_ok(gateway.settings, "disable-tenant", "--name", "acme")
    assert (await gateway.http.post(URL, json=CHAT, headers=bearer(second[0]))).status_code == 401
    assert failures(gateway, "tenant_disabled") == 1


async def test_key_status_is_hidden_from_someone_without_the_secret(gateway: Gateway) -> None:
    # The secret is checked before revocation, so a guesser with only the (public) prefix
    # can't learn that this key exists and was revoked: they just see "wrong_secret".
    prefix = parse_key(gateway.key)[0]  # type: ignore[index]
    await admin_ok(gateway.settings, "revoke-key", "--prefix", prefix)
    resp = await gateway.http.post(
        URL, json=CHAT, headers=bearer(other_secret_same_prefix(gateway.key))
    )
    assert resp.status_code == 401
    assert failures(gateway, "wrong_secret") == 1
    assert failures(gateway, "revoked") == 0


async def test_health_and_metrics_need_no_api_key(gateway: Gateway) -> None:
    assert (await gateway.http.get("/healthz")).status_code == 200
    assert (await gateway.http.get("/readyz")).status_code == 200
    assert (await gateway.http.get("/metrics")).status_code == 200  # test env, no token


async def test_database_down_means_503_not_401() -> None:
    settings = Settings(
        env="test",
        auth="required",
        database_url=SecretStr("postgresql://127.0.0.1:1/nothing"),
        api_key_pepper=SecretStr(PEPPER),
        db_connect_timeout_s=0.5,
    )
    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            resp = await http.post(URL, json=CHAT, headers=bearer(generate_key().full))
    assert resp.status_code == 503  # fail closed, but don't blame the client's key
    assert resp.json()["error"]["code"] == "auth_unavailable"


async def test_openai_sdk_with_a_real_key(gateway: Gateway) -> None:
    async def sdk(key: str) -> openai.AsyncOpenAI:
        return openai.AsyncOpenAI(
            api_key=key,
            base_url="http://t/v1",
            http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway.app)),
            max_retries=0,
        )

    async with await sdk(gateway.key) as good:
        reply = await good.chat.completions.create(model="mock-1", messages=QUESTION)
        assert reply.choices[0].message.content
    async with await sdk(generate_key().full) as bad:
        with pytest.raises(openai.AuthenticationError) as info:
            await bad.chat.completions.create(model="mock-1", messages=QUESTION)
        assert info.value.code == "invalid_api_key"


def test_generated_key_type_is_frozen() -> None:
    key = GeneratedKey(prefix="a" * 12, secret="b" * 43)
    with pytest.raises(AttributeError):
        key.secret = "c" * 43  # type: ignore[misc]
