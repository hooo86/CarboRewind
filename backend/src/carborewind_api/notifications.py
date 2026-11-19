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
  failed items, an agent silent for 30 minutes, a daily digest of unprotected accounts, the
  end of a migration batch, and a mailbox over its sending limit.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import smtplib
import ssl
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Literal

import httpx
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
    "mail_limit",
    "integrity_failed",
    "replica_lag",
    "storage_full",
    "mailbox_quota",
    "archive_report",
    "service_down",
    "service_up",
    "mail_queue",
    "blacklisted",
    "blacklist_cleared",
    "restore_test_failed",
)
# Events sent to Telegram by default: the ones someone must act on now.
URGENT_EVENTS = {
    "job_failed",
    "agent_offline",
    "integrity_failed",
    "storage_full",
    "mail_limit",
    "service_down",
    "service_up",
    "mail_queue",
    "blacklisted",
    "restore_test_failed",
}
TELEGRAM_KEY = "telegram"
TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX = 4000  # Telegram refuses texts over 4096 characters
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
    # Fallback server: used when the main one refuses or cannot be reached.
    fallback_enabled: bool = False
    fallback_host: str = ""
    fallback_port: int = 587
    fallback_security: Security = "starttls"
    fallback_username: str = ""
    fallback_password_enc: str | None = None
    fallback_sender: str = ""  # empty: same sender as the main server

    @property
    def has_fallback(self) -> bool:
        return self.fallback_enabled and bool(self.fallback_host)

    def fallback(self) -> SmtpConfig:
        """The same settings, pointing at the fallback server."""
        return replace(
            self,
            host=self.fallback_host,
            port=self.fallback_port,
            security=self.fallback_security,
            username=self.fallback_username,
            password_enc=self.fallback_password_enc,
            sender=self.fallback_sender or self.sender,
        )

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


@dataclass
class TelegramConfig:
    enabled: bool = False
    token_enc: str | None = None
    chat_ids: list[str] = field(default_factory=list)
    events: dict[str, bool] = field(default_factory=lambda: {e: e in URGENT_EVENTS for e in EVENTS})

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> TelegramConfig:
        cfg = cls()
        for key, value in (data or {}).items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        cfg.events = {**{e: e in URGENT_EVENTS for e in EVENTS}, **(cfg.events or {})}
        return cfg

    def wants(self, event: str) -> bool:
        return (
            self.enabled
            and bool(self.token_enc)
            and bool(self.chat_ids)
            and self.events.get(event, False)
        )


async def load_telegram(db: AsyncSession) -> TelegramConfig:
    row = await db.get(AppSetting, TELEGRAM_KEY)
    return TelegramConfig.from_json(row.value if row else None)


