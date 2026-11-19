"""Archiving: old messages leave the mailboxes for the repository, and stay searchable.

* An :class:`~.db.models.ArchivePolicy` selects accounts (domains, accounts, exclusions,
  administrators left out by default) and messages (older than N days; Trash, Junk, Drafts and
  the policy's folders never; flagged / unread messages kept on request).
* Each run queues one ``archive`` job per account. The agent stores, reads back and indexes
  every message (``archive-stored``) **before** deleting anything, checks copy 2 when there
  is one, then reports what left the mailbox (``archive-complete``).
* ``archive`` mode (deletion) is only used once a simulation finished after the last change
  of the criteria; a stale simulation turns a scheduled run into a simulation.
* Archived messages are kept forever, or ``retention_days``; then their index rows go and the
  purge deletes the blobs no backup uses and the archive manifests nobody references.
* Reading a message for the interface is a ``fetch`` job: the agent sends the raw message,
  stored encrypted for one hour (:class:`~.db.models.FetchedContent`).
"""

from __future__ import annotations

import email
import email.policy
import html
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .config import Settings
from .db.models import (
    Account,
    AccountStatus,
    ActorType,
    ArchivedMessage,
    ArchivePolicy,
    Blob,
    FetchedContent,
    Job,
    JobKind,
    JobStatus,
    Outcome,
)
from .notifications import _fernet, notify
from .scheduling import Cron, CronError, zone

log = logging.getLogger(__name__)

PENDING = (JobStatus.QUEUED, JobStatus.RUNNING)
ARCHIVE_PRIORITY = 150  # after scheduled backups (100)
FETCH_PRIORITY = 10  # a user is waiting
MAX_SKIP_IDS = 200_000
MAX_FETCH_BYTES = 20 * 2**20
FETCH_TTL = timedelta(hours=1)
FETCH_JOBS_TTL = timedelta(days=1)
MAX_PURGE_ARCHIVES = 500
# Changing one of these makes the last simulation stale.
CRITERIA = (
    "domain_ids",
    "account_ids",
    "exclude_account_ids",
    "older_than_days",
    "exclude_folders",
    "keep_flagged",
    "keep_unread",
    "include_admins",
)


def simulation_valid(policy: ArchivePolicy) -> bool:
    return policy.simulated_at is not None and policy.simulated_at >= policy.criteria_changed_at


def effective_mode(policy: ArchivePolicy) -> str:
    """Mode a run really uses: never deletion on criteria nobody simulated."""
    if policy.mode == "archive" and not simulation_valid(policy):
        return "simulate"
    return policy.mode


def scope_query(policy: ArchivePolicy) -> Any:
    query = select(Account.id).where(
        Account.status.in_([AccountStatus.ACTIVE, AccountStatus.LOCKED])
    )
    if policy.domain_ids:
        query = query.where(Account.domain_id.in_([uuid.UUID(d) for d in policy.domain_ids]))
    if policy.account_ids:
        query = query.where(Account.id.in_([uuid.UUID(a) for a in policy.account_ids]))
    if policy.exclude_account_ids:
        query = query.where(Account.id.not_in([uuid.UUID(a) for a in policy.exclude_account_ids]))
    if not policy.include_admins:
        query = query.where(Account.is_admin.is_(False))
    return query


def cutoff(policy: ArchivePolicy, now: datetime) -> datetime:
    """Messages received before this instant (midnight UTC) qualify."""
    day = (now - timedelta(days=policy.older_than_days)).astimezone(UTC)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def job_params(policy: ArchivePolicy, mode: str, now: datetime, run: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "before": cutoff(policy, now).isoformat(),
        "exclude_folders": list(policy.exclude_folders),
        "keep_flagged": policy.keep_flagged,
        "keep_unread": policy.keep_unread,
        "max_messages": policy.max_per_run,
        "policy_id": str(policy.id),
        "policy": policy.name,
        "run": run,
    }


