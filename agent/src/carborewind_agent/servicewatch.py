"""Carbonio services watcher: a thread of the agent, independent of the job loop.

Every minute it reports to the server:

* the status of each Carbonio service (``GetServiceStatusRequest``, what ``zmcontrol
  status`` shows: amavis, antispam, mta, mailbox, opendkim…), ignored when older than
  :data:`STALE` (the stats service that records it may be down itself);
* whether the essential local ports answer (MTA 25, Amavis 10024, LMTP 7025, LDAP 389):
  this works even when the admin API does not;
* the size of the MTA queues (``GetMailQueueInfoRequest``), when Carbonio answers.

The server decides when to alert (a service down on two reports in a row) and when the
service is back.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any

from .api_client import AgentApi, ApiError
from .carbonio import CarbonioClient, SoapFaultError

log = logging.getLogger(__name__)

STALE = 15 * 60  # seconds


def _hosts() -> list[str]:
    """Where Carbonio services listen: loopback, and the server's own name (LDAP, for one,
    listens only on ``mail.example.com:389``, not on 127.0.0.1)."""
    out = ["127.0.0.1"]
    for name in (socket.getfqdn(), socket.gethostname()):
        if name and name not in out and name != "localhost":
            out.append(name)
    return out


def port_open(port: int, host: str | None = None, timeout: float = 2.0) -> bool:
    """True when something accepts connections on ``port`` (on ``host``, or on any of
    the server's own addresses)."""
    for candidate in [host] if host else _hosts():
        try:
            with socket.create_connection((candidate, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def collect(
    carbonio: CarbonioClient,
    ports: dict[str, int],
    *,
    now: float | None = None,
    probe: Any = port_open,
) -> dict[str, Any]:
    """One report: ``{"checked_at", "services": [...], "queue": {...} | None, "errors"}``."""
    now = time.time() if now is None else now
    services: list[dict[str, Any]] = []
    errors: list[str] = []
    servers: set[str] = set()
    try:
        status = carbonio.service_status()
    except (SoapFaultError, OSError, ValueError) as exc:
        status = []
        errors.append(f"service status: {exc}")
    multi = len({srv for srv, *_ in status}) > 1
    for server, name, running, checked in status:
        if not name:
            continue
        servers.add(server)
        fresh = checked and now - checked <= STALE
        services.append(
            {
                "name": f"{name}@{server}" if multi else name,
                "ok": running if fresh else None,  # None: unknown (stale record)
                "source": "carbonio",
                "checked": checked or None,
            }
        )
    for name, port in sorted(ports.items()):
        services.append(
            {"name": f"port:{name}", "ok": bool(probe(port)), "source": "port", "port": port}
        )
    queue: dict[str, int] | None = None
    if servers:
        queue = {}
        for server in sorted(servers):
            try:
                for name, count in carbonio.mail_queue(server).items():
                    queue[name] = queue.get(name, 0) + count
            except (SoapFaultError, OSError, ValueError) as exc:
                errors.append(f"mail queue: {exc}")
                queue = None
                break
    return {
        "checked_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "services": services,
        "queue": queue,
        "errors": errors[:5],
    }


class ServiceMonitor:
    def __init__(
        self,
        api: AgentApi,
        carbonio: CarbonioClient,
        ports: dict[str, int],
        *,
        interval: float = 60.0,
    ) -> None:
        self._api = api
        self._carbonio = carbonio
        self._ports = ports
        self._interval = interval
        self.enabled = True

    def configure(self, enabled: bool, ports: dict[str, int]) -> None:
        """Settings → Agent: turn the watcher on or off, change the probed ports."""
        self.enabled = enabled
        self._ports = {str(k): int(v) for k, v in ports.items()}

    def tick(self) -> dict[str, Any]:
        report = collect(self._carbonio, self._ports)
        self._api.services(report)
        return report

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.enabled:
                    self.tick()
            except (ApiError, OSError) as exc:
                log.warning("service report failed", extra={"error": str(exc)})
            except Exception:  # keep watching whatever happens
                log.exception("service watch crashed")
            stop.wait(self._interval)
