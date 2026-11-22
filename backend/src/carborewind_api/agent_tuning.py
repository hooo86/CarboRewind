"""Agent settings chosen in the interface (Settings → Agent).

Everything an administrator may want to tune without logging on to the Carbonio server:
parallel backups, bandwidth, load back-off, disk guard, Carbonio services and mail traffic
watchers. The agent fetches them (``GET /api/agent/config``) every few seconds and applies
them at once; until they are saved once, it keeps the values of its ``agent.env``.

Connection and security settings (API address, certificates, repository path, key file,
Carbonio admin account) stay in ``agent.env`` on purpose: changing them remotely could cut
the agent off or weaken its security.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import AppSetting

KEY = "agent_tuning"

# Probed on the Carbonio host. 587/465: submission; 10025: Amavis hands mail back to
# Postfix (missing when an upgrade restored the stock master.cf).
NEW_PORTS = {"submission": 587, "smtps": 465, "amavis_return": 10025}
DEFAULT_PORTS = {"mta": 25, **NEW_PORTS, "amavis": 10024, "lmtp": 7025, "ldap": 389}
PORTS_DEFAULTS = 2


@dataclass
class AgentTuning:
    revision: int = 0
    max_concurrent_accounts: int = 2
    max_bandwidth_mbps: int = 0
    max_load_per_cpu: float = 1.5
    min_free_gb_local: float = 20.0
    min_free_percent_local: float = 15.0
    min_free_gb_network: float = 2.0
    service_monitor: bool = True
    service_ports: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PORTS))
    mail_monitor: bool = True
    # Start Carbonio again when its essential parts stay down this long (off by default:
    # an administrator decides), and test sending once it is back.
    autostart_enabled: bool = False
    autostart_minutes: int = 5
    autostart_paused_until: str | None = None  # maintenance: ISO date, None = not paused
    mail_test_after_start: bool = True
    mail_test_internal: str = ""
    mail_test_external: str = ""
    # Version of the default port list the saved settings already include (see from_json).
    ports_defaults: int = PORTS_DEFAULTS

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> AgentTuning:
        out = cls()
        for key, value in (data or {}).items():
            if hasattr(out, key):
                setattr(out, key, value)
        if data and int(data.get("ports_defaults", 1)) < PORTS_DEFAULTS:
            # Settings saved before the Postfix ports were added: add them once (a port
            # the administrator removes afterwards stays removed).
            out.service_ports = {**NEW_PORTS, **dict(out.service_ports)}
            out.ports_defaults = PORTS_DEFAULTS
        return out


async def load(db: AsyncSession) -> AgentTuning:
    row = await db.get(AppSetting, KEY)
    return AgentTuning.from_json(row.value if row else None)


async def save(db: AsyncSession, cfg: AgentTuning) -> None:
    value = asdict(cfg)
    await db.execute(
        insert(AppSetting)
        .values(key=KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )
