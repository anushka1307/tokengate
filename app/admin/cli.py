"""Admin commands for tenants and API keys.

    uv run python -m app.admin create-tenant --name pipelinemedic --rpm 60 --daily-tokens 200000
    uv run python -m app.admin create-key --tenant pipelinemedic --name ci
    uv run python -m app.admin list-keys --tenant pipelinemedic
    uv run python -m app.admin revoke-key --prefix Ab3dEf9hJk2L
    uv run python -m app.admin disable-tenant --name pipelinemedic

Uses TOKENGATE_DATABASE_URL and TOKENGATE_API_KEY_PEPPER. Results go to stdout; logs go to
stderr, so a new key is printed exactly once and never written to a log.
"""

import argparse
from typing import TextIO

import asyncpg

from app.auth.keys import fingerprint, generate_key
from app.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-tenant", help="add a tenant")
    p.add_argument("--name", required=True, help="lowercase slug, e.g. pipelinemedic")
    p.add_argument("--rpm", type=int, required=True, help="requests per minute")
    p.add_argument("--daily-tokens", type=int, required=True, help="daily token budget")

    p = sub.add_parser("create-key", help="issue a new API key (shown once)")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True, help="label, e.g. ci or laptop")
    p.add_argument("--rpm", type=int, default=None, help="optional per-key limit")

    p = sub.add_parser("list-keys", help="list a tenant's keys (prefixes only)")
    p.add_argument("--tenant", required=True)

    p = sub.add_parser("revoke-key", help="revoke a key by its prefix")
    p.add_argument("--prefix", required=True)

    p = sub.add_parser("disable-tenant", help="block all of a tenant's keys")
    p.add_argument("--name", required=True)
    return parser


async def run(argv: list[str], settings: Settings, out: TextIO) -> int:
    """Run one admin command. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    if settings.database_url is None:
        out.write("error: TOKENGATE_DATABASE_URL is not set\n")
        return 1
    conn = await asyncpg.connect(
        settings.database_url.get_secret_value(), timeout=settings.db_connect_timeout_s
    )
    try:
        return await _dispatch(args, settings, conn, out)
    except asyncpg.UniqueViolationError:
        out.write("error: that name or key already exists\n")
        return 1
    except asyncpg.CheckViolationError as exc:
        out.write(f"error: value not allowed ({exc.constraint_name})\n")
        return 1
    finally:
        await conn.close()


async def _dispatch(
    args: argparse.Namespace, settings: Settings, conn: asyncpg.Connection, out: TextIO
) -> int:
    if args.command == "create-tenant":
        tenant_id = await conn.fetchval(
            "INSERT INTO tenants (name, requests_per_minute, daily_token_budget) "
            "VALUES ($1, $2, $3) RETURNING id",
            args.name,
            args.rpm,
            args.daily_tokens,
        )
        out.write(f"created tenant {args.name} ({tenant_id})\n")
        return 0

    if args.command == "create-key":
        if settings.api_key_pepper is None:
            out.write("error: TOKENGATE_API_KEY_PEPPER is not set\n")
            return 1
        tenant_id = await conn.fetchval("SELECT id FROM tenants WHERE name = $1", args.tenant)
        if tenant_id is None:
            out.write(f"error: no tenant named {args.tenant!r}\n")
            return 1
        key = generate_key()
        await conn.execute(
            "INSERT INTO api_keys (tenant_id, prefix, key_hash, name, requests_per_minute) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id,
            key.prefix,
            fingerprint(key.secret, settings.api_key_pepper.get_secret_value().encode()),
            args.name,
            args.rpm,
        )
        out.write(
            f"created key {key.prefix} for tenant {args.tenant}\n\n"
            f"    {key.full}\n\n"
            "Store it now: it is not saved anywhere and cannot be shown again.\n"
        )
        return 0

    if args.command == "list-keys":
        rows = await conn.fetch(
            "SELECT k.prefix, k.name, k.created_at, k.revoked_at "
            "FROM api_keys k JOIN tenants t ON t.id = k.tenant_id "
            "WHERE t.name = $1 ORDER BY k.created_at",
            args.tenant,
        )
        if not rows:
            out.write(f"no keys for tenant {args.tenant!r}\n")
        for row in rows:
            status = "revoked" if row["revoked_at"] else "active"
            out.write(f"{row['prefix']}  {status:8}  {row['name']}  {row['created_at']:%Y-%m-%d}\n")
        return 0

    if args.command == "revoke-key":
        result = await conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE prefix = $1 AND revoked_at IS NULL",
            args.prefix,
        )
        if result == "UPDATE 0":
            out.write(f"error: no active key with prefix {args.prefix!r}\n")
            return 1
        out.write(
            f"revoked key {args.prefix} (instances may accept it for up to "
            f"{settings.auth_cache_ttl_s:g} s while their cache expires)\n"
        )
        return 0

    if args.command == "disable-tenant":
        result = await conn.execute(
            "UPDATE tenants SET disabled_at = now() WHERE name = $1 AND disabled_at IS NULL",
            args.name,
        )
        if result == "UPDATE 0":
            out.write(f"error: no active tenant named {args.name!r}\n")
            return 1
        out.write(f"disabled tenant {args.name}\n")
        return 0

    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover
