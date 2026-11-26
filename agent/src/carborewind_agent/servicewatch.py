"""Carbonio services watcher: a thread of the agent, independent of the job loop.

Every minute it reports to the server:

* the status of each Carbonio service (``GetServiceStatusRequest``, what ``zmcontrol
  status`` shows: amavis, antispam, mta, mailbox, opendkim…), ignored when older than
  :data:`STALE` (the stats service that records it may be down itself);
* whether the essential local ports answer (MTA 25, Amavis 10024, LMTP 7025, LDAP 389):
  this works even when the admin API does not;
* the size of the MTA queues (``GetMailQueueInfoRequest``), and the messages waiting in
  the deferred and hold queues (``GetMailQueueRequest``: sender, recipients, reason);
  the server answers with the queue actions asked in the interface (requeue, hold,
  release, delete, flush), applied at once;
* whether Postfix can actually deliver (:mod:`.mtahealth`: antivirus circuit, milter,
  policy server, read from the MTA log; the Postfix guard's status when installed).

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
from .autostart import AutoStart
from .carbonio import CarbonioClient, SoapFaultError
from .certcheck import CertWatch
from .mailtest import MailTest
from .mtahealth import MtaHealth

log = logging.getLogger(__name__)

STALE = 15 * 60  # seconds
# Queues whose messages are listed in the interface (active / incoming drain on their own).
DETAIL_QUEUES = ("deferred", "hold")
QUEUE_ITEMS = 50


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
    mta: MtaHealth | None = None,
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
    if mta is not None:
        extra, mta_errors = mta.collect(now)
        services.extend(extra)
        errors.extend(mta_errors)
    if not servers:
        # The service list is unavailable (stats service down, zimbraLogHostname unset…):
        # the queue still belongs to this host.
        servers = set(_hosts()[1:2]) or {"localhost"}
    totals: dict[str, int] = {}
    failed = False
    details: list[dict[str, Any]] = []
    for server in sorted(servers):
        try:
            counts = carbonio.mail_queue(server)
        except (SoapFaultError, OSError, ValueError) as exc:
            errors.append(f"mail queue: {exc}")
            failed = True
            break
        for name, count in counts.items():
            totals[name] = totals.get(name, 0) + count
        for name in DETAIL_QUEUES:
            if not counts.get(name):
                continue
            try:
                detail = carbonio.mail_queue_items(server, name, limit=QUEUE_ITEMS)
            except (SoapFaultError, OSError, ValueError) as exc:
                errors.append(f"mail queue {name}: {exc}")
                continue
            details.append({"server": server, "queue": name, **detail})
    return {
        "checked_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "services": services,
        "queue": None if failed else totals,
        "queue_items": details,
        "errors": errors[:5],
    }


def apply_queue_action(carbonio: CarbonioClient, action: dict[str, Any]) -> str | None:
    """Run one queue action asked in the interface; the error text, or None."""
    op = str(action.get("op", ""))
    server = str(action.get("server") or "") or (_hosts()[1:2] or ["localhost"])[0]
    try:
        if op == "flush":
            carbonio.mail_queue_flush(server)
        else:
            ids = [str(i) for i in action.get("ids") or []]
            carbonio.mail_queue_action(server, str(action.get("queue", "")), op, ids)
    except (SoapFaultError, OSError, ValueError) as exc:
        return str(exc)[:500]
    log.warning("mail queue action %s on %s (%s)", op, server, action.get("queue"))
    return None


class ServiceMonitor:
    def __init__(
        self,
        api: AgentApi,
        carbonio: CarbonioClient,
        ports: dict[str, int],
        *,
        interval: float = 60.0,
        mta: MtaHealth | None = None,
        sender: str = "",
        autostart: AutoStart | None = None,
        cert: CertWatch | None = None,
    ) -> None:
        self._api = api
        self._carbonio = carbonio
        self._ports = ports
        self._interval = interval
        self._mta = mta
        self.enabled = True
        self.sender = sender  # the agent's Carbonio account: From of the sending test
        self.autostart = autostart or AutoStart()
        self.cert = cert or CertWatch()
        self.test: MailTest | None = None
        self.test_after_start = True
        self.recipients: dict[str, str] = {}
        self.cert_enabled = True

    def configure(
        self, enabled: bool, ports: dict[str, int], cfg: dict[str, Any] | None = None
    ) -> None:
        """Settings → Agent: turn the watcher on or off, change the probed ports, the
        automatic start of Carbonio and the addresses of the sending test."""
        self.enabled = enabled
        self._ports = {str(k): int(v) for k, v in ports.items()}
        if cfg is not None:
            self.autostart.configure(cfg)
            self.cert_enabled = bool(cfg.get("cert_monitor", True))
            self.cert.configure(cfg, default_name=socket.getfqdn())
            self.test_after_start = bool(cfg.get("mail_test_after_start", True))
            self.recipients = {
                kind: str(cfg.get(f"mail_test_{kind}") or "").strip()
                for kind in ("internal", "external")
                if str(cfg.get(f"mail_test_{kind}") or "").strip()
            }

    def start_test(self, reason: str, test_id: str | None = None) -> MailTest | None:
        """Send the test message now, unless one is still waiting for its outcome."""
        if self.test is not None and not self.test.done:
            return None
        test = MailTest(self.sender, dict(self.recipients), reason=reason)
        if test_id:
            test.id = test_id
        if not self.sender:
            test.error = "no sender: CARBOREWIND_AGENT_CARBONIO_ADMIN_NAME is not set"
        elif not self.recipients:
            test.error = "no test address configured (Settings → Agent)"
        else:
            test.send()
        self.test = test
        log.warning("mail sending test %s (%s): %s", test.id, reason, test.error or "sent")
        return test

    def _extend(self, report: dict[str, Any]) -> dict[str, Any]:
        """Automatic start and sending test: decide, follow, report."""
        now = time.time()
        if self.autostart.evaluate(report["services"], now) and self.test_after_start:
            self.start_test("after_start")
        if self.test is not None and self._mta is not None:
            self.test.feed(self._mta.last_lines, now)
        if cert_enabled := self.cert_enabled:
            self.cert.evaluate(now)
        report["certificate"] = self.cert.report() if cert_enabled else None
        report["autostart"] = self.autostart.report()
        report["mail_test"] = self.test.report() if self.test else None
        return report

    def tick(self) -> dict[str, Any]:
        report = self._extend(collect(self._carbonio, self._ports, mta=self._mta))
        answer = self._api.services(report) or {}
        request = answer.get("mail_test")
        if isinstance(request, dict) and request.get("id"):
            self.start_test("manual", str(request["id"]))
        renew = answer.get("cert_renew")
        if isinstance(renew, dict) and renew.get("id"):
            self.cert.renew(time.time(), "manual", str(renew["id"]))
        actions = answer.get("queue_actions") or []
        for action in actions:
            error = apply_queue_action(self._carbonio, action)
            self._api.queue_action_done(str(action["id"]), error)
        if actions:  # show the result at once rather than in a minute
            report = self._extend(collect(self._carbonio, self._ports, mta=self._mta))
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
