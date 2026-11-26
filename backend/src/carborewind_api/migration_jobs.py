"""Agent side of migrations: what a ``migrate`` job carries, and what its result changes.

The IMAP credentials are decrypted only here, when an agent claims the job, and travel in
the claim answer (mutual TLS); they are never stored in the job row.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings
from .db.models import (
    Job,
    JobKind,
    JobStatus,
    MigrationAuth,
    MigrationBatch,
    MigrationMailbox,
    MigrationSource,
)
from .notifications import SecretKeyError, decrypt, notify


class MigrationSpecError(Exception):
    """The job cannot run (mailbox removed, password missing or undecryptable)."""


async def job_spec(db: AsyncSession, settings: Settings, job: Job) -> dict[str, Any]:
    """``migration`` field of the claim answer."""
    mailbox = await db.get(MigrationMailbox, uuid.UUID(str(job.params.get("mailbox_id"))))
    if mailbox is None:
        raise MigrationSpecError("mailbox removed from its migration batch")
    batch = await db.get(MigrationBatch, mailbox.batch_id)
    source = await db.get(MigrationSource, batch.source_id) if batch else None
    if batch is None or source is None:  # pragma: no cover - FKs
        raise MigrationSpecError("migration batch removed")
    login = mailbox.source_login or mailbox.source_email
    try:
        if source.auth_mode is MigrationAuth.PASSWORD:
            if not mailbox.password_enc:
                raise MigrationSpecError(f"no password for {login}")
            auth = {
                "auth": "login",
                "username": login,
                "password": decrypt(settings, mailbox.password_enc),
            }
        else:
            if not source.master_password_enc or not source.master_username:
                raise MigrationSpecError("the source has no master user password")
            password = decrypt(settings, source.master_password_enc)
            if source.auth_mode is MigrationAuth.MASTER_PLAIN:
                auth = {
                    "auth": "plain",
                    "username": source.master_username,
                    "password": password,
                    "authzid": login,
                }
            else:
                auth = {
                    "auth": "login",
                    "username": f"{login}{source.master_separator}{source.master_username}",
                    "password": password,
                }
    except SecretKeyError as exc:
        raise MigrationSpecError(str(exc)) from exc
    options = dict(batch.options or {})
    if mailbox.root:
        options["root"] = mailbox.root
    return {
        "imap": {
            "host": source.host,
            "port": source.port,
            "security": source.security.value,
            "verify_tls": source.verify_tls,
            **auth,
        },
        "options": options,
        "state": mailbox.state or {},
        "dry_run": bool(job.params.get("dry_run")),
    }


def record_result(mailbox: MigrationMailbox, result: dict[str, Any], now: datetime) -> None:
    """Keep the new incremental state and the figures shown in the interface."""
    folders = result.get("folders") or []
    if result.get("dry_run"):
        mailbox.stats = {
            **(mailbox.stats or {}),
            "plan": {
                "messages": sum(int(f.get("new", 0)) for f in folders),
                "total": sum(int(f.get("total", 0)) for f in folders),
                "folders": len(folders),
                "at": now.isoformat(),
            },
        }
        return
    previous = mailbox.stats or {}
    mailbox.state = result.get("state") or {}
    mailbox.stats = {
        **{k: v for k, v in previous.items() if k == "plan"},
        "copied": int(result.get("copied", 0)),
        "duplicates": int(result.get("duplicates", 0)),
        "failed": int(result.get("failed_count", 0)),
        "bytes": int(result.get("bytes", 0)),
        "folders": len(folders),
        "cancelled": bool(result.get("cancelled")),
        "total_copied": int(previous.get("total_copied", 0)) + int(result.get("copied", 0)),
        "total_bytes": int(previous.get("total_bytes", 0)) + int(result.get("bytes", 0)),
        "finished_at": now.isoformat(),
    }
    mailbox.last_run_at = now


async def notify_batch_done(db: AsyncSession, job: Job) -> bool:
    """Queue ``migration_done`` when the last copy of a batch is over."""
    batch_id = job.params.get("batch_id")
    if job.kind is not JobKind.MIGRATE or not batch_id or job.params.get("dry_run"):
        return False
    remaining = await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.kind == JobKind.MIGRATE,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            Job.params["batch_id"].astext == str(batch_id),
            Job.id != job.id,
        )
    )
    batch = await db.get(MigrationBatch, uuid.UUID(str(batch_id)))
    if remaining or batch is None:
        return False
    rows = (
        await db.execute(
            select(MigrationMailbox, Job)
            .join(Job, Job.id == MigrationMailbox.last_job_id)
            .where(MigrationMailbox.batch_id == batch.id)
        )
    ).all()
    done = failed = copied = errors = 0
    for mailbox, last in rows:
        if last.params.get("dry_run"):
            continue
        stats = mailbox.stats or {}
        if last.status is JobStatus.SUCCEEDED:
            done += 1
        elif last.status is JobStatus.FAILED:
            failed += 1
        copied += int(stats.get("total_copied", 0))
        errors += int(stats.get("failed", 0))
    return await notify(
        db,
        "migration_done",
        dedup_key=f"migration_done:{batch.id}:{job.id}",
        batch=batch.name,
        done=done,
        failed=failed,
        copied=copied,
        errors=errors,
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
