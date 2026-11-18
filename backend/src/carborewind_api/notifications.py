"""E-mail notifications over SMTP.

* **Settings** (``app_settings`` row ``smtp``): server, port, security (STARTTLS, implicit TLS
  or none), credentials, sender, recipients, which events to send, digest hour. The password
  is encrypted with ``CARBOREWIND_SECRET_KEY`` (Fernet = AES-128-CBC + HMAC-SHA256) and never
  returned by the API.
* **Outbox** (``notifications`` table): events are queued in the same transaction as the
  change that caused them, then delivered by the scheduler with retries, so a slow or broken
  mail server never blocks the API or loses a message. A ``dedup_key`` sends each digest or
  alert once.
* **Events**: a job that failed for good, a backup finished with item errors, a restore with
  failed items, an agent silent for 30 minutes, a daily digest of unprotected accounts, and
  the end of a migration batch.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db.models import (
    Account,
    AccountStatus,
    Agent,
    AgentStatus,
    AppSetting,
    Notification,
    NotificationStatus,
    Snapshot,
    SnapshotStatus,
)
from .scheduling import zone

log = logging.getLogger(__name__)

SMTP_KEY = "smtp"
EVENTS = (
    "job_failed",
    "backup_partial",
    "restore_failed",
    "agent_offline",
    "daily_digest",
    "migration_done",
)
MAX_ATTEMPTS = 6
AGENT_SILENCE = timedelta(minutes=30)
Security = Literal["starttls", "ssl", "none"]


class SecretKeyError(RuntimeError):
    pass


def _fernet(settings: Settings) -> Fernet:
    if settings.secret_key is None or len(settings.secret_key.get_secret_value()) < 32:
        raise SecretKeyError("CARBOREWIND_SECRET_KEY (32+ characters) is not configured")
    digest = hashlib.sha256(
        b"carborewind/secrets/v1" + settings.secret_key.get_secret_value().encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(settings: Settings, value: str) -> str:
    return _fernet(settings).encrypt(value.encode()).decode()


def decrypt(settings: Settings, token: str) -> str:
    try:
        return _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken as exc:  # key changed: the admin must re-enter the password
        raise SecretKeyError("stored secret cannot be decrypted with the current key") from exc


@dataclass
class SmtpConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: Security = "starttls"
    username: str = ""
    password_enc: str | None = None
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    events: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(EVENTS, True))
    digest_hour: int = 8
    timezone: str = "UTC"
    language: Literal["fr", "en"] = "fr"

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> SmtpConfig:
        cfg = cls()
        for key, value in (data or {}).items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.events = {**dict.fromkeys(EVENTS, True), **(cfg.events or {})}
        return cfg

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def wants(self, event: str) -> bool:
        return self.enabled and bool(self.recipients) and self.events.get(event, False)


async def load_config(db: AsyncSession) -> SmtpConfig:
    row = await db.get(AppSetting, SMTP_KEY)
    return SmtpConfig.from_json(row.value if row else None)


async def save_config(db: AsyncSession, cfg: SmtpConfig) -> None:
    await db.execute(
        insert(AppSetting)
        .values(key=SMTP_KEY, value=cfg.to_json(), updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": cfg.to_json(), "updated_at": func.now()}
        )
    )


# ------------------------------------------------------------------ messages

TEXTS: dict[str, dict[str, str]] = {
    "fr": {
        "job_failed": "[CarboRewind] Échec : {kind} de {account}",
        "job_failed_body": (
            "Le job {kind} du compte {account} a échoué définitivement.\n\n"
            "Erreur : {error}\nJob : {job}\n"
        ),
        "backup_partial": "[CarboRewind] Sauvegarde partielle de {account}",
        "backup_partial_body": (
            "La sauvegarde n° {seq} du compte {account} est terminée avec {errors} élément(s) "
            "en erreur. Les autres éléments sont sauvegardés et restaurables.\n"
        ),
        "restore_failed": "[CarboRewind] Restauration incomplète vers {account}",
        "restore_failed_body": (
            "La restauration vers {account} s'est terminée avec {failed} élément(s) non "
            "restauré(s) ({restored} restauré(s)).\n"
        ),
        "agent_offline": "[CarboRewind] Agent {agent} silencieux",
        "agent_offline_body": (
            "L'agent {agent} ({host}) n'a pas contacté le serveur depuis {since}. "
            "Aucune sauvegarde ni restauration ne peut s'exécuter.\n"
        ),
        "daily_digest": "[CarboRewind] {count} compte(s) sans sauvegarde depuis 24 h",
        "daily_digest_body": (
            "{count} compte(s) actif(s) n'ont pas de sauvegarde réussie depuis 24 h :\n\n{list}\n"
        ),
        "migration_done": "[CarboRewind] Migration « {batch} » terminée",
        "migration_done_body": (
            "Le lot de migration « {batch} » n'a plus de tâche en cours.\n\n"
            "Boîtes copiées : {done}\nBoîtes en échec : {failed}\n"
            "Messages copiés : {copied}\nMessages non copiés : {errors}\n"
        ),
        "test": "[CarboRewind] E-mail de test",
        "test_body": "Les notifications de CarboRewind fonctionnent.\n",
        "never": "jamais",
    },
    "en": {
        "job_failed": "[CarboRewind] Failed: {kind} of {account}",
        "job_failed_body": (
            "The {kind} job of {account} failed for good.\n\nError: {error}\nJob: {job}\n"
        ),
        "backup_partial": "[CarboRewind] Partial backup of {account}",
        "backup_partial_body": (
            "Backup #{seq} of {account} finished with {errors} item(s) in error. The other "
            "items are backed up and restorable.\n"
        ),
        "restore_failed": "[CarboRewind] Incomplete restore into {account}",
        "restore_failed_body": (
            "The restore into {account} finished with {failed} item(s) not restored "
            "({restored} restored).\n"
        ),
        "agent_offline": "[CarboRewind] Agent {agent} is silent",
        "agent_offline_body": (
            "Agent {agent} ({host}) has not contacted the server since {since}. No backup or "
            "restore can run.\n"
        ),
        "daily_digest": "[CarboRewind] {count} account(s) without a backup for 24 h",
        "daily_digest_body": (
            "{count} active account(s) have no successful backup in the last 24 h:\n\n{list}\n"
        ),
        "migration_done": '[CarboRewind] Migration "{batch}" finished',
        "migration_done_body": (
            'Migration batch "{batch}" has no job left running.\n\n'
            "Mailboxes copied: {done}\nMailboxes failed: {failed}\n"
            "Messages copied: {copied}\nMessages not copied: {errors}\n"
        ),
        "test": "[CarboRewind] Test e-mail",
        "test_body": "CarboRewind notifications work.\n",
        "never": "never",
    },
}


def _one_line(value: object) -> str:
    return " ".join(str(value).split())[:500]


async def notify(db: AsyncSession, event: str, dedup_key: str | None = None, **values: Any) -> bool:
    """Queue ``event`` if notifications want it; returns True when queued.

    Runs in the caller's transaction: the message exists only if the change is committed.
    """
    cfg = await load_config(db)
    if not cfg.wants(event):
        return False
    texts = TEXTS.get(cfg.language, TEXTS["fr"])
    safe = {k: _one_line(v) if k != "list" else str(v) for k, v in values.items()}
    stmt = insert(Notification).values(
        event=event,
        subject=texts[event].format(**safe),
        body=texts[f"{event}_body"].format(**safe),
        recipients=list(cfg.recipients),
        dedup_key=dedup_key,
    )
    if dedup_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
    result = await db.execute(stmt.returning(Notification.id))
    return result.first() is not None


# ------------------------------------------------------------------ sending


class SmtpError(Exception):
    pass


def send_mail(
    settings: Settings, cfg: SmtpConfig, recipients: list[str], subject: str, body: str
) -> None:
    """Send one message synchronously (call from a thread). Raises :class:`SmtpError`."""
    if not cfg.host or not cfg.sender or not recipients:
        raise SmtpError("SMTP server, sender and recipients are required")
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.rpartition("@")[2] or None)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)
    password = decrypt(settings, cfg.password_enc) if cfg.password_enc else ""
    context = ssl.create_default_context()
    try:
        if cfg.security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=20, context=context)
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, timeout=20)
        with client:
            client.ehlo()
            if cfg.security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if cfg.username:
                client.login(cfg.username, password)
            client.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise SmtpError(f"{type(exc).__name__}: {exc}") from exc


async def deliver_pending(db: AsyncSession, settings: Settings, now: datetime) -> int:
    """Send due messages of the outbox; retries with backoff. Returns how many were sent."""
    cfg = await load_config(db)
    pending = list(
        await db.scalars(
            select(Notification)
            .where(
                Notification.status == NotificationStatus.PENDING,
                Notification.next_attempt_at <= now,
            )
            .order_by(Notification.id)
            .limit(50)
            .with_for_update(skip_locked=True)
        )
    )
    sent = 0
    for note in pending:
        note.attempts += 1
        try:
            if not cfg.enabled:
                raise SmtpError("notifications are disabled")
            await asyncio.to_thread(
                send_mail, settings, cfg, list(note.recipients), note.subject, note.body
            )
        except (SmtpError, SecretKeyError) as exc:
            note.error = str(exc)[:1000]
            if note.attempts >= MAX_ATTEMPTS:
                note.status = NotificationStatus.FAILED
            else:
                note.next_attempt_at = now + timedelta(minutes=2**note.attempts)
            log.warning("notification %s not sent: %s", note.id, exc)
            continue
        note.status, note.sent_at, note.error = NotificationStatus.SENT, now, None
        sent += 1
    return sent


# ------------------------------------------------------------------ periodic alerts


async def periodic_alerts(db: AsyncSession, now: datetime) -> int:
    """Agent silence and the daily digest; returns how many messages were queued."""
    cfg = await load_config(db)
    if not cfg.enabled:
        return 0
    queued = 0
    texts = TEXTS.get(cfg.language, TEXTS["fr"])
    if cfg.events.get("agent_offline"):
        silent = await db.scalars(
            select(Agent).where(
                Agent.status == AgentStatus.ACTIVE,
                Agent.last_heartbeat_at < now - AGENT_SILENCE,
            )
        )
        for agent in silent:
            since = agent.last_heartbeat_at or now
            queued += await notify(
                db,
                "agent_offline",
                dedup_key=f"agent_offline:{agent.id}:{since:%Y%m%d%H}",
                agent=agent.name,
                host=agent.hostname,
                since=f"{since:%Y-%m-%d %H:%M} UTC",
            )
    local = now.astimezone(zone(cfg.timezone))
    if cfg.events.get("daily_digest") and local.hour >= cfg.digest_hour:
        latest_ok = (
            select(func.max(Snapshot.taken_at))
            .where(
                Snapshot.account_id == Account.id,
                Snapshot.status.in_([SnapshotStatus.COMPLETE, SnapshotStatus.PARTIAL]),
            )
            .scalar_subquery()
        )
        rows = (
            await db.execute(
                select(Account.email, latest_ok)
                .where(Account.status == AccountStatus.ACTIVE)
                .where((latest_ok.is_(None)) | (latest_ok < now - timedelta(hours=24)))
                .order_by(Account.email)
            )
        ).all()
        if rows:
            lines = [
                f"- {email} ({texts['never'] if last is None else f'{last:%Y-%m-%d %H:%M} UTC'})"
                for email, last in rows[:200]
            ]
            if len(rows) > 200:
                lines.append(f"… (+{len(rows) - 200})")
            queued += await notify(
                db,
                "daily_digest",
                dedup_key=f"digest:{local:%Y-%m-%d}",
                count=len(rows),
                list="\n".join(lines),
            )
    return queued
