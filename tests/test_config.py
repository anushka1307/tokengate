import pydantic
import pytest

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
    # prod requires auth, which requires a database and a pepper
    monkeypatch.setenv("TOKENGATE_AUTH", "required")
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
