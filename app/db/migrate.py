"""Apply SQL migrations in order (docs/DECISIONS.md, D29).

    uv run python -m app.db.migrate          # uses TOKENGATE_DATABASE_URL

Rules:
- Files are ``NNNN_snake_case.sql`` in ``app/db/migrations/``, numbered 1, 2, 3... with no gaps.
- Each file runs in its own transaction, together with its row in ``schema_migrations``,
  so a failed migration leaves nothing half-applied.
- An applied file must never change: its SHA-256 is recorded, and a mismatch stops everything.
  Fix mistakes with a new migration.
- A Postgres advisory lock means two instances starting at once can't both migrate.
"""

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import structlog

from app.config import Settings
from app.telemetry.logging import configure_logging

log = structlog.get_logger("tokengate.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
# Any fixed 64-bit number works; this one is "tokengat" in ASCII.
_ADVISORY_LOCK_KEY = 0x746F6B656E676174


class MigrationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"bad migration filename: {path.name} (want NNNN_name.sql)")
        migrations.append(Migration(int(match[1]), match[2], path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migration versions must be 1..N with no gaps, got {versions}")
    return migrations


async def migrate(conn: asyncpg.Connection, migrations: list[Migration]) -> list[int]:
    """Apply pending migrations. Returns the versions applied by this call."""
    await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    integer     PRIMARY KEY,
                name       text        NOT NULL,
                checksum   text        NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        applied = {row["version"]: row["checksum"] for row in rows}

        unknown = set(applied) - {m.version for m in migrations}
        if unknown:
            raise MigrationError(
                f"database has migrations this code doesn't know: {sorted(unknown)}"
            )

        newly_applied = []
        for migration in migrations:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} was changed after "
                        "it was applied; add a new migration instead"
                    )
                continue
            async with conn.transaction():
                await conn.execute(migration.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                    migration.version,
                    migration.name,
                    migration.checksum,
                )
            log.info("migration_applied", version=migration.version, name=migration.name)
            newly_applied.append(migration.version)
        return newly_applied
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)


async def _main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    if settings.database_url is None:
        log.error("migrate_failed", reason="TOKENGATE_DATABASE_URL is not set")
        return 1
    conn = await asyncpg.connect(
        settings.database_url.get_secret_value(), timeout=settings.db_connect_timeout_s
    )
    try:
        applied = await migrate(conn, load_migrations())
    except MigrationError as exc:
        log.error("migrate_failed", reason=str(exc))
        return 1
    finally:
        await conn.close()
    log.info("migrate_complete", applied=applied)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
