import pydantic
import pytest
from asgi_lifespan import LifespanManager

from app.api.errors import APIError
from app.config import Settings
from app.main import create_app
from app.providers.mock import MockProvider
from app.providers.router import ModelRouter


def test_resolves_alias_to_provider_and_target_model() -> None:
    mock = MockProvider()
    router = ModelRouter({"fast": "mock/echo-v1"}, {"mock": mock})
    provider, route = router.resolve("fast")
    assert provider is mock
    assert (route.alias, route.provider_name, route.target_model) == ("fast", "mock", "echo-v1")
    assert router.aliases == ("fast",)


def test_unknown_alias_is_a_400() -> None:
    router = ModelRouter({"fast": "mock/echo-v1"}, {"mock": MockProvider()})
    with pytest.raises(APIError) as info:
        router.resolve("slow")
    assert info.value.status_code == 400
    assert info.value.code == "model_not_found"


def test_route_to_unknown_provider_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown provider 'gemini'"):
        ModelRouter({"fast": "gemini/gemini-2.0-flash"}, {"mock": MockProvider()})


async def test_app_refuses_to_start_with_a_broken_route() -> None:
    app = create_app(Settings(env="test", model_routes={"fast": "gemini/gemini-2.0-flash"}))
    with pytest.raises(ValueError, match="unknown provider"):
        async with LifespanManager(app):
            pass


def test_routes_from_env_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENGATE_MODEL_ROUTES", '{"mock-1": "mock/echo-v1", "fast": "mock/v2"}')
    assert Settings().model_routes == {"mock-1": "mock/echo-v1", "fast": "mock/v2"}


@pytest.mark.parametrize(
    "routes",
    [
        {},  # at least one route
        {"fast": "no-slash"},
        {"fast": "Mock/echo"},  # provider names are lowercase
        {"has space": "mock/echo"},
        {"fast": "mock/"},
    ],
)
def test_invalid_routes_rejected_at_config_load(routes: dict[str, str]) -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(env="test", model_routes=routes)
