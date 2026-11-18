"""Append-only, hash-chained audit trail.

``hash = SHA-256(prev_hash || canonical_json(event))`` where the canonical JSON has sorted keys
and no whitespace. Writers serialise on a transaction-scoped advisory lock so the chain stays
linear; the lock is released at commit/rollback. Verification and export arrive with #5.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .db.models import ActorType, AuditEvent, Outcome

GENESIS = "0" * 64
# Arbitrary constant identifying the audit-chain advisory lock.
_LOCK_KEY = 0x0CA4B0AE


def canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def chain_hash(prev_hash: str, event: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(event)).encode()).hexdigest()


def event_payload(e: AuditEvent) -> dict[str, Any]:
    """Fields covered by the hash (everything except id, prev_hash and hash)."""
    return {
        "ts": e.ts.astimezone(UTC).isoformat(),
        "actor_type": ActorType(e.actor_type).value,
        "actor_id": e.actor_id,
        "actor_name": e.actor_name,
        "action": e.action,
        "target_type": e.target_type,
        "target_id": e.target_id,
        "domain_id": str(e.domain_id) if e.domain_id else None,
        "outcome": Outcome(e.outcome).value,
        "source_ip": str(e.source_ip) if e.source_ip else None,
        "details": e.details,
    }


async def record(
    db: AsyncSession,
    *,
    action: str,
    outcome: Outcome,
    actor_type: ActorType = ActorType.USER,
    actor_id: str | None = None,
    actor_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    domain_id: uuid.UUID | None = None,
    source_ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event to the chain (within the caller's transaction)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    prev = await db.scalar(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1))
    event = AuditEvent(
        # Set client-side (not server default) so the hashed value is the stored value;
        # timestamptz keeps microseconds, so the round-trip is exact.
        ts=datetime.now(UTC),
        actor_type=actor_type,
        actor_id=actor_id,
        actor_name=actor_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        domain_id=domain_id,
        outcome=outcome,
        source_ip=source_ip,
        details=details or {},
        prev_hash=prev or GENESIS,
        hash="",
    )
    event.hash = chain_hash(event.prev_hash, event_payload(event))
    db.add(event)
    await db.flush()
    return event
