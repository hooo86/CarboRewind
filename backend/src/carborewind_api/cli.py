"""Administrative command line: ``python -m carborewind_api.cli``.

Used to bootstrap the first super-admin (local account). The password is read from the
terminal (or stdin when not a TTY), never from the command line, so it does not end up in the
shell history or the process list.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import async_sessionmaker

from . import audit
from .auth.service import normalise_username
from .config import get_settings
from .db.models import ActorType, AuthSource, Outcome, Role, RoleAssignment, User
from .db.session import make_engine
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carborewind-api")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="create a local user with a global role")
    create.add_argument("username")
    create.add_argument("--role", choices=[r.value for r in Role], default=Role.SUPER_ADMIN.value)
    args = parser.parse_args(argv)
    try:
        asyncio.run(create_user(args.username, Role(args.role), _read_password()))
    except WeakPasswordError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"user {normalise_username(args.username)} created")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