async def save_telegram(db: AsyncSession, cfg: TelegramConfig) -> None:
    value = dict(cfg.__dict__)
    await db.execute(
        insert(AppSetting)
        .values(key=TELEGRAM_KEY, value=value, updated_at=func.now())
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": value, "updated_at": func.now()}
        )
    )


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
        "mail_limit": "[CarboRewind] Limite d'envoi atteinte : {account}",
        "mail_limit_body": (
            "Le compte {account} a atteint une limite d'envoi.\n\n"
            "Mesure : {metric}\nValeur : {value} (seuil {threshold})\nAction : {action}\n\n"
            "Si le compte est verrouillé, réactivez-le depuis CarboRewind (Trafic mail) après "
            "avoir changé son mot de passe.\n"
        ),
        "integrity_failed": "[CarboRewind] Corruption détectée dans {target}",
        "integrity_failed_body": (
            "La vérification d'intégrité de {target} a trouvé {count} fichier(s) corrompu(s) "
            "sur {checked} vérifié(s).\nJob : {job}\n\nLes sauvegardes concernées ne sont "
            "plus restaurables depuis cette copie : utilisez l'autre copie et vérifiez le "
            "stockage (disque, NAS).\n"
        ),
        "replica_lag": "[CarboRewind] La copie 2 a {hours} h de retard",
        "replica_lag_body": (
            "La copie 2 (deuxième NAS) n'a pas été mise à jour depuis {hours} heures (seuil : "
            "{limit} h). Vérifiez que le partage est accessible.\n"
        ),
        "storage_full": "[CarboRewind] Stockage plein dans {days} jours ({target})",
        "storage_full_body": (
            "Au rythme actuel, le stockage {target} de l'agent {agent} sera plein dans environ "
            "{days} jours. Agrandissez-le ou réduisez la rétention.\n"
        ),
        "mailbox_quota": "[CarboRewind] {count} boîte(s) proche(s) de la saturation",
        "mailbox_quota_body": (
            "{count} boîte(s) ont atteint le seuil d'alerte (Supervision → Réglages) :\n\n{list}\n"
        ),
        "restore_test_failed": "[CarboRewind] Test de restauration en échec ({source})",
        "restore_test_failed_body": (
            "Le test de restauration automatique de {source} (sauvegarde n° {seq}) vers {target} "
            "a échoué : {restored} message(s) restauré(s) sur {selected}, {failed} en erreur.\n"
            "{error}\nVérifiez le job dans CarboRewind (Jobs) : une restauration réelle pourrait "
            "échouer de la même façon.\n"
        ),
        "blacklisted": "[CarboRewind] Adresse {ip} en liste noire : {zone}",
        "blacklisted_body": (
            "L'adresse {ip} du serveur de messagerie apparaît dans la liste noire {zone} "
            "(code {codes}).\nCertains destinataires risquent de refuser vos messages. Cherchez "
            "la cause (boîte compromise, envoi massif : voir Trafic mail), corrigez-la puis "
            "demandez le retrait sur le site de la liste.\n"
        ),
        "blacklist_cleared": "[CarboRewind] Adresse {ip} retirée de {zone}",
        "blacklist_cleared_body": "L'adresse {ip} n'apparaît plus dans la liste noire {zone}.\n",
        "service_down": "[CarboRewind] Service Carbonio arrêté : {service}",
        "service_down_body": (
            "Le service {service} ne fonctionne plus sur le serveur de l'agent {agent} depuis "
            "{since}.\nSi c'est Amavis ou le MTA, les messages restent dans la file d'attente "
            '(rien n\'est perdu). Vérifiez avec : su - zextras -c "zmcontrol status" ; '
            'redémarrez avec : su - zextras -c "zmcontrol start".\n'
        ),
        "service_up": "[CarboRewind] Service Carbonio rétabli : {service}",
        "service_up_body": "Le service {service} fonctionne à nouveau (arrêté depuis {since}).\n",
        "mail_queue": "[CarboRewind] File d'attente mail : {count} message(s)",
        "mail_queue_body": (
            "La file d'attente du MTA ({agent}) contient {count} message(s) (seuil : {limit}) : "
            "{detail}.\nUn service arrêté (Amavis…) ou un destinataire injoignable bloque "
            "l'envoi.\n"
        ),
        "archive_report": "[CarboRewind] Archivage « {policy} » : {archived} message(s) archivé(s)",
        "archive_report_body": (
            "Passage de la politique d'archivage « {policy} » ({mode}) sur {accounts} compte(s) :\n"
            "- messages concernés : {candidates}\n"
            "- messages archivés : {archived}\n"
            "- messages retirés des boîtes : {removed} ({freed} Gio libérés)\n"
            "- erreurs : {errors} ; comptes en échec : {failed}\n"
        ),
        "mode.simulate": "simulation",
        "mode.copy": "copie seule",
        "mode.archive": "archivage complet",
        "metric.sent_messages_hour": "messages envoyés dans l'heure",
        "metric.sent_recipients_hour": "destinataires dans l'heure",
        "metric.sent_messages_day": "messages envoyés sur 24 h",
        "metric.sent_recipients_day": "destinataires sur 24 h",
        "metric.spam_sent_day": "spams envoyés sur 24 h",
        "action.alert": "alerte seulement",
        "action.lock": "compte verrouillé",
        "action.alert_admin": (
            "alerte seulement (compte administrateur, jamais verrouillé automatiquement)"
        ),
        "action.alert_cap": "alerte seulement (trop de verrouillages automatiques dans l'heure)",
        "target.primary": "la copie 1 (dépôt)",
        "target.replica": "la copie 2 (deuxième NAS)",
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
        "mail_limit": "[CarboRewind] Sending limit reached: {account}",
        "mail_limit_body": (
            "Account {account} reached a sending limit.\n\n"
            "Metric: {metric}\nValue: {value} (limit {threshold})\nAction: {action}\n\n"
            "If the account is locked, unlock it from CarboRewind (Mail traffic) after "
            "changing its password.\n"
        ),
        "integrity_failed": "[CarboRewind] Corruption found in {target}",
        "integrity_failed_body": (
            "The integrity check of {target} found {count} corrupted file(s) out of {checked} "
            "checked.\nJob: {job}\n\nThe affected backups can no longer be restored from this "
            "copy: use the other copy and check the storage (disk, NAS).\n"
        ),
        "replica_lag": "[CarboRewind] Copy 2 is {hours} h behind",
        "replica_lag_body": (
            "Copy 2 (second NAS) has not been refreshed for {hours} hours (limit: {limit} h). "
            "Check that the share is reachable.\n"
        ),
        "storage_full": "[CarboRewind] Storage full in {days} days ({target})",
        "storage_full_body": (
            "At the current rate, the {target} storage of agent {agent} will be full in about "
            "{days} days. Enlarge it or reduce the retention.\n"
        ),
        "mailbox_quota": "[CarboRewind] {count} mailbox(es) nearly full",
        "mailbox_quota_body": (
            "{count} mailbox(es) reached the alert threshold (Monitoring → Settings):\n\n{list}\n"
        ),
        "restore_test_failed": "[CarboRewind] Restore test failed ({source})",
        "restore_test_failed_body": (
            "The automatic restore test of {source} (backup #{seq}) into {target} failed: "
            "{restored} message(s) restored out of {selected}, {failed} in error.\n{error}\n"
            "Look at the job in CarboRewind (Jobs): a real restore could fail the same way.\n"
        ),
        "blacklisted": "[CarboRewind] Address {ip} blacklisted: {zone}",
        "blacklisted_body": (
            "The mail server address {ip} is listed in the blacklist {zone} (code {codes}).\n"
            "Some recipients may refuse your messages. Find the cause (compromised mailbox, "
            "mass mailing: see Mail traffic), fix it, then ask for removal on the list's "
            "site.\n"
        ),
        "blacklist_cleared": "[CarboRewind] Address {ip} removed from {zone}",
        "blacklist_cleared_body": "The address {ip} is no longer listed in {zone}.\n",
        "service_down": "[CarboRewind] Carbonio service down: {service}",
        "service_down_body": (
            "The service {service} has been down on the server of agent {agent} since "
            "{since}.\nIf it is Amavis or the MTA, messages wait in the queue (nothing is "
            'lost). Check with: su - zextras -c "zmcontrol status"; restart with: '
            'su - zextras -c "zmcontrol start".\n'
        ),
        "service_up": "[CarboRewind] Carbonio service back: {service}",
        "service_up_body": "The service {service} works again (down since {since}).\n",
        "mail_queue": "[CarboRewind] Mail queue: {count} message(s)",
        "mail_queue_body": (
            "The MTA queue ({agent}) holds {count} message(s) (threshold: {limit}): "
            "{detail}.\nA stopped service (Amavis…) or an unreachable recipient blocks "
            "delivery.\n"
        ),
        "archive_report": '[CarboRewind] Archiving "{policy}": {archived} message(s) archived',
        "archive_report_body": (
            'Run of the archive policy "{policy}" ({mode}) on {accounts} account(s):\n'
            "- messages selected: {candidates}\n"
            "- messages archived: {archived}\n"
            "- messages removed from the mailboxes: {removed} ({freed} GiB freed)\n"
            "- errors: {errors}; failed accounts: {failed}\n"
        ),
        "mode.simulate": "simulation",
        "mode.copy": "copy only",
        "mode.archive": "full archiving",
        "metric.sent_messages_hour": "messages sent in the hour",
        "metric.sent_recipients_hour": "recipients in the hour",
        "metric.sent_messages_day": "messages sent in 24 h",
        "metric.sent_recipients_day": "recipients in 24 h",
        "metric.spam_sent_day": "spam sent in 24 h",
        "action.alert": "alert only",
        "action.lock": "account locked",
        "action.alert_admin": "alert only (administrator account, never locked automatically)",
        "action.alert_cap": "alert only (too many automatic locks in the hour)",
        "target.primary": "copy 1 (repository)",
        "target.replica": "copy 2 (second NAS)",
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
    telegram = await load_telegram(db)
    channels = [
        (channel, recipients)
        for channel, wanted, recipients in (
            ("email", cfg.wants(event), list(cfg.recipients)),
            ("telegram", telegram.wants(event), list(telegram.chat_ids)),
        )
        if wanted
    ]
    if not channels:
        return False
    texts = TEXTS.get(cfg.language, TEXTS["fr"])
    safe = {k: _one_line(v) if k != "list" else str(v) for k, v in values.items()}
    for key in ("metric", "action", "target", "mode"):  # codes with a translated label
        if f"{key}.{safe.get(key)}" in texts:
            safe[key] = texts[f"{key}.{safe[key]}"]
    queued = False
    for channel, recipients in channels:
        dedup: str | None = (
            dedup_key if channel == "email" or not dedup_key else f"{dedup_key}:{channel}"
        )
        stmt = insert(Notification).values(
            event=event,
            subject=texts[event].format(**safe),
            body=texts[f"{event}_body"].format(**safe),
            recipients=recipients,
            channel=channel,
            dedup_key=dedup,
        )
        if dedup:
            stmt = stmt.on_conflict_do_nothing(index_elements=["dedup_key"])
        result = await db.execute(stmt.returning(Notification.id))
        queued = result.first() is not None or queued
    return queued


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


