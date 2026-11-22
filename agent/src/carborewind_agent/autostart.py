"""Start Carbonio again on its own when it stays down, then check that mail goes out.

After an upgrade, a reboot or a crash, Carbonio may stay stopped until someone runs
``zmcontrol start``. With the setting on (Settings → Agent), the agent asks for that start
once the essential parts (MTA, LMTP, LDAP, the mailbox) have been down for N minutes:

* the agent is unprivileged: it writes ``/var/lib/carborewind/control/request.json`` and the
  ``carborewind-control.path`` unit runs ``carborewind-agent control apply`` as root, which
  validates the request again and runs the one fixed command ``zmcontrol start`` as zextras
  (see :mod:`.control_apply`); its outcome comes back in ``status.json``;
* never while packages are being installed (apt / dpkg running: an upgrade stops Carbonio
  on purpose), never while the administrator paused it (maintenance), at most once every
  :data:`RETRY_AFTER` and :data:`MAX_STARTS` times in :data:`WINDOW`;
* once the services answer again, a test message goes to the internal and the external
  addresses chosen (:mod:`.mailtest`), and the outcome is reported.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONTROL_DIR = Path("/var/lib/carborewind/control")
RETRY_AFTER = 15 * 60  # seconds between two start requests
WINDOW = 6 * 3600
MAX_STARTS = 3
# What must answer for mail to work: Carbonio services and probed ports.
CORE_SERVICES = {"mta", "mailbox", "directory-server", "ldap"}
CORE_PORTS = {"port:mta", "port:lmtp", "port:ldap"}
PACKAGE_TOOLS = {"apt", "apt-get", "dpkg", "unattended-upgr", "aptitude"}


def package_manager_running(proc: Path = Path("/proc")) -> str | None:
    """Name of a running apt / dpkg process, if any (an upgrade is in progress)."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            name = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if name in PACKAGE_TOOLS:
            return name
    return None


def core_down(services: list[dict[str, Any]]) -> list[str]:
    """The essential parts reported down in this services report."""
    down = []
    for svc in services:
        name = str(svc.get("name", ""))
        base = name.split("@", 1)[0]
        if svc.get("ok") is False and (
            (svc.get("source") == "carbonio" and base in CORE_SERVICES) or name in CORE_PORTS
        ):
            down.append(name)
    return down


def _ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


@dataclass
class AutoStart:
    """Decides when to ask root to start Carbonio; keeps what the interface shows."""

    control_dir: Path = CONTROL_DIR
    enabled: bool = False
    minutes: int = 5
    paused_until: float | None = None
    busy: Callable[[], str | None] = package_manager_running
    down_since: float | None = None
    requests: list[float] = field(default_factory=list)
    last_request: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None
    waiting_back: bool = False
    note: str | None = None

    def configure(self, cfg: dict[str, Any]) -> None:
        self.enabled = bool(cfg.get("autostart_enabled", False))
        self.minutes = max(1, min(240, int(cfg.get("autostart_minutes") or 5)))
        self.paused_until = _ts(cfg.get("autostart_paused_until"))

    def _write_request(self, now: float) -> dict[str, Any]:
        request = {
            "id": uuid.uuid4().hex,
            "action": "start",
            "at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        }
        self.control_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.control_dir / ".request.json.tmp"
        tmp.write_text(json.dumps(request), encoding="utf-8")
        os.replace(tmp, self.control_dir / "request.json")  # the path unit sees one write
        return request

    def _read_result(self) -> None:
        try:
            data = json.loads((self.control_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if (
            isinstance(data, dict)
            and self.last_request
            and data.get("id") == self.last_request.get("id")
        ):
            self.last_result = {
                k: data.get(k) for k in ("id", "at", "ok", "returncode", "output", "error")
            }

    def evaluate(self, services: list[dict[str, Any]], now: float) -> bool:
        """Look at one services report; ask for a start when due. True when the services
        just came back after a start this agent asked for (time for the sending test)."""
        self._read_result()
        down = core_down(services)
        if not down:
            came_back = self.waiting_back
            self.down_since, self.waiting_back, self.note = None, False, None
            return came_back
        if self.down_since is None:
            self.down_since = now
        self.requests = [t for t in self.requests if now - t < WINDOW]
        if not self.enabled:
            self.note = "disabled"
            return False
        if self.paused_until and now < self.paused_until:
            self.note = "paused"
            return False
        if now - self.down_since < self.minutes * 60:
            self.note = "waiting"
            return False
        tool = self.busy()
        if tool:
            self.note = f"packages being installed ({tool})"
            return False
        if self.requests and now - self.requests[-1] < RETRY_AFTER:
            self.note = "started recently"
            return False
        if len(self.requests) >= MAX_STARTS:
            self.note = "too many starts: intervention needed"
            return False
        try:
            self.last_request = {**self._write_request(now), "down": down}
        except OSError as exc:
            self.note = f"cannot ask for the start: {exc}"
            return False
        self.requests.append(now)
        self.last_result = None
        self.waiting_back = True
        self.note = "start asked"
        return False

    def report(self) -> dict[str, Any]:
        def iso(t: float | None) -> str | None:
            return datetime.fromtimestamp(t, tz=UTC).isoformat() if t else None

        return {
            "enabled": self.enabled,
            "minutes": self.minutes,
            "paused_until": iso(self.paused_until),
            "down_since": iso(self.down_since),
            "note": self.note,
            "last_request": self.last_request,
            "last_result": self.last_result,
            "starts_6h": len(self.requests),
        }
