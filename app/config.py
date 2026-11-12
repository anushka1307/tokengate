"""Typed settings, read only from environment variables (12-factor).

Every variable is prefixed with ``TOKENGATE_``. Invalid values fail at startup, never mid-request.
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


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

    # Request limits (docs/DECISIONS.md D11-D14). Clients send model *aliases*; only these are
    # accepted. Env format is JSON, e.g. TOKENGATE_ALLOWED_MODELS='["mock-1","fast"]'.
    allowed_models: tuple[str, ...] = Field(default=("mock-1",), min_length=1)
    max_body_bytes: int = Field(default=1_048_576, gt=0, le=10 * 1_048_576)
    max_messages: int = Field(default=256, gt=0, le=10_000)
    max_input_chars: int = Field(default=200_000, gt=0, le=5_000_000)
    max_completion_tokens: int = Field(default=4_096, gt=0, le=200_000)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
