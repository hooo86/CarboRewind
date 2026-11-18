"""Application settings, loaded from environment variables only (never from the repository)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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
    # postgresql+psycopg://user:password@host/db — contains a secret, hence SecretStr.
    database_url: SecretStr | None = None
    # redis://[:password@]host:port/db
    redis_url: SecretStr | None = None
    # Per-dependency timeout of the readiness probe, in seconds.
    readiness_timeout: float = Field(default=2.0, gt=0, le=30)

    # --- sessions
    # Sessions end after this much inactivity…
    session_idle_minutes: int = Field(default=30, ge=5, le=24 * 60)
    # …and in any case after this long.
    session_absolute_hours: int = Field(default=12, ge=1, le=7 * 24)

    # --- login throttling
    # Failed attempts on one account before it is locked; the lock doubles each time.
    login_max_failures: int = Field(default=5, ge=3, le=20)
    login_lock_base_minutes: int = Field(default=1, ge=1, le=60)
    # Attempts per source IP and window (all usernames together).
    login_ip_limit: int = Field(default=20, ge=1)
    login_ip_window_seconds: int = Field(default=300, ge=10)

    # --- LDAP authentication against the Carbonio directory (disabled when ldap_url is unset)
    # ldaps://mail.example.org:636 (or ldap:// + ldap_start_tls). Plain LDAP is refused.
    ldap_url: str | None = None
    ldap_start_tls: bool = False
    ldap_ca_file: str | None = None
    # Read-only service account used to look the user up before binding as them.
    ldap_bind_dn: str | None = None
    ldap_bind_password: SecretStr | None = None
    ldap_user_base: str | None = None
    # {username} is replaced by the RFC 4515-escaped login. Default: Carbonio admin accounts
    # (assumption to verify on the target Carbonio CE version).
    ldap_user_filter: str = (
        "(&(objectClass=zimbraAccount)(zimbraIsAdminAccount=TRUE)"
        "(zimbraAccountStatus=active)(|(uid={username})(mail={username})))"
    )
    ldap_timeout: float = Field(default=5.0, gt=0, le=60)

    @property
    def ldap_enabled(self) -> bool:
        return bool(self.ldap_url and self.ldap_user_base)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
