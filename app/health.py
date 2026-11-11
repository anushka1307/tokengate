"""Liveness and readiness probes (see docs/DECISIONS.md, D9).

- ``/healthz``: the process is up and the event loop answers. Never checks dependencies, so a
  Redis blip doesn't get every instance restarted at once.
- ``/readyz``: this instance should receive traffic. False before startup completes and once
  shutdown begins. Dependency checks (Redis, Postgres) are added here in Phase 2.
"""

from fastapi import APIRouter, Request, Response, status

from app.state import get_state

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    if get_state(request.app).ready:
        return {"status": "ready"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready"}
