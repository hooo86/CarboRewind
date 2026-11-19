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
    # CA bundle of the API's server certificate (self-signed install). None = system CAs.
    api_ca_file: Path | None = None
    tls_cert: Path = Path("/etc/carborewind/agent.crt")
    tls_key: Path = Path("/etc/carborewind/agent.key")
    tls_ca: Path = Path("/etc/carborewind/ca.crt")

    # Local Carbonio endpoints.
    soap_admin_url: AnyHttpUrl = AnyHttpUrl("https://localhost:7071/service/admin/soap")
    mailbox_url: AnyHttpUrl = AnyHttpUrl("https://localhost:443")
    # Dedicated Carbonio admin account used by the agent; its password lives in a 0600 file.
    carbonio_admin_name: str = "carborewind@localhost"
    carbonio_admin_password_file: Path = Path("/etc/carborewind/carbonio-admin.password")
    # CA bundle for Carbonio's certificate (self-signed by default). None = system CAs.
    # TLS verification is never disabled.
    carbonio_ca_file: Path | None = None
    carbonio_timeout: float = Field(default=60.0, gt=0, le=600)

    # Backup repository (local disk or NFS mount point).
    repository_path: Path = Path("/var/lib/carborewind/repository")
    # Disk guard: backups stop before the repository's file system gets too full. On a
    # local disk (shared with Carbonio), max(min_free_gb_local, min_free_percent_local % of
    # the disk) must stay free; on a network share, min_free_gb_network.
    min_free_gb_local: float = Field(default=20.0, ge=0)
    min_free_percent_local: float = Field(default=15.0, ge=0, le=90)
    min_free_gb_network: float = Field(default=2.0, ge=0)
    # Mail traffic statistics (sending limits, spam) read from the MTA log.
    mail_monitor: bool = True
    mail_logs: list[Path] = Field(
        default_factory=lambda: [
            Path("/var/log/carbonio.log"),
            Path("/var/log/zimbra.log"),
            Path("/var/log/mail.log"),
        ]
    )
    mail_state_file: Path = Path("/var/lib/carborewind/mail-stats.json")
    # Copy 02: mirror of the repository on a second share (set by `storage apply`).
    replica_path: Path | None = None
    # Hand-off with the root helper applying the storage settings of the interface.
    storage_dir: Path = Path("/var/lib/carborewind/storage")
    # KEK protecting the repository master key: base64 of 32 bytes, file mode 0600/0400.
    repository_key_file: Path = Path("/etc/carborewind/repository.key")

    # Throttling: concurrent mailboxes and bandwidth ceiling (0 = unlimited).
    max_concurrent_accounts: int = Field(default=2, ge=1, le=32)
    max_bandwidth_mbps: int = Field(default=0, ge=0)
    # Pause reads while the 1-min load average per CPU exceeds this (0 = disabled).
    max_load_per_cpu: float = Field(default=1.5, ge=0)

    def missing_files(self) -> list[Path]:
        """Return the TLS files that do not exist on disk."""
        return [p for p in (self.tls_cert, self.tls_key, self.tls_ca) if not p.is_file()]
