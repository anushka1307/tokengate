"""Postgres foundation: migrations, schema rules, pool timeouts, and readiness.

Tests marked with the ``pg`` fixture need a real Postgres:
    TOKENGATE_TEST_DATABASE_URL=postgresql://localhost/tokengate_test uv run pytest
Each test gets its own throwaway schema. Without the URL they are skipped locally; CI sets
TOKENGATE_REQUIRE_DB_TESTS=1 so they fail instead of silently skipping.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pydantic
import pytest
from asgi_lifespan import LifespanManager
from pydantic import SecretStr

from app.config import MigrationSettings, Settings
from app.db import migrate as migrate_cli
from app.db.migrate import Migration, MigrationError, load_migrations, migrate
from app.db.pool import create_pool
from app.main import create_app
from app.state import get_state

UNREACHABLE = "postgresql://127.0.0.1:1/nothing"  # port 1: nothing listens there


@pytest.fixture
async def conn(pg: str) -> AsyncIterator[asyncpg.Connection]:
    c = await asyncpg.connect(pg)
    try:
        yield c
    finally:
        await c.close()


async def tables(c: asyncpg.Connection) -> set[str]:
    rows = await c.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    return {r["tablename"] for r in rows}


# ---------------------------------------------------------------- loading migration files


def test_shipped_migrations_load_in_order() -> None:
    migrations = load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "tenants_and_keys"


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"0001_ok.sql": "", "bad name.sql": ""}, "bad migration filename"),
        ({"0001_a.sql": "", "0003_c.sql": ""}, "no gaps"),
        ({"0002_b.sql": ""}, "no gaps"),
    ],
)
def test_bad_migration_files_are_rejected(
    tmp_path: Path, files: dict[str, str], error: str
) -> None:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    with pytest.raises(MigrationError, match=error):
        load_migrations(tmp_path)


# ---------------------------------------------------------------- applying migrations


async def test_migrate_applies_once_then_is_a_no_op(conn: asyncpg.Connection) -> None:
    migrations = load_migrations()
    assert await migrate(conn, migrations) == [m.version for m in migrations]
    assert {"tenants", "api_keys", "schema_migrations"} <= await tables(conn)
    assert await migrate(conn, migrations) == []


async def test_edited_migration_is_refused(conn: asyncpg.Connection) -> None:
    original = load_migrations()
    await migrate(conn, original)
    edited = [Migration(original[0].version, original[0].name, original[0].sql + "\n-- edit")]
    with pytest.raises(MigrationError, match="changed after it was applied"):
        await migrate(conn, edited)


async def test_unknown_applied_version_is_refused(conn: asyncpg.Connection) -> None:
    extra = Migration(2, "future", "CREATE TABLE future_thing (x int)")
    await migrate(conn, [*load_migrations(), extra])
    with pytest.raises(MigrationError, match="doesn't know"):
        await migrate(conn, load_migrations())


async def test_failed_migration_leaves_nothing_behind(conn: asyncpg.Connection) -> None:
    broken = Migration(2, "broken", "CREATE TABLE half_done (x int); SELECT 1 / 0;")
    with pytest.raises(asyncpg.DivisionByZeroError):
        await migrate(conn, [*load_migrations(), broken])
    assert "half_done" not in await tables(conn)
    versions = [r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")]
    assert versions == [1]  # migration 1 committed; 2 rolled back entirely


async def test_concurrent_runners_apply_each_migration_once(pg: str) -> None:
    a, b = await asyncpg.connect(pg), await asyncpg.connect(pg)
    try:
        results = await asyncio.gather(migrate(a, load_migrations()), migrate(b, load_migrations()))
        assert sorted(v for applied in results for v in applied) == [1]
        assert await a.fetchval("SELECT count(*) FROM schema_migrations") == 1
    finally:
        await a.close()
        await b.close()


# ---------------------------------------------------------------- schema rules


async def add_tenant(c: asyncpg.Connection, **overrides: object) -> uuid.UUID:
    row = {"name": "pipelinemedic", "requests_per_minute": 60, "daily_token_budget": 100_000}
    row.update(overrides)
    tenant_id: uuid.UUID = await c.fetchval(
        "INSERT INTO tenants (name, requests_per_minute, daily_token_budget) "
        "VALUES ($1, $2, $3) RETURNING id",
        row["name"],
        row["requests_per_minute"],
        row["daily_token_budget"],
    )
    return tenant_id


async def add_key(c: asyncpg.Connection, tenant_id: uuid.UUID, **overrides: object) -> None:
    row: dict[str, object] = {"prefix": "abcd1234", "key_hash": b"\x00" * 32, "name": "ci"}
    row.update(overrides)
    await c.execute(
        "INSERT INTO api_keys (tenant_id, prefix, key_hash, name) VALUES ($1, $2, $3, $4)",
        tenant_id,
        row["prefix"],
        row["key_hash"],
        row["name"],
    )


async def test_valid_rows_are_accepted(conn: asyncpg.Connection) -> None:
    await migrate(conn, load_migrations())
    tenant = await add_tenant(conn)
    await add_key(conn, tenant)
    assert await conn.fetchval("SELECT count(*) FROM api_keys WHERE tenant_id = $1", tenant) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"requests_per_minute": 0},
        {"daily_token_budget": 0},
        {"name": "Has Capitals"},
        {"name": "x"},  # too short
        {"name": "-leading-dash"},
    ],
)
async def test_bad_tenants_are_rejected(
    conn: asyncpg.Connection, overrides: dict[str, object]
) -> None:
    await migrate(conn, load_migrations())
    with pytest.raises(asyncpg.CheckViolationError):
        await add_tenant(conn, **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"key_hash": b"\x00" * 31},  # not a SHA-256 digest
        {"prefix": "short"},
        {"prefix": "has-dash!"},
        {"name": ""},
    ],
)
async def test_bad_keys_are_rejected(
    conn: asyncpg.Connection, overrides: dict[str, object]
) -> None:
    await migrate(conn, load_migrations())
    tenant = await add_tenant(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await add_key(conn, tenant, **overrides)


async def test_uniqueness_and_delete_protection(conn: asyncpg.Connection) -> None:
    await migrate(conn, load_migrations())
    tenant = await add_tenant(conn)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_tenant(conn)  # same name
    await add_key(conn, tenant)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_key(conn, tenant)  # same prefix
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("DELETE FROM tenants WHERE id = $1", tenant)


# ---------------------------------------------------------------- pool and readiness


async def test_postgres_enforces_the_query_timeout(pg: str) -> None:
    settings = Settings(env="test", database_url=SecretStr(pg), db_query_timeout_s=0.2)
    pool = await create_pool(settings)
    try:
        async with pool.acquire() as c:
            # Server side: statement_timeout cancels it even if the client never gave up.
            with pytest.raises(asyncpg.QueryCanceledError):
                await c.execute("SELECT pg_sleep(1)", timeout=5)
            # Client side: command_timeout gives up after 0.2 s by default.
            with pytest.raises(TimeoutError):
                await c.execute("SET statement_timeout = 0; SELECT pg_sleep(1)")
    finally:
        await pool.close()


async def test_ready_when_database_answers(pg: str) -> None:
    app = create_app(Settings(env="test", database_url=SecretStr(pg)))
    async with LifespanManager(app) as manager:
        assert get_state(app).db_pool is not None
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/readyz")).status_code == 200
    assert get_state(app).db_pool is None  # closed on shutdown


async def test_unreachable_database_means_not_ready_but_alive() -> None:
    settings = Settings(env="test", database_url=SecretStr(UNREACHABLE), db_connect_timeout_s=0.5)
    app = create_app(settings)
    # Startup must succeed even though the database is down (D31).
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            started = time.perf_counter()
            ready = await c.get("/readyz")
            elapsed = time.perf_counter() - started
            alive = await c.get("/healthz")
    assert ready.status_code == 503
    assert ready.json() == {"status": "not_ready"}  # doesn't reveal which dependency failed
    assert elapsed < 2  # bounded by the 1 s readiness check, not a hang
    assert alive.status_code == 200


async def test_no_database_configured_keeps_phase_1_behaviour() -> None:
    app = create_app(Settings(env="test"))
    async with LifespanManager(app) as manager:
        assert get_state(app).db_pool is None
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/readyz")).status_code == 200


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize("url", ["mysql://localhost/x", "localhost:5432", "http://db"])
def test_database_url_must_be_postgres(url: str) -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        Settings(env="test", database_url=SecretStr(url))
    assert url not in str(info.value)  # the URL (maybe with a password) isn't echoed


def test_database_password_never_printed() -> None:
    url = "postgresql://app:hunter2-very-secret@db.example/tokengate"
    settings = Settings(env="test", database_url=SecretStr(url))
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())


def test_pool_min_cannot_exceed_max() -> None:
    with pytest.raises(pydantic.ValidationError, match="must not exceed"):
        Settings(env="test", db_pool_min_size=6, db_pool_max_size=5)


async def test_migrate_command_needs_only_the_database_url(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: with auth required (the default), the full Settings demands a pepper. The
    # migration command must not, because a migration job never handles API keys.
    monkeypatch.delenv("TOKENGATE_AUTH", raising=False)
    monkeypatch.delenv("TOKENGATE_API_KEY_PEPPER", raising=False)
    monkeypatch.setenv("TOKENGATE_DATABASE_URL", pg)
    assert await migrate_cli._main() == 0
    assert await migrate_cli._main() == 0  # second run applies nothing and still succeeds


def test_migration_settings_reject_a_non_postgres_url() -> None:
    with pytest.raises(pydantic.ValidationError, match="must start with postgresql://"):
        MigrationSettings(database_url=SecretStr("mysql://user:secret@host/db"))
