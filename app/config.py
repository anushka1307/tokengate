"""Typed settings, read only from environment variables (12-factor).

Every variable is prefixed with ``TOKENGATE_``. Invalid values fail at startup, never mid-request.
"""

import re
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

_ALIAS = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TARGET = re.compile(r"[a-z0-9_-]{1,32}/[A-Za-z0-9._:-]{1,128}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOKENGATE_", frozen=True, extra="ignore")

    env: Literal["local", "test", "prod"] = "local"
    log_level: LogLevel = "INFO"

    # Shared outbound HTTP client (used by provider adapters). Every network call has a timeout.
    http_connect_timeout_s: float = Field(default=2.0, gt=0, le=30)
    http_read_timeout_s: float = Field(default=30.0, gt=0, le=300)
    http_write_timeout_s: float = Field(default=5.0, gt=0, le=60)
    http_pool_timeout_s: float = Field(default=2.0, gt=0, le=30)
    http_max_connections: int = Field(default=100, gt=0, le=10_000)
    http_max_keepalive_connections: int = Field(default=20, gt=0, le=10_000)

    # Model aliases clients may request, each mapped to "<provider>/<provider's model>" (D16).
    # Only these aliases are accepted. Env format is JSON, e.g.
    # TOKENGATE_MODEL_ROUTES='{"mock-1": "mock/echo-v1", "fast": "gemini/gemini-2.0-flash"}'
    model_routes: dict[str, str] = Field(
        default_factory=lambda: {"mock-1": "mock/echo-v1"}, min_length=1
    )

    # MockProvider behaviour (local dev, tests, load and chaos tests).
    mock_first_token_delay_ms: float = Field(default=0.0, ge=0, le=60_000)
    mock_per_token_delay_ms: float = Field(default=0.0, ge=0, le=10_000)
    mock_error_rate: float = Field(default=0.0, ge=0, le=1)
    mock_mid_stream_error_rate: float = Field(default=0.0, ge=0, le=1)
    mock_seed: int | None = None

    # Postgres (tenants and API keys). Optional until auth lands in step 2.2. A SecretStr so a
    # password in the URL never appears in logs or reprs. e.g. postgresql://user:pw@host/db
    database_url: SecretStr | None = None
    # Small pool: Cloud Run runs a few instances, and free Postgres tiers cap connections.
    # 0 at startup means a database outage doesn't stop the app from booting; /readyz reports
    # it instead (D31).
    db_pool_min_size: int = Field(default=0, ge=0, le=50)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)
    db_connect_timeout_s: float = Field(default=3.0, gt=0, le=30)
    # Client-side limit per query, plus the same limit enforced by Postgres itself.
    db_query_timeout_s: float = Field(default=2.0, gt=0, le=30)

    # API-key authentication on /v1/chat/completions (D32-D35). "disabled" exists only for
    # local development and tests and is refused in prod.
    auth: Literal["required", "disabled"] = "required"
    # Server-side secret mixed into every key fingerprint. Without it a stolen database
    # can't be used to test key guesses. Changing it invalidates all existing keys.
    api_key_pepper: SecretStr | None = Field(default=None, min_length=32)
    # How long a key lookup is trusted. Also the longest a revoked key can keep working.
    auth_cache_ttl_s: float = Field(default=60.0, ge=0, le=600)
    auth_cache_max_entries: int = Field(default=10_000, ge=1, le=1_000_000)

    # /metrics needs "Authorization: Bearer <token>" when set. With no token, /metrics is open
    # in local/test and disabled (404) in prod (D21). SecretStr keeps it out of logs and reprs.
    metrics_token: SecretStr | None = Field(default=None, min_length=16)

    # uvicorn's graceful-shutdown limit. Cloud Run sends SIGTERM and kills the container
    # 10 s later, so this must stay below 10 there (D22).
    graceful_shutdown_timeout_s: float = Field(default=8.0, gt=0, le=60)

    # Request limits (D11-D14).
    max_body_bytes: int = Field(default=1_048_576, gt=0, le=10 * 1_048_576)
    max_messages: int = Field(default=256, gt=0, le=10_000)
    max_input_chars: int = Field(default=200_000, gt=0, le=5_000_000)
    max_completion_tokens: int = Field(default=4_096, gt=0, le=200_000)

    @field_validator("model_routes")
    @classmethod
    def _check_routes(cls, routes: dict[str, str]) -> dict[str, str]:
        for alias, target in routes.items():
            if not _ALIAS.fullmatch(alias):
                raise ValueError(f"invalid model alias {alias!r}")
            if not _TARGET.fullmatch(target):
                raise ValueError(f"route for {alias!r} must look like 'provider/model'")
        return routes

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, url: SecretStr | None) -> SecretStr | None:
        if url is not None and not url.get_secret_value().startswith(
            ("postgresql://", "postgres://")
        ):
            # Don't echo the value: it may contain a password.
            raise ValueError("database_url must start with postgresql:// or postgres://")
        return url

    @model_validator(mode="after")
    def _check_pool_sizes(self) -> Self:
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("db_pool_min_size must not exceed db_pool_max_size")
        return self

    @model_validator(mode="after")
    def _check_auth(self) -> Self:
        if self.env == "prod" and self.auth != "required":
            raise ValueError("auth cannot be disabled in prod")
        if self.auth == "required":
            missing = [
                name
                for name, value in (
                    ("TOKENGATE_DATABASE_URL", self.database_url),
                    ("TOKENGATE_API_KEY_PEPPER", self.api_key_pepper),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    f"auth is required but {', '.join(missing)} is not set "
                    "(set TOKENGATE_AUTH=disabled for local development without a database)"
                )
        return self

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
