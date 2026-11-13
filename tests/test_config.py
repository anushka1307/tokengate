import pydantic
import pytest
from pydantic import SecretStr

from app.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKENGATE_ENV", raising=False)
    monkeypatch.delenv("TOKENGATE_LOG_LEVEL", raising=False)
    s = Settings()
    assert s.env == "local"
    assert s.log_level == "INFO"
    assert s.http_connect_timeout_s > 0


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENGATE_ENV", "prod")
    # prod requires auth (database and pepper) and rate limiting (Redis)
    monkeypatch.setenv("TOKENGATE_AUTH", "required")
    monkeypatch.setenv("TOKENGATE_REDIS_URL", "rediss://cache.example:6379")
    monkeypatch.setenv("TOKENGATE_DATABASE_URL", "postgresql://db.example/tokengate")
    monkeypatch.setenv("TOKENGATE_API_KEY_PEPPER", "p" * 32)
    monkeypatch.setenv("TOKENGATE_LOG_LEVEL", "debug")  # case-insensitive
    monkeypatch.setenv("TOKENGATE_HTTP_READ_TIMEOUT_S", "12.5")
    s = Settings()
    assert s.env == "prod"
    assert s.log_level == "DEBUG"
    assert s.http_read_timeout_s == 12.5


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("TOKENGATE_LOG_LEVEL", "LOUD"),
        ("TOKENGATE_ENV", "staging"),
        ("TOKENGATE_HTTP_CONNECT_TIMEOUT_S", "0"),  # a zero timeout means "no timeout": rejected
        ("TOKENGATE_HTTP_READ_TIMEOUT_S", "-1"),
        ("TOKENGATE_HTTP_MAX_CONNECTIONS", "lots"),
    ],
)
def test_invalid_values_fail_fast(monkeypatch: pytest.MonkeyPatch, var: str, value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_settings_are_immutable() -> None:
    s = Settings(env="test")
    with pytest.raises(pydantic.ValidationError):
        s.env = "prod"


def test_prod_requires_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENGATE_ENV", "prod")
    monkeypatch.setenv("TOKENGATE_AUTH", "required")
    monkeypatch.setenv("TOKENGATE_DATABASE_URL", "postgresql://db.example/tokengate")
    monkeypatch.setenv("TOKENGATE_API_KEY_PEPPER", "p" * 32)
    with pytest.raises(pydantic.ValidationError, match="TOKENGATE_REDIS_URL is required"):
        Settings()


def test_redis_url_scheme_is_checked_without_echoing_it() -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        Settings(env="test", redis_url=SecretStr("http://user:hunter2@cache.example"))
    assert "must start with redis://" in str(info.value)
    assert "hunter2" not in str(info.value)
