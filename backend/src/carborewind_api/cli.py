"""Administrative command line: ``python -m carborewind_api.cli``.

Used to bootstrap the first super-admin (local account). The password is read from the
terminal (or stdin when not a TTY), never from the command line, so it does not end up in the
shell history or the process list.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import audit
from .auth.service import normalise_username
from .config import get_settings
from .db.models import (
    Account,
    AccountStatus,
    ActorType,
    Agent,
    AgentStatus,
    AuthSource,
    BackupType,
    Job,
    JobKind,
    JobStatus,
    Outcome,
    Role,
    RoleAssignment,
    User,
)
from .db.session import make_engine
from .pki import PkiError, init_ca, token_digest
from .security.passwords import WeakPasswordError, hash_password


def _read_password() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Confirm: ") != first:
        raise SystemExit("passwords do not match")
    return first


async def create_user(username: str, role: Role, password: str) -> None:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("CARBOREWIND_DATABASE_URL is not set")
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as db, db.begin():
            user = User(
                username=normalise_username(username),
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(password),
                roles=[RoleAssignment(role=role)],
            )
            db.add(user)
            await db.flush()
            await audit.record(
                db,
                action="user.create",
                outcome=Outcome.SUCCESS,
                actor_type=ActorType.SYSTEM,
                actor_name="cli",
                target_type="user",
                target_id=str(user.id),
                details={"username": user.username, "role": role.value},
            )
    finally:
        await engine.dispose()


async def audit_verify() -> int:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("CARBOREWIND_DATABASE_URL is not set")
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as db:
            result = await audit.verify_chain(db)
    finally:
        await engine.dispose()
    if result.ok:
        print(f"audit chain OK: {result.checked} events, head {result.head_id} {result.head_hash}")
        return 0
    print(f"audit chain BROKEN at event {result.broken_id}: {result.reason}", file=sys.stderr)
    return 1


def _sessionmaker() -> tuple[Any, async_sessionmaker[Any]]:
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("CARBOREWIND_DATABASE_URL is not set")
    engine = make_engine(settings.database_url.get_secret_value())
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def agent_create(name: str, hostname: str) -> tuple[str, str]:
    """Register an agent; returns (agent id, one-time enrolment token)."""
    settings = get_settings()
    engine, factory = _sessionmaker()
    token = secrets.token_urlsafe(32)
    try:
        async with factory() as db, db.begin():
            agent = Agent(
                name=name,
                hostname=hostname,
                status=AgentStatus.PENDING,
                enrollment_token_hash=token_digest(token),
                enrollment_expires_at=datetime.now(UTC)
                + timedelta(minutes=settings.agent_enrollment_minutes),
            )
            db.add(agent)
            await db.flush()
            await audit.record(
                db,
                action="agent.create",
                outcome=Outcome.SUCCESS,
                actor_type=ActorType.SYSTEM,
                actor_name="cli",
                target_type="agent",
                target_id=str(agent.id),
                details={"name": name, "hostname": hostname},
            )
            return str(agent.id), token
    finally:
        await engine.dispose()


async def backup_request(email: str, full: bool) -> str:
    """Queue a backup of one account (same job as POST /api/backups)."""
    engine, factory = _sessionmaker()
    try:
        async with factory() as db, db.begin():
            account = await db.scalar(
                select(Account).where(
                    func.lower(Account.email) == email.lower(),
                    Account.status != AccountStatus.DELETED,
                )
            )
            if account is None:
                raise SystemExit(f"unknown account {email} (has the agent synced the inventory?)")
            backup_type = BackupType.FULL if full else BackupType.INCREMENTAL
            job = Job(
                kind=JobKind.BACKUP,
                status=JobStatus.QUEUED,
                account_id=account.id,
                priority=50,
                params={"backup_type": backup_type.value},
            )
            db.add(job)
            await db.flush()
            await audit.record(
                db,
                action="backup.request",
                outcome=Outcome.SUCCESS,
                actor_type=ActorType.SYSTEM,
                actor_name="cli",
                target_type="account",
                target_id=account.email,
                domain_id=account.domain_id,
                details={"job": str(job.id), "type": backup_type.value},
            )
            return str(job.id)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carborewind-api")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="create a local user with a global role")
    create.add_argument("username")
    create.add_argument("--role", choices=[r.value for r in Role], default=Role.SUPER_ADMIN.value)
    sub.add_parser("audit-verify", help="recompute the audit hash chain (exit 1 if broken)")
    sub.add_parser("pki-init", help="create the agents' certificate authority")
    agent = sub.add_parser("agent-create", help="register an agent, print its enrolment token")
    agent.add_argument("name")
    agent.add_argument("hostname")
    backup = sub.add_parser("backup", help="queue a backup of one account")
    backup.add_argument("email")
    backup.add_argument("--full", action="store_true", help="force a full backup")
    args = parser.parse_args(argv)
    if args.command == "agent-create":
        agent_id, token = asyncio.run(agent_create(args.name, args.hostname))
        print(
            f"agent {agent_id} registered; one-time enrolment token (valid "
            f"{get_settings().agent_enrollment_minutes} min):\n{token}"
        )
        return 0
    if args.command == "backup":
        print(f"backup job {asyncio.run(backup_request(args.email, args.full))} queued")
        return 0
    if args.command == "audit-verify":
        return asyncio.run(audit_verify())
    if args.command == "pki-init":
        directory = get_settings().pki_dir
        try:
            init_ca(directory)
        except PkiError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"CA created in {directory} — configure the proxy with {directory}/ca.crt")
        return 0
    try:
        asyncio.run(create_user(args.username, Role(args.role), _read_password()))
    except WeakPasswordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"user {normalise_username(args.username)} created")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
