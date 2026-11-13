"""Look up API keys by prefix, with a small in-memory cache (docs/DECISIONS.md, D33).

Trade-off: a cached entry is trusted for ``ttl_s`` seconds, so a revoked key (or a disabled
tenant) can keep working for up to that long on each instance. Misses are cached too, so a
flood of made-up keys can't turn into a flood of database queries. The cache has a hard size
limit (least recently used entries are dropped first), so it can't grow without bound.
"""

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class KeyRecord:
    key_id: uuid.UUID
    prefix: str
    key_hash: bytes
    key_revoked: bool
    key_requests_per_minute: int | None
    tenant_id: uuid.UUID
    tenant_name: str
    tenant_disabled: bool
    tenant_requests_per_minute: int
    daily_token_budget: int


_LOOKUP = """
    SELECT k.id                         AS key_id,
           k.prefix,
           k.key_hash,
           k.revoked_at IS NOT NULL     AS key_revoked,
           k.requests_per_minute        AS key_requests_per_minute,
           t.id                         AS tenant_id,
           t.name                       AS tenant_name,
           t.disabled_at IS NOT NULL    AS tenant_disabled,
           t.requests_per_minute        AS tenant_requests_per_minute,
           t.daily_token_budget
      FROM api_keys k
      JOIN tenants t ON t.id = k.tenant_id
     WHERE k.prefix = $1
"""


class KeyStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        ttl_s: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        # prefix -> (expires_at, record or None for "no such key")
        self._cache: OrderedDict[str, tuple[float, KeyRecord | None]] = OrderedDict()
        self.db_lookups = 0  # for tests and debugging

    async def lookup(self, prefix: str) -> KeyRecord | None:
        now = self._clock()
        cached = self._cache.get(prefix)
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(prefix)
            return cached[1]

        self.db_lookups += 1
        row = await self._pool.fetchrow(_LOOKUP, prefix)
        record = KeyRecord(**dict(row)) if row is not None else None

        if self._ttl_s > 0:
            self._cache[prefix] = (now + self._ttl_s, record)
            self._cache.move_to_end(prefix)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)  # drop the least recently used
        return record
