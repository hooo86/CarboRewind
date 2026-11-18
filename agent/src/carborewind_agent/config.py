"""Agent settings, loaded from environment variables (``CARBOREWIND_AGENT_*``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """Runtime configuration of the agent.

    Certificates and keys are referenced by path (files readable only by the agent's
    system user); their content never appears in the configuration itself.
    """

    model_config = SettingsConfigDict(env_prefix="CARBOREWIND_AGENT_", extra="ignore")

    # Control-plane API, reached over mTLS (outbound connection only).
    api_url: AnyHttpUrl
    tls_cert: Path = Path("/etc/carborewind/agent.crt")
    tls_key: Path = Path("/etc/carborewind/agent.key")
    tls_ca: Path = Path("/etc/carborewind/ca.crt")

    # Local Carbonio endpoints.
    soap_admin_url: AnyHttpUrl = AnyHttpUrl("https://localhost:7071/service/admin/soap")
    mailbox_url: AnyHttpUrl = AnyHttpUrl("https://localhost:443")

    # Backup repository (local disk or NFS mount point).
    repository_path: Path = Path("/var/lib/carborewind/repository")

    # Throttling: concurrent mailboxes and bandwidth ceiling (0 = unlimited).
    max_concurrent_accounts: int = Field(default=2, ge=1, le=32)
    max_bandwidth_mbps: int = Field(default=0, ge=0)

    def missing_files(self) -> list[Path]:
        """Return the TLS files that do not exist on disk."""
        return [p for p in (self.tls_cert, self.tls_key, self.tls_ca) if not p.is_file()]
