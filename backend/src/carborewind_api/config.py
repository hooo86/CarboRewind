"""Application settings, loaded from environment variables only (never from the repository)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Every value comes from a ``CARBOREWIND_*`` environment variable (or a mounted ``.env`` file
    outside the repository). Secrets must never have a usable default.
    """

    model_config = SettingsConfigDict(env_prefix="CARBOREWIND_", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Origins allowed to call the API from a browser. Empty = same-origin only.
    cors_origins: list[str] = Field(default_factory=list)
    # Sent as Strict-Transport-Security; HTTPS is terminated by the reverse proxy.
    hsts_max_age: int = 63072000


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