async def _busy(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.scalars(
        select(Job.account_id).where(
            Job.kind == JobKind.ARCHIVE, Job.status.in_(PENDING), Job.account_id.is_not(None)
        )
    )
    return {r for r in rows if r is not None}


async def enqueue_run(
    db: AsyncSession,
    policy: ArchivePolicy,
    mode: str,
    now: datetime,
    *,
    created_by: uuid.UUID | None = None,
    slot: str | None = None,
) -> tuple[str, int]:
    """Queue one job per account in scope (accounts already being archived are skipped)."""
    run = str(uuid.uuid4())
    busy = await _busy(db)
    accounts = [a for a in await db.scalars(scope_query(policy)) if a not in busy]
    if not accounts:
        return run, 0
    params = job_params(policy, mode, now, run)
    rows = [
        {
            "kind": JobKind.ARCHIVE,
            "status": JobStatus.QUEUED,
            "account_id": account,
            "created_by": created_by,
            "priority": ARCHIVE_PRIORITY,
            "max_attempts": 3,
            "params": params,
            "idempotency_key": f"archive:{policy.id}:{slot}:{account}" if slot else None,
        }
        for account in accounts
    ]
    result = await db.execute(
        insert(Job)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(Job.id)
    )
    return run, len(result.all())


async def enqueue_due(db: AsyncSession, now: datetime) -> tuple[int, list[str]]:
    """Scheduled runs of the enabled policies; returns (jobs queued, errors)."""
    queued, errors = 0, []
    policies = await db.scalars(
        select(ArchivePolicy).where(ArchivePolicy.enabled.is_(True)).order_by(ArchivePolicy.name)
    )
    for policy in policies:
        try:
            slot = Cron.parse(policy.schedule).last_slot(
                policy.last_run_at or policy.created_at, now, zone(policy.timezone)
            )
        except CronError as exc:
            errors.append(f"archive policy {policy.name}: {exc}")
            continue
        if slot is None:
            continue
        slot_utc = slot.astimezone(UTC)
        mode = effective_mode(policy)
        _, count = await enqueue_run(db, policy, mode, now, slot=f"{slot_utc:%Y%m%dT%H%M}")
        queued += count
        policy.last_run_at = slot_utc
        await audit.record(
            db,
            action="archive.policy.run",
            outcome=Outcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            actor_name="scheduler",
            target_type="archive_policy",
            target_id=str(policy.id),
            details={
                "policy": policy.name,
                "slot": slot_utc.isoformat(),
                "mode": mode,
                "configured_mode": policy.mode,
                "jobs": count,
            },
        )
    return queued, errors


async def skip_ids(db: AsyncSession, account_id: uuid.UUID) -> list[int]:
    """Messages of the account already archived and still in the mailbox (copy mode)."""
    rows = await db.scalars(
        select(ArchivedMessage.carbonio_item_id)
        .where(ArchivedMessage.account_id == account_id, ArchivedMessage.removed_at.is_(None))
        .limit(MAX_SKIP_IDS)
    )
    return sorted(set(rows))


async def record_stored(
    db: AsyncSession, job: Job, account: Account, entries: list[dict[str, Any]]
) -> int:
    """Index archived messages (idempotent); returns the number of new rows."""
    if not entries:
        return 0
    blobs: dict[str, int] = {}
    for e in entries:
        blobs[e["blob"]] = e["size"]
        blobs[e["meta_blob"]] = e.get("meta_size", 0)
    await db.execute(
        insert(Blob)
        .values([{"id": k, "size": v, "stored_size": v} for k, v in blobs.items()])
        .on_conflict_do_nothing(index_elements=["id"])
    )
    policy_id = job.params.get("policy_id")
    policy = await db.get(ArchivePolicy, uuid.UUID(policy_id)) if policy_id else None
    rows = [
        {
            "account_id": account.id,
            "policy_id": policy.id if policy else None,
            "job_id": job.id,
            "carbonio_item_id": e["item_id"],
            "folder_path": e["folder_path"],
            "subject": e.get("subject"),
            "sender": e.get("sender"),
            "recipients": e.get("recipients"),
            "item_date": e.get("date"),
            "size": e["size"],
            "has_attachments": bool(e.get("has_attachments")),
            "flags": e.get("flags") or "",
            "content_blob_id": e["blob"],
            "meta_blob_id": e["meta_blob"],
        }
        for e in entries
    ]
    result = await db.execute(
        insert(ArchivedMessage)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_archived_messages_item")
        .returning(ArchivedMessage.id)
    )
    return len(result.all())


async def mark_removed(
    db: AsyncSession, account: Account, item_ids: list[int], now: datetime
) -> int:
    if not item_ids:
        return 0
    result = await db.execute(
        update(ArchivedMessage)
        .where(
            ArchivedMessage.account_id == account.id,
            ArchivedMessage.carbonio_item_id.in_(item_ids),
            ArchivedMessage.removed_at.is_(None),
        )
        .values(removed_at=now)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def finish_run(db: AsyncSession, job: Job, now: datetime) -> None:
    """When the last job of a run ends: simulation bookkeeping and the report e-mail."""
    run = job.params.get("run")
    if not run:
        return
    jobs = list(
        await db.scalars(
            select(Job).where(Job.kind == JobKind.ARCHIVE, Job.params["run"].astext == run)
        )
    )
    if any(j.status in PENDING for j in jobs if j.id != job.id):
        return
    mode = str(job.params.get("mode"))
    ok = all(j.status is JobStatus.SUCCEEDED for j in jobs)
    policy_id = job.params.get("policy_id")
    policy = await db.get(ArchivePolicy, uuid.UUID(policy_id)) if policy_id else None
    if mode == "simulate" and ok and policy is not None:
        started = min((j.created_at for j in jobs), default=now)
        if started >= policy.criteria_changed_at:
            policy.simulated_at = now
    totals = {"candidates": 0, "archived": 0, "removed": 0, "removed_bytes": 0, "errors": 0}
    for j in jobs:
        p = j.progress or {}
        totals["candidates"] += int(p.get("candidates", 0))
        totals["archived"] += int(p.get("archived", 0))
        totals["removed"] += int(p.get("removed_count", 0))
        totals["removed_bytes"] += int(p.get("removed_bytes", 0))
        totals["errors"] += int(p.get("error_count", 0)) + (j.status is JobStatus.FAILED)
    await notify(
        db,
        "archive_report",
        dedup_key=f"archive:{run}",
        policy=job.params.get("policy", "?"),
        mode=mode,
        accounts=len(jobs),
        failed=sum(j.status is JobStatus.FAILED for j in jobs),
        freed=f"{totals['removed_bytes'] / 2**30:.2f}",
        **{k: v for k, v in totals.items() if k != "removed_bytes"},
    )


async def apply_retention(db: AsyncSession, now: datetime) -> int:
    """Forget archived messages older than their policy's retention."""
    restoring = await db.scalar(
        select(func.count()).where(Job.kind == JobKind.RESTORE, Job.status.in_(PENDING))
    )
    if restoring:  # a restore may be reading these blobs
        return 0
    deleted = 0
    policies = await db.scalars(select(ArchivePolicy).where(ArchivePolicy.retention_days > 0))
    for policy in policies:
        result = await db.execute(
            delete(ArchivedMessage).where(
                ArchivedMessage.policy_id == policy.id,
                ArchivedMessage.archived_at < now - timedelta(days=policy.retention_days),
            )
        )
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if count:
            deleted += count
            await audit.record(
                db,
                action="archive.retention",
                outcome=Outcome.SUCCESS,
                actor_type=ActorType.SYSTEM,
                actor_name="scheduler",
                target_type="archive_policy",
                target_id=str(policy.id),
                details={"policy": policy.name, "deleted": count},
            )
    return deleted


def purgeable_archives_query(limit: int) -> Any:
    """Archive manifests whose messages are all gone from the index."""
    referenced = exists().where(ArchivedMessage.job_id == Job.id)
    return (
        select(Job.id, Account.carbonio_id)
        .join(Account, Account.id == Job.account_id)
        .where(
            Job.kind == JobKind.ARCHIVE,
            Job.status == JobStatus.SUCCEEDED,
            Job.progress["manifest_ref"].astext.is_not(None),
            Job.progress["manifest_purged"].astext.is_(None),
            ~referenced,
        )
        .order_by(Job.finished_at)
        .limit(limit)
    )


async def purgeable_archives(
    db: AsyncSession, limit: int = MAX_PURGE_ARCHIVES
) -> list[dict[str, str]]:
    rows = await db.execute(purgeable_archives_query(limit))
    return [{"account": carbonio, "archive": str(job_id)} for job_id, carbonio in rows]


async def mark_archives_purged(db: AsyncSession, job_ids: list[str]) -> None:
    for job_id in job_ids:
        job = await db.get(Job, uuid.UUID(job_id))
        if job is not None and job.kind is JobKind.ARCHIVE:
            job.progress = {**(job.progress or {}), "manifest_purged": True}


async def prune_fetched(db: AsyncSession, now: datetime) -> None:
    await db.execute(delete(FetchedContent).where(FetchedContent.created_at < now - FETCH_TTL))
    await db.execute(
        delete(Job).where(
            Job.kind == JobKind.FETCH,
            Job.status.not_in(PENDING),
            Job.created_at < now - FETCH_JOBS_TTL,
        )
    )


# ------------------------------------------------------------------ reading a message


def _content_key(settings: Settings) -> Fernet:
    return _fernet(settings)


def seal_content(settings: Settings, data: bytes) -> bytes:
    return _content_key(settings).encrypt(data)


def open_content(settings: Settings, sealed: bytes) -> bytes | None:
    try:
        return _content_key(settings).decrypt(sealed)
    except InvalidToken:
        return None


class _TextOnly(HTMLParser):
    """HTML to plain text: no tag, link, image or script ever reaches the browser."""

    SKIP = frozenset({"script", "style", "head", "title"})
    BREAK = frozenset({"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextOnly()
    parser.feed(markup)
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t\r\f\v]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


MAX_TEXT = 200_000


def parse_message(raw: bytes) -> dict[str, Any]:
    """Headers, plain text and attachment list of a message, for display only."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)  # noqa: S101 - guaranteed by policy.default

    def header(name: str) -> str | None:
        try:
            value = msg.get(name)
        except (ValueError, TypeError, IndexError):
            return None
        return str(value)[:2000] if value else None

    text, html_only = "", False
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is not None:
            content = body.get_content()
            if body.get_content_type() == "text/html":
                text, html_only = html_to_text(str(content)), True
            else:
                text = str(content)
    except (LookupError, ValueError, TypeError, AttributeError):
        text = ""
    attachments = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_payload(decode=True) or b""
        except (ValueError, TypeError, AssertionError):
            payload = b""
        attachments.append(
            {
                "name": (part.get_filename() or "")[:255] or None,
                "content_type": part.get_content_type(),
                "size": len(payload) if isinstance(payload, bytes) else 0,
            }
        )
    return {
        "subject": header("Subject"),
        "from": header("From"),
        "to": header("To"),
        "cc": header("Cc"),
        "date": header("Date"),
        "text": text[:MAX_TEXT],
        "truncated": len(text) > MAX_TEXT,
        "html_only": html_only,
        "attachments": attachments[:100],
        "size": len(raw),
    }
