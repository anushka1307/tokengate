"""The overshoot benchmark (loadtest/overshoot.py): its maths, its safety guard, and one small
end-to-end run with real gateway processes against the test Postgres and Redis."""

import secrets

import pytest

from loadtest.overshoot import (
    SHORT_WORDS_PROMPT,
    TEXT_PROMPT,
    Bench,
    Params,
    RunResult,
    bucket_ceiling,
    instance_prefixes,
    markdown_summary,
    overshoot_pct,
    prepare_database,
    require_local,
    run_once,
    worst_cases,
)


def _result(observed: int, *, repeat: int = 1, limit: int = 100) -> RunResult:
    return RunResult(
        scenario="ratelimit",
        mode="shared",
        instances=2,
        repeat=repeat,
        requests=200,
        concurrency=50,
        limit=limit,
        observed=observed,
        charged=None,
        admitted=observed,
        refused=200 - observed,
        errors=0,
        elapsed_s=1.0,
    )


def test_bucket_ceiling_adds_refill_over_the_window() -> None:
    assert bucket_ceiling(60, 1.0, 0.0) == 60
    assert bucket_ceiling(60, 1.0, 0.99) == 60  # partial tokens can't admit a request
    assert bucket_ceiling(60, 1.0, 2.5) == 62
    assert bucket_ceiling(60, 1.0, -1.0) == 60


def test_overshoot_is_never_negative() -> None:
    assert overshoot_pct(50, 100) == 0.0
    assert overshoot_pct(150, 100) == 50.0
    assert _result(90).overshoot == 0
    assert _result(130).to_json()["overshoot"] == 30


def test_worst_case_keeps_the_highest_overshoot() -> None:
    results = [_result(100, repeat=1), _result(104, repeat=2), _result(101, repeat=3)]
    [worst] = worst_cases(results)
    assert worst.repeat == 2
    assert "| ratelimit | shared | 2 | 100 req | 104 | 4 (4.0%) | 0 |" in markdown_summary(
        results, 3
    )


def test_naive_mode_gives_each_instance_its_own_counters() -> None:
    assert instance_prefixes("abcd1234", "shared", 3) == ["bench:abcd1234:"] * 3
    naive = instance_prefixes("abcd1234", "naive", 3)
    assert len(set(naive)) == 3
    # Must pass the gateway's own prefix rule (^[a-z0-9:_-]{1,32}$).
    assert all(len(p) <= 32 for p in naive)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://localhost:5432/tokengate_bench",
        "postgresql://user:pw@127.0.0.1/db",
        "redis://localhost:6379/14",
        "redis://[::1]:6379/0",
    ],
)
def test_local_urls_are_allowed(url: str) -> None:
    require_local(url, "store")


@pytest.mark.parametrize(
    "url",
    [
        "rediss://default:pw@eu1-example.upstash.io:6379",
        "redis://cache.example.com:6379/0",
        "postgresql://user:pw@db.example.supabase.co/postgres",
        "rediss://localhost:6379/0",  # TLS means a managed service, even when tunnelled
    ],
)
def test_remote_urls_are_refused(url: str) -> None:
    with pytest.raises(SystemExit, match="non-local"):
        require_local(url, "store")


def test_prompts_have_the_intended_estimate_error() -> None:
    # The gateway estimates ceil(chars / 4); MockProvider counts words.
    assert -(-len(TEXT_PROMPT) // 4) > len(TEXT_PROMPT.split())
    assert -(-len(SHORT_WORDS_PROMPT) // 4) < len(SHORT_WORDS_PROMPT.split())


async def test_end_to_end_shared_counters_hold_and_naive_ones_do_not(
    pg: str, redis_url: str
) -> None:
    """Two real gateway processes. Shared Redis keys: no overshoot. Separate keys: overshoot."""
    await prepare_database(pg)
    bench = Bench(pg, redis_url, secrets.token_urlsafe(32), secrets.token_hex(4))
    params = Params(
        rpm=10, budget=1000, max_tokens=50, requests=40, concurrency=20, mock_delay_ms=10
    )

    shared = await run_once(bench, "ratelimit", "shared", 2, 1, params)
    assert shared.errors == 0
    assert shared.admitted >= 10
    assert shared.overshoot == 0

    naive = await run_once(bench, "ratelimit", "naive", 2, 1, params)
    assert naive.errors == 0
    assert naive.overshoot > 0  # each instance admits its own 10

    budget = await run_once(bench, "budget-text", "shared", 2, 1, params)
    assert budget.errors == 0
    assert budget.admitted > 0
    assert budget.observed <= budget.limit
    assert budget.charged == budget.observed  # settled to exactly what the clients saw
