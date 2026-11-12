"""Process-wide state owned by the app lifespan, stored on ``app.state.tg``."""

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx
from starlette.applications import Starlette

from app.config import Settings
from app.providers.base import Provider
from app.providers.router import ModelRouter


@dataclass(slots=True)
class AppState:
    settings: Settings
    # Tests can inject providers; otherwise they are built from settings at startup.
    provider_overrides: Mapping[str, Provider] | None = None
    http_client: httpx.AsyncClient | None = None
    providers: dict[str, Provider] = field(default_factory=dict)
    model_router: ModelRouter | None = None
    ready: bool = False


def get_state(app: Starlette) -> AppState:
    state = app.state.tg
    if not isinstance(state, AppState):  # pragma: no cover - programming error
        raise TypeError("app.state.tg is not an AppState; was the app built with create_app()?")
    return state
