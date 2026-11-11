import asyncio
import re

import httpx
import pytest
from fastapi import FastAPI

from app.telemetry.middleware import (
    OVERHEAD_HEADER,
    REQUEST_ID_HEADER,
    RequestTiming,
    track_provider_wait,
)

GENERATED_ID = re.compile(r"[0-9a-f]{32}")


async def test_generates_request_id_when_absent(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert GENERATED_ID.fullmatch(resp.headers[REQUEST_ID_HEADER])


async def test_ids_are_unique_per_request(client: httpx.AsyncClient) -> None:
    ids = {(await client.get("/healthz")).headers[REQUEST_ID_HEADER] for _ in range(5)}
    assert len(ids) == 5


async def test_propagates_valid_incoming_request_id(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "trace-abc-123"


@pytest.mark.parametrize(
    "bad_id",
    [
        "has space",
        "a" * 65,  # too long
        "<script>alert(1)</script>",
        '{"level":"error"}',  # attempt to forge JSON log fields
        "id;drop",
    ],
)
async def test_replaces_untrusted_request_id(client: httpx.AsyncClient, bad_id: str) -> None:
    resp = await client.get("/healthz", headers={REQUEST_ID_HEADER: bad_id})
    assert resp.headers[REQUEST_ID_HEADER] != bad_id
    assert GENERATED_ID.fullmatch(resp.headers[REQUEST_ID_HEADER])


async def test_overhead_header_is_a_non_negative_number(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert float(resp.headers[OVERHEAD_HEADER]) >= 0


async def test_error_responses_also_get_headers(client: httpx.AsyncClient) -> None:
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
    assert REQUEST_ID_HEADER in resp.headers
    assert OVERHEAD_HEADER in resp.headers


async def test_provider_wait_is_excluded_from_overhead(app: FastAPI) -> None:
    provider_delay_s = 0.2

    @app.get("/fake-provider-call")
    async def fake_provider_call() -> dict[str, str]:
        with track_provider_wait():
            await asyncio.sleep(provider_delay_s)
        return {"ok": "yes"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        resp = await c.get("/fake-provider-call")

    overhead_ms = float(resp.headers[OVERHEAD_HEADER])
    # Total time is >= 200 ms; overhead must not include the simulated provider wait.
    # The bound is loose on purpose so slow CI machines don't make this flaky.
    assert overhead_ms < provider_delay_s * 1000 / 2


def test_track_provider_wait_outside_a_request_is_a_no_op() -> None:
    with track_provider_wait():
        pass


def test_request_timing_math() -> None:
    t = RequestTiming(start=10.0, provider_wait_s=0.25)
    assert t.total_ms(now=10.3) == pytest.approx(300.0)
    assert t.overhead_ms(now=10.3) == pytest.approx(50.0)
    # Never negative, even if clocks or bookkeeping disagree.
    assert RequestTiming(start=10.0, provider_wait_s=5.0).overhead_ms(now=10.1) == 0.0
