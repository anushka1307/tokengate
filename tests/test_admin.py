"""python -m app.admin: tenant and key management against real Postgres."""

import io

import asyncpg
import pytest
from pydantic import SecretStr

from app.admin.cli import run
from app.auth.keys import KEY_PATTERN, fingerprint, parse_key
from app.config import Settings
from app.db.migrate import load_migrations, migrate

PEPPER = "admin-test-pepper-0123456789-abcdefgh"


@pytest.fixture
async def settings(pg: str) -> Settings:
    conn = await asyncpg.connect(pg)
    await migrate(conn, load_migrations())
    await conn.close()
    return Settings(env="test", database_url=SecretStr(pg), api_key_pepper=SecretStr(PEPPER))


async def cli(settings: Settings, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = await run(list(argv), settings, out)
    return code, out.getvalue()


async def test_create_tenant_then_duplicate_is_a_friendly_error(settings: Settings) -> None:
    code, out = await cli(
        settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500"
    )
    assert code == 0 and "created tenant acme" in out
    code, out = await cli(
        settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500"
    )
    assert code == 1 and "already exists" in out


async def test_invalid_values_are_refused_by_the_database(settings: Settings) -> None:
    code, out = await cli(
        settings, "create-tenant", "--name", "acme", "--rpm", "0", "--daily-tokens", "500"
    )
    assert code == 1 and "not allowed" in out


async def test_create_key_prints_it_once_and_stores_only_a_fingerprint(
    settings: Settings, pg: str
) -> None:
    await cli(settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500")
    code, out = await cli(settings, "create-key", "--tenant", "acme", "--name", "ci")
    assert code == 0
    keys = KEY_PATTERN.findall(out)
    assert len(keys) == 1  # printed exactly once
    full = KEY_PATTERN.search(out)[0]  # type: ignore[index]
    prefix, secret = parse_key(full)  # type: ignore[misc]

    conn = await asyncpg.connect(pg)
    try:
        row = await conn.fetchrow("SELECT * FROM api_keys WHERE prefix = $1", prefix)
        stored = " ".join(str(v) for v in row.values())  # type: ignore[union-attr]
    finally:
        await conn.close()
    assert secret not in stored  # the secret itself is nowhere in the row
    assert row["key_hash"] == fingerprint(secret, PEPPER.encode())  # type: ignore[index]


async def test_list_shows_prefixes_not_secrets(settings: Settings) -> None:
    await cli(settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500")
    _, created = await cli(settings, "create-key", "--tenant", "acme", "--name", "ci")
    prefix, secret = parse_key(KEY_PATTERN.search(created)[0])  # type: ignore[index, misc]
    code, listing = await cli(settings, "list-keys", "--tenant", "acme")
    assert code == 0
    assert prefix in listing and "active" in listing and "ci" in listing
    assert secret not in listing


async def test_revoke_and_disable(settings: Settings) -> None:
    await cli(settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500")
    _, created = await cli(settings, "create-key", "--tenant", "acme", "--name", "ci")
    prefix = parse_key(KEY_PATTERN.search(created)[0])[0]  # type: ignore[index]

    code, out = await cli(settings, "revoke-key", "--prefix", prefix)
    assert code == 0 and "up to 60 s" in out  # tells the operator about the cache delay
    assert "revoked" in (await cli(settings, "list-keys", "--tenant", "acme"))[1]
    assert (await cli(settings, "revoke-key", "--prefix", prefix))[0] == 1  # already revoked

    assert (await cli(settings, "disable-tenant", "--name", "acme"))[0] == 0
    assert (await cli(settings, "disable-tenant", "--name", "acme"))[0] == 1


@pytest.mark.parametrize(
    "argv",
    [
        ("create-key", "--tenant", "nobody", "--name", "ci"),
        ("revoke-key", "--prefix", "AAAAAAAAAAAA"),
        ("disable-tenant", "--name", "nobody"),
    ],
)
async def test_unknown_names_are_errors(settings: Settings, argv: tuple[str, ...]) -> None:
    code, out = await cli(settings, *argv)
    assert code == 1 and out.startswith("error:")


async def test_create_key_needs_the_pepper(pg: str) -> None:
    settings = Settings(env="test", database_url=SecretStr(pg))
    conn = await asyncpg.connect(pg)
    await migrate(conn, load_migrations())
    await conn.close()
    await cli(settings, "create-tenant", "--name", "acme", "--rpm", "60", "--daily-tokens", "500")
    code, out = await cli(settings, "create-key", "--tenant", "acme", "--name", "ci")
    assert code == 1 and "PEPPER" in out