def send_any(
    settings: Settings, cfg: SmtpConfig, recipients: list[str], subject: str, body: str
) -> str | None:
    """Send through the main server, else through the fallback one.

    Returns None when the main server sent it, or the main server's error when the fallback
    did. Raises :class:`SmtpError` when both failed.
    """
    try:
        send_mail(settings, cfg, recipients, subject, body)
        return None
    except (SmtpError, SecretKeyError) as exc:
        if not cfg.has_fallback:
            raise SmtpError(str(exc)) from exc
        primary = str(exc)
    try:
        send_mail(settings, cfg.fallback(), recipients, subject, body)
    except (SmtpError, SecretKeyError) as exc:
        raise SmtpError(f"main server: {primary}; fallback server: {exc}") from exc
    log.warning("main SMTP server failed (%s), message sent by the fallback server", primary)
    return primary


class TelegramError(SmtpError):
    pass


def _telegram_client() -> httpx.Client:
    return httpx.Client(base_url=TELEGRAM_API, timeout=20)


def telegram_call(settings: Settings, token_enc: str, method: str, body: dict[str, Any]) -> Any:
    """Call the Bot API. Errors never carry the token (it is part of the URL)."""
    token = decrypt(settings, token_enc)
    try:
        with _telegram_client() as client:
            response = client.post(f"/bot{token}/{method}", json=body)
    except httpx.HTTPError as exc:
        raise TelegramError(f"Telegram unreachable ({type(exc).__name__})") from None
    try:
        data = response.json()
    except ValueError:
        raise TelegramError(f"Telegram answered HTTP {response.status_code}") from None
    if not data.get("ok"):
        description = str(data.get("description") or f"HTTP {response.status_code}")
        raise TelegramError(f"Telegram: {description.replace(token, '***')[:300]}")
    return data.get("result")


def send_telegram(settings: Settings, cfg: TelegramConfig, chat_ids: list[str], text: str) -> None:
    """Send one plain-text message to every chat (call from a thread)."""
    if not cfg.token_enc or not chat_ids:
        raise TelegramError("Telegram bot token and chat are required")
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - 1] + "…"
    errors = []
    for chat_id in chat_ids:
        try:
            telegram_call(
                settings,
                cfg.token_enc,
                "sendMessage",
                {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        except TelegramError as exc:
            errors.append(f"{chat_id}: {exc}")
    if errors:
        raise TelegramError("; ".join(errors))


async def deliver_pending(db: AsyncSession, settings: Settings, now: datetime) -> int:
    """Send due messages of the outbox; retries with backoff. Returns how many were sent."""
    cfg = await load_config(db)
    telegram = await load_telegram(db)
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
            if note.channel == "telegram":
                if not telegram.enabled:
                    raise TelegramError("Telegram notifications are disabled")
                await asyncio.to_thread(
                    send_telegram,
                    settings,
                    telegram,
                    list(note.recipients),
                    f"{note.subject}\n\n{note.body}",
                )
                primary_error = None
            else:
                if not cfg.enabled:
                    raise SmtpError("notifications are disabled")
                primary_error = await asyncio.to_thread(
                    send_any, settings, cfg, list(note.recipients), note.subject, note.body
                )
        except (SmtpError, SecretKeyError) as exc:
            note.error = str(exc)[:1000]
            if note.attempts >= MAX_ATTEMPTS:
                note.status = NotificationStatus.FAILED
            else:
                note.next_attempt_at = now + timedelta(minutes=2**note.attempts)
            log.warning("notification %s not sent: %s", note.id, exc)
            continue
        note.status, note.sent_at = NotificationStatus.SENT, now
        # Sent, but say so when the main server failed: someone should look at it.
        note.error = (
            f"sent by the fallback server; main: {primary_error}"[:1000]
            if (primary_error)
            else None
        )
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
