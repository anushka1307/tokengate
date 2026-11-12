"""Liveness and readiness probes (see docs/DECISIONS.md, D9).

- ``/healthz``: the process is up and the event loop answers. Never checks dependencies, so a
  Redis blip doesn't get every instance restarted at once.
- ``/readyz``: this instance should receive traffic. False before startup completes, once
  shutdown begins, and while Postgres (when configured) can't answer ``SELECT 1`` (D31).
  The response never says *which* dependency failed; that detail goes to the logs.
"""

from fastapi import APIRouter, Request, Response, status

from app.db.pool import is_healthy
from app.state import get_state

# Readiness probes run every few seconds; keep each database check short.
_DB_CHECK_TIMEOUT_S = 1.0

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    state = get_state(request.app)
    ready = state.ready
    if ready and state.db_pool is not None:
        ready = await is_healthy(state.db_pool, _DB_CHECK_TIMEOUT_S)
    if ready:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready"}
