"""Maps the model alias a client asks for to a provider and that provider's model (D16).

Routes come from config, e.g. ``{"mock-1": "mock/echo-v1", "fast": "gemini/gemini-2.0-flash"}``.
Changing what ``fast`` means is a config change; no client changes code.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.api.errors import APIError
from app.providers.base import Provider


@dataclass(frozen=True, slots=True)
class Route:
    alias: str
    provider_name: str
    target_model: str


class ModelRouter:
    def __init__(self, routes: Mapping[str, str], providers: Mapping[str, Provider]) -> None:
        self._providers = dict(providers)
        self._routes: dict[str, Route] = {}
        for alias, target in routes.items():
            provider_name, _, target_model = target.partition("/")
            if provider_name not in self._providers:
                # Fail at startup, not on the first request that uses this alias.
                raise ValueError(
                    f"route '{alias}' points at unknown provider '{provider_name}'; "
                    f"configured providers: {sorted(self._providers)}"
                )
            self._routes[alias] = Route(alias, provider_name, target_model)

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self._routes)

    def resolve(self, alias: str) -> tuple[Provider, Route]:
        route = self._routes.get(alias)
        if route is None:
            raise APIError(
                400,
                "The requested model is not available. Allowed: " + ", ".join(self._routes),
                code="model_not_found",
                param="model",
            )
        return self._providers[route.provider_name], route
