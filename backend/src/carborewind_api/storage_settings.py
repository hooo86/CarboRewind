"""Where backups are stored, chosen in the web interface (``app_settings`` row ``storage``).

* **Copy 1** (the repository): local disk of the Carbonio server or an SMB/CIFS share (NAS).
* **Copy 2** (optional): a second SMB share holding a mirror of the repository, refreshed by
  ``replicate`` jobs after new backups and on demand.

Every change bumps a revision (per copy). The agent fetches the settings with the share
passwords decrypted (mutual TLS only) and hands them to a root helper on the Carbonio server
that mounts the shares; the outcome comes back in the agent's storage report. Passwords are
stored encrypted with ``CARBOREWIND_SECRET_KEY`` and never returned by the API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db.models import AppSetting
from .notifications import decrypt

STORAGE_KEY = "storage"
# Same rules as the agent (``carborewind_agent.storage``), checked again there as root.
SHARE_RE = re.compile(r"^//[A-Za-z0-9._-]+(/[^/\x00-\x1f\\,]+)+/?$")
SAFE_FIELD = re.compile(r"^[^\x00-\x1f,=\\]*$")


def valid_share(value: str) -> bool:
    return bool(SHARE_RE.match(value)) and not any(p in {".", ".."} for p in value.split("/"))


@dataclass
class StorageConfig:
    revision: int = 0
    primary_revision: int = 0
    copy_revision: int = 0
    # {"kind": "local"} or {"kind": "smb", share, username, password_enc, domain, seal, migrate}
    primary: dict[str, Any] | None = None
    # None, or {share, username, password_enc, domain, seal}
    copy: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> StorageConfig:
        data = dict(data or {})
        return cls(
            revision=int(data.pop("revision", 0)),
            primary_revision=int(data.pop("primary_revision", 0)),
            copy_revision=int(data.pop("copy_revision", 0)),
            primary=data.pop("primary", None),
            copy=data.pop("copy", None),
            extra=data,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            **self.extra,
            "revision": self.revision,
            "primary_revision": self.primary_revision,
            "copy_revision": self.copy_revision,
            "primary": self.primary,
            "copy": self.copy,
        }

    def for_agent(self, settings: Settings) -> dict[str, Any]:
        """What the agent needs, passwords decrypted (raises SecretKeyError)."""

        def clear(section: dict[str, Any] | None) -> dict[str, Any] | None:
            if section is None:
                return None
            out = {k: v for k, v in section.items() if k != "password_enc"}
            if section.get("password_enc"):
                out["password"] = decrypt(settings, section["password_enc"])
            return out

        return {
            "revision": self.revision,
            "primary_revision": self.primary_revision,
            "copy_revision": self.copy_revision,
            "primary": clear(self.primary),
            "copy": clear(self.copy),
        }


async def load(db: AsyncSession) -> StorageConfig:
    row = await db.get(AppSetting, STORAGE_KEY)
    return StorageConfig.from_json(row.value if row else None)


async def save(db: AsyncSession, cfg: StorageConfig) -> None:
    await db.execute(
        insert(AppSetting)
        .values(key=STORAGE_KEY, value=cfg.to_json(), updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": cfg.to_json(), "updated_at": func.now()}
        )
    )
