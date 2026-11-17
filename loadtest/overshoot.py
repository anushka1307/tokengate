"""Measure how far several gateway instances overshoot rate limits and daily budgets (D45, D46).

    createdb tokengate_bench   # once
    uv run python -m loadtest.overshoot --instances 2 4 --repeat 5

Starts N real gateway processes (``python -m app``, prod mode, MockProvider) on 127.0.0.1, all
sharing one local Redis and Postgres, then fires concurrent requests spread round-robin across
them. Two modes:

- ``shared``: the real setup. Every instance uses the same Redis keys.
- ``naive``: the baseline. Each instance gets its own Redis key prefix, so it keeps its own
  counters, which is what per-instance (in-memory) limiting would do.

Scenarios:

- ``ratelimit``: a burst well above the tenant's requests-per-minute. Overshoot = admitted
  requests above the most a token bucket can allow over the burst window,
  ``floor(capacity + rate * elapsed)``, where ``elapsed`` is measured by this client from the
  first request sent to the last response received. That window is at least as long as the
  server's, so the ceiling is generous and the measured overshoot can only err low by a
  fraction of one request.
- ``budget-text`` / ``budget-short-words``: concurrent requests against a small daily budget.
  Overshoot = real tokens used (sum of ``usage.total_tokens`` the clients saw) minus the budget.
  ``short-words`` uses a prompt of one-letter words, which the gateway's original
  4-chars-per-token input estimate undercounted (MockProvider counts words): +49% in step 2.5.
  It stays as a regression check for the current estimate (D47).

Prints one JSON line per run, then a markdown summary with the worst case across repeats.
Refuses non-local Redis or Postgres URLs: this sends thousands of commands per run, which would
eat a free-tier quota (Upstash: 500K commands a month).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import math
import os
import platform
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import asyncpg
import httpx
import redis.asyncio as redis
from pydantic import SecretStr

from app.admin.cli import run as admin_run
from app.config import Settings
from app.db.migrate import load_migrations, migrate

Mode = Literal["shared", "naive"]
Scenario = Literal["ratelimit", "budget-text", "budget-short-words"]
SCENARIOS: tuple[Scenario, ...] = ("ratelimit", "budget-text", "budget-short-words")
MODES: tuple[Mode, ...] = ("shared", "naive")

DEFAULT_DATABASE_URL = "postgresql://localhost:5432/tokengate_bench"
DEFAULT_REDIS_URL = "redis://localhost:6379/14"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_KEY_PATTERN = re.compile(r"tg_[A-Za-z0-9]{12}_[A-Za-z0-9]{43}")
_REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=2.0)
_READY_TIMEOUT_S = 30.0
_STOP_TIMEOUT_S = 10.0

# ~45 words / ~270 characters: the 4-chars-per-token estimate (68) is above the word count.
TEXT_PROMPT = (
    "Summarize the incident report below in two sentences for the on-call channel. The "
    "nightly ingestion job failed after the upstream vendor changed a column name, so the "
    "dashboard showed stale numbers until the morning team noticed, patched the mapping, and "
    "replayed the missing partitions."
)
# 200 one-letter words / 399 characters: estimated at 100 tokens, counted as 200.
SHORT_WORDS_PROMPT = " ".join(["a"] * 200)


# ---------- pure helpers (unit-tested) ----------


def require_local(url: str, what: str) -> None:
    """Refuse anything but a plaintext connection to this machine (no cloud quota, no TLS)."""
    parts = urlsplit(url)
    if parts.scheme in {"rediss"} or parts.hostname not in LOCAL_HOSTS:
        raise SystemExit(
            f"refusing to benchmark against a non-local {what} (host {parts.hostname!r}): "
            "this sends thousands of commands per run and would use a free-tier quota"
        )


def bucket_ceiling(capacity: int, per_second: float, elapsed_s: float) -> int:
    """The most requests a full token bucket can admit within ``elapsed_s``."""
    return math.floor(capacity + per_second * max(elapsed_s, 0.0))


def overshoot_pct(observed: int, limit: int) -> float:
    return round(100.0 * max(observed - limit, 0) / limit, 2)


@dataclass(frozen=True, slots=True)
class RunResult:
    scenario: Scenario
    mode: Mode
    instances: int
    repeat: int
    requests: int
    concurrency: int
    limit: int  # allowed requests (ratelimit) or the daily budget in tokens (budget)
    observed: int  # admitted requests (ratelimit) or real tokens used (budget)
    charged: int | None  # tokens on the gateway's counters after settling (budget only)
    admitted: int
    refused: int
    errors: int
    elapsed_s: float

    @property
    def overshoot(self) -> int:
        return max(self.observed - self.limit, 0)

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["overshoot"] = self.overshoot
        data["overshoot_pct"] = overshoot_pct(self.observed, self.limit)
        return data


def worst_cases(results: Sequence[RunResult]) -> list[RunResult]:
    """The run with the highest overshoot % for each (scenario, mode, instances)."""
    worst: dict[tuple[str, str, int], RunResult] = {}
    for r in results:
        k = (r.scenario, r.mode, r.instances)
        best = worst.get(k)
        if best is None or overshoot_pct(r.observed, r.limit) > overshoot_pct(
            best.observed, best.limit
        ):
            worst[k] = r
    return [worst[k] for k in sorted(worst, key=lambda k: (SCENARIOS.index(k[0]), k[1], k[2]))]


def markdown_summary(results: Sequence[RunResult], repeats: int) -> str:
    lines = [
        f"Worst case across {repeats} run(s) per row:",
        "",
        "| Scenario | Mode | Instances | Limit | Observed | Overshoot | Errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in worst_cases(results):
        unit = "req" if r.scenario == "ratelimit" else "tokens"
        lines.append(
            f"| {r.scenario} | {r.mode} | {r.instances} | {r.limit} {unit} | {r.observed} "
            f"| {r.overshoot} ({overshoot_pct(r.observed, r.limit)}%) | {r.errors} |"
        )
    return "\n".join(lines)


# ---------- environment: database, tenants, keys ----------


@dataclass(frozen=True, slots=True)
class Bench:
    database_url: str
    redis_url: str
    pepper: str
    run_id: str  # lowercase hex, used in tenant names and Redis prefixes


async def prepare_database(database_url: str) -> None:
    conn = await asyncpg.connect(database_url, timeout=5)
    try:
        await migrate(conn, load_migrations())
    finally:
        await conn.close()


async def issue_key(bench: Bench, tenant: str, rpm: int, daily_tokens: int) -> str:
    """Create a tenant and one key through the real admin CLI. Returns the key."""
    settings = Settings(
        env="test",
        auth="required",
        database_url=SecretStr(bench.database_url),
        api_key_pepper=SecretStr(bench.pepper),
    )
    for argv in (
        ["create-tenant", "--name", tenant, "--rpm", str(rpm), "--daily-tokens", str(daily_tokens)],
        ["create-key", "--tenant", tenant, "--name", "bench"],
    ):
        out = io.StringIO()
        if await admin_run(argv, settings, out) != 0:
            raise RuntimeError(f"admin {argv[0]} failed: {out.getvalue().strip()}")
    match = _KEY_PATTERN.search(out.getvalue())
    if match is None:  # pragma: no cover - the CLI always prints the key
        raise RuntimeError("admin create-key printed no key")
    return match.group(0)


def instance_prefixes(run_tag: str, mode: Mode, instances: int) -> list[str]:
    """One prefix for all instances (shared), or one per instance (naive). Max 32 chars."""
    base = f"bench:{run_tag}"
    if mode == "shared":
        return [f"{base}:"] * instances
    return [f"{base}:{i}:" for i in range(instances)]


async def delete_prefixes(redis_url: str, prefixes: Sequence[str]) -> None:
    client = redis.Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
    try:
        for prefix in set(prefixes):
            keys = [k async for k in client.scan_iter(match=f"{prefix}*")]
            if keys:
                await client.delete(*keys)
    finally:
        await client.aclose()


async def charged_tokens(redis_url: str, prefixes: Sequence[str]) -> int:
    """Sum of every budget counter under the given prefixes."""
    client = redis.Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
    try:
        total = 0
        for prefix in set(prefixes):
            async for key in client.scan_iter(match=f"{prefix}budget:*"):
                value = await client.get(key)
                total += int(value or 0)
        return total
    finally:
        await client.aclose()


# ---------- gateway processes ----------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@dataclass(slots=True)
class Instance:
    process: asyncio.subprocess.Process
    port: int
    log_path: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def instance_env(bench: Bench, port: int, key_prefix: str, mock_delay_ms: float) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOKENGATE_")}
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "TOKENGATE_ENV": "prod",
            "TOKENGATE_LOG_LEVEL": "WARNING",
            "TOKENGATE_DATABASE_URL": bench.database_url,
            "TOKENGATE_API_KEY_PEPPER": bench.pepper,
            "TOKENGATE_REDIS_URL": bench.redis_url,
            "TOKENGATE_REDIS_KEY_PREFIX": key_prefix,
            "TOKENGATE_MOCK_FIRST_TOKEN_DELAY_MS": str(mock_delay_ms),
            "TOKENGATE_MOCK_SEED": "1",
        }
    )
    return env


async def _wait_ready(instance: Instance, client: httpx.AsyncClient) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if instance.process.returncode is not None:
            break
        with contextlib.suppress(httpx.HTTPError):
            if (await client.get(f"{instance.base_url}/readyz")).status_code == 200:
                return
        await asyncio.sleep(0.1)
    tail = instance.log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
    raise RuntimeError(f"instance on port {instance.port} never became ready:\n{tail}")


async def _stop(instance: Instance) -> None:
    if instance.process.returncode is not None:
        return
    instance.process.send_signal(signal.SIGTERM)  # graceful shutdown, like Cloud Run
    try:
        await asyncio.wait_for(instance.process.wait(), _STOP_TIMEOUT_S)
    except TimeoutError:
        instance.process.kill()
        await instance.process.wait()


@contextlib.asynccontextmanager
async def gateways(
    bench: Bench, prefixes: Sequence[str], mock_delay_ms: float, log_dir: Path
) -> AsyncIterator[list[Instance]]:
    started: list[Instance] = []
    try:
        for i, prefix in enumerate(prefixes):
            port = free_port()
            log_path = log_dir / f"{prefix.replace(':', '_')}{i}.log"
            with log_path.open("wb") as log_file:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app",
                    env=instance_env(bench, port, prefix, mock_delay_ms),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
            started.append(Instance(process, port, log_path))
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            await asyncio.gather(*(_wait_ready(inst, client) for inst in started))
        yield started
    finally:
        await asyncio.gather(*(_stop(inst) for inst in started))


# ---------- load ----------


@dataclass(frozen=True, slots=True)
class Outcome:
    admitted: int
    refused: int
    errors: int
    tokens: int  # sum of usage.total_tokens over admitted responses
    elapsed_s: float


async def fire(
    instances: Sequence[Instance],
    api_key: str,
    *,
    prompt: str,
    max_tokens: int,
    requests: int,
    concurrency: int,
) -> Outcome:
    """Send ``requests`` chat calls round-robin across instances, ``concurrency`` at a time."""
    body = {
        "model": "mock-1",
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    semaphore = asyncio.Semaphore(concurrency)
    statuses: list[int] = []
    tokens = 0
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, limits=limits) as client:

        async def one(i: int) -> None:
            nonlocal tokens
            url = f"{instances[i % len(instances)].base_url}/v1/chat/completions"
            async with semaphore:
                try:
                    response = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError:
                    statuses.append(0)
                    return
            statuses.append(response.status_code)
            if response.status_code == 200:
                tokens += int(response.json()["usage"]["total_tokens"])

        start = time.monotonic()
        await asyncio.gather(*(one(i) for i in range(requests)))
        elapsed = time.monotonic() - start

    admitted = statuses.count(200)
    refused = statuses.count(429)
    return Outcome(admitted, refused, len(statuses) - admitted - refused, tokens, elapsed)


@dataclass(frozen=True, slots=True)
class Params:
    rpm: int
    budget: int
    max_tokens: int
    requests: int
    concurrency: int
    mock_delay_ms: float


async def run_once(
    bench: Bench, scenario: Scenario, mode: Mode, instances: int, repeat: int, p: Params
) -> RunResult:
    tag = f"{scenario}-{mode}-{instances}-{repeat}"
    tenant = f"bench-{bench.run_id}-{tag}"
    prefixes = instance_prefixes(secrets.token_hex(4), mode, instances)
    is_rate = scenario == "ratelimit"
    # Each scenario gets a tenant where only the limit under test can bind.
    key = await issue_key(
        bench,
        tenant,
        rpm=p.rpm if is_rate else 1_000_000,
        daily_tokens=1_000_000_000 if is_rate else p.budget,
    )
    prompt = SHORT_WORDS_PROMPT if scenario == "budget-short-words" else TEXT_PROMPT
    try:
        with tempfile.TemporaryDirectory(prefix="tg-bench-") as log_dir:
            async with gateways(bench, prefixes, p.mock_delay_ms, Path(log_dir)) as running:
                outcome = await fire(
                    running,
                    key,
                    prompt=prompt,
                    max_tokens=p.max_tokens,
                    requests=p.requests,
                    concurrency=p.concurrency,
                )
            charged = None if is_rate else await charged_tokens(bench.redis_url, prefixes)
    finally:
        await delete_prefixes(bench.redis_url, prefixes)

    if is_rate:
        limit = bucket_ceiling(p.rpm, p.rpm / 60, outcome.elapsed_s)
        observed = outcome.admitted
    else:
        limit, observed = p.budget, outcome.tokens
    return RunResult(
        scenario=scenario,
        mode=mode,
        instances=instances,
        repeat=repeat,
        requests=p.requests,
        concurrency=p.concurrency,
        limit=limit,
        observed=observed,
        charged=charged,
        admitted=outcome.admitted,
        refused=outcome.refused,
        errors=outcome.errors,
        elapsed_s=round(outcome.elapsed_s, 3),
    )


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty"],  # noqa: S607 - fixed command, no input
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m loadtest.overshoot",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--instances", type=int, nargs="+", default=[2, 4])
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--rpm", type=int, default=60, help="tenant limit in the ratelimit run")
    parser.add_argument("--budget", type=int, default=3000, help="daily tokens in budget runs")
    parser.add_argument("--max-tokens", type=int, default=50, help="output limit per request")
    parser.add_argument("--requests", type=int, default=240)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--mock-delay-ms", type=float, default=20.0, help="time to first token")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = os.environ.get("TOKENGATE_BENCH_DATABASE_URL", DEFAULT_DATABASE_URL)
    redis_url = os.environ.get("TOKENGATE_BENCH_REDIS_URL", DEFAULT_REDIS_URL)
    require_local(database_url, "Postgres")
    require_local(redis_url, "Redis")
    if any(n < 1 for n in args.instances) or args.repeat < 1:
        raise SystemExit("--instances and --repeat must be at least 1")

    bench = Bench(database_url, redis_url, secrets.token_urlsafe(32), secrets.token_hex(4))
    params = Params(
        args.rpm, args.budget, args.max_tokens, args.requests, args.concurrency, args.mock_delay_ms
    )
    await prepare_database(database_url)
    meta = {
        "commit": _git_commit(),
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs",
        "python": platform.python_version(),
        "params": asdict(params),
    }
    print(json.dumps({"meta": meta}), flush=True)

    results: list[RunResult] = []
    for instances in args.instances:
        for scenario in args.scenarios:
            for mode in args.modes:
                for repeat in range(1, args.repeat + 1):
                    result = await run_once(bench, scenario, mode, instances, repeat, params)
                    results.append(result)
                    print(json.dumps(result.to_json()), flush=True)

    print()
    print(markdown_summary(results, args.repeat))
    errors = sum(r.errors for r in results)
    if errors:
        print(f"\n{errors} request(s) failed with neither 200 nor 429: see the JSON lines")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
