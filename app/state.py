"""Process-wide state owned by the app lifespan, stored on ``app.state.tg``."""

from dataclasses import dataclass

import httpx
from starlette.applications import Starlette

from app.config import Settings


@dataclass(slots=True)
class AppState:
    settings: Settings
    http_client: httpx.AsyncClient | None = None
    ready: bool = False


def get_state(app: Starlette) -> AppState:
    state = app.state.tg
    if not isinstance(state, AppState):  # pragma: no cover - programming error
        raise TypeError("app.state.tg is not an AppState; was the app built with create_app()?")
    return state
