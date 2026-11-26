"""Test that mail goes out: one message to an internal and one to an external address.

The message is handed to the local Postfix (``127.0.0.1:25``, the server's own address, so
no password is needed) with a unique ``Message-ID``. Its fate is then read in the MTA log
the agent already follows: every queue id that logged that Message-ID (the original one,
and the one Amavis hands back after the antivirus scan), then the line where Postfix
delivered it — ``status=sent`` (delivered to the mailbox, or accepted by the remote server),
``deferred`` (will retry) or ``bounced`` (refused), with the reason. The hop to Amavis
(``relay=…:10024``) is not a delivery and is skipped.
"""

from __future__ import annotations

import re
import smtplib
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formatdate
from typing import Any

SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25
TIMEOUT = 10 * 60  # seconds before a recipient without news counts as failed
ADDRESS = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_QID = r"([0-9A-Za-z]{5,32})"
MSGID_LINE = re.compile(_QID + r": message-id=<([^>]+)>")
DELIVERY = re.compile(
    _QID + r": to=<([^>]+)>.*?relay=([^,]+),.*?status=(sent|deferred|bounced|expired)\s*(.*)$"
)


@dataclass
class MailTest:
    """One test run: who it was sent to, and what Postfix did with each copy."""

    sender: str
    recipients: dict[str, str]  # kind ("internal" / "external") -> address
    reason: str = "manual"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.time)
    error: str | None = None
    queue_ids: set[str] = field(default_factory=set)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def message_id(self) -> str:
        return f"carborewind-test-{self.id}@{self.sender.split('@')[-1]}"

    def send(self, smtp: Any = smtplib.SMTP) -> None:
        targets = [a for a in self.recipients.values() if ADDRESS.match(a)]
        for kind, address in self.recipients.items():
            self.results[kind] = {"address": address, "status": "pending"}
            if not ADDRESS.match(address):
                self.results[kind] = {"address": address, "status": "error", "detail": "invalid"}
        if not targets:
            self.error = "no valid recipient"
            return
        msg = EmailMessage()
        msg["From"] = f"CarboRewind <{self.sender}>"
        msg["To"] = ", ".join(targets)
        msg["Subject"] = f"[CarboRewind] Test d'envoi ({self.reason})"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = f"<{self.message_id}>"
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(
            "Message de test envoyé automatiquement par CarboRewind pour vérifier que la "
            "messagerie distribue le courrier en interne et vers l'extérieur.\n"
            "Vous pouvez le supprimer.\n"
        )
        try:
            with smtp(SMTP_HOST, SMTP_PORT, timeout=30) as client:
                refused = client.send_message(msg, from_addr=self.sender, to_addrs=targets)
        except (OSError, smtplib.SMTPException) as exc:
            self.error = f"SMTP {SMTP_HOST}:{SMTP_PORT}: {exc}"[:300]
            for result in self.results.values():
                if result["status"] == "pending":
                    result.update(status="error", detail=self.error)
            return
        for address, (code, text) in (refused or {}).items():
            for result in self.results.values():
                if result["address"] == address:
                    result.update(status="error", detail=f"{code} {text!r}"[:300])

    def feed(self, lines: list[str], now: float | None = None) -> None:
        """Read new MTA log lines; settle the recipients whose delivery is logged."""
        now = time.time() if now is None else now
        wanted = {r["address"].lower(): r for r in self.results.values()}
        for line in lines:
            m = MSGID_LINE.search(line)
            if m and m.group(2) == self.message_id:
                self.queue_ids.add(m.group(1))
                continue
            d = DELIVERY.search(line)
            if not d or d.group(1) not in self.queue_ids:
                continue
            relay = d.group(3)
            if relay.endswith(":10024") or relay.endswith(":10026"):
                continue  # the hop to the content filter, not the delivery
            result = wanted.get(d.group(2).lower())
            if result is None or result["status"] in ("sent", "bounced", "expired"):
                continue
            result.update(status=d.group(4), relay=relay, detail=d.group(5)[:300])
        for result in self.results.values():
            if result["status"] in ("pending", "deferred") and now - self.started > TIMEOUT:
                result["status"] = "timeout" if result["status"] == "pending" else "deferred"
                result["final"] = True

    @property
    def done(self) -> bool:
        return all(
            r["status"] in ("sent", "bounced", "expired", "error", "timeout") or r.get("final")
            for r in self.results.values()
        )

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r["status"] == "sent" for r in self.results.values())

    def report(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
            "started": datetime.fromtimestamp(self.started, tz=UTC).isoformat(),
            "done": self.done,
            "ok": self.ok if self.done else None,
            "error": self.error,
            "results": self.results,
        }
