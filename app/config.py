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

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
