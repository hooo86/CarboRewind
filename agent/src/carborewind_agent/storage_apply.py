"""Apply the storage settings chosen in the web interface (runs as root).

The agent runs unprivileged and cannot mount a share. When the settings change, it writes
them to ``/var/lib/carborewind/storage/request.json``; the ``carborewind-storage.path`` unit
notices the file and starts ``carborewind-agent storage apply`` as root, which:

1. reads the request **defensively** (no symlink, regular file owned by the agent's user,
   size bounded) and deletes it at once — the passwords then only exist in
   ``/etc/carborewind/smb*.credentials`` (root, 0600);
2. validates everything again: the agent's user is not trusted with root's decisions, so
   only the two fixed mount points (``nas`` and ``copy02`` below ``/var/lib/carborewind``)
   and the fixed local repository directory can be used;
3. applies only the sections whose revision changed (copy 1 = the repository, copy 2 = its
   mirror), initialises an empty repository on a new share, and restarts the agent;
4. writes ``status.json`` (root:carborewind 0640, no secret) that the agent reports to the
   server, so the interface shows the outcome.
"""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import (
    AGENT_USER,
    COPY_MOUNT,
    DEFAULT_MOUNT,
    ETC,
    SYSTEMD,
    SmbRequest,
    StorageError,
    _write,
    configure_local,
    configure_replica,
    configure_smb,
    copy_repository,
    describe,
    is_repository,
    read_env,
    remove_replica,
)

STORAGE_DIR = Path("/var/lib/carborewind/storage")
LOCAL_REPOSITORY = Path("/var/lib/carborewind/repository")
MAX_REQUEST = 64 * 1024
REPO_KEY = "CARBOREWIND_AGENT_REPOSITORY_PATH"
REPLICA_KEY = "CARBOREWIND_AGENT_REPLICA_PATH"
ROOT_UID = 0


def _run(*cmd: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise StorageError(f"{' '.join(cmd)}: {(result.stderr or result.stdout).strip()}")


def read_request(path: Path, owner_uid: int) -> dict[str, Any]:
    """Read and delete the request, refusing anything but a small regular file of the agent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise StorageError(f"cannot open the request: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StorageError("the request is not a regular file")
        if info.st_uid not in (owner_uid, ROOT_UID):
            raise StorageError("the request does not belong to the agent's user")
        if info.st_size > MAX_REQUEST:
            raise StorageError("the request is too large")
        data = os.read(fd, MAX_REQUEST + 1)
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()
    try:
        request = json.loads(data)
    except ValueError as exc:
        raise StorageError("the request is not valid JSON") from exc
    if not isinstance(request, dict):
        raise StorageError("the request is not a JSON object")
    return request


def _text(section: dict[str, Any], key: str, required: bool = True) -> str:
    value = section.get(key, "")
    if not isinstance(value, str) or (required and not value):
        raise StorageError(f"invalid or missing {key}")
    return value


def _smb(section: dict[str, Any], mount_point: Path, migrate: bool = False) -> SmbRequest:
    return SmbRequest(
        share=_text(section, "share"),
        username=_text(section, "username"),
        password=_text(section, "password"),
        domain=_text(section, "domain", required=False),
        mount_point=mount_point,
        seal=section.get("seal") is True,
        migrate=migrate,
        start_empty=not migrate,
    )


def _chown_tree(root: Path, user: pwd.struct_passwd) -> None:
    for dirpath, _dirs, files in os.walk(root):
        os.chown(dirpath, user.pw_uid, user.pw_gid)
        for name in files:
            os.chown(Path(dirpath) / name, user.pw_uid, user.pw_gid)


def _init_repository(path: Path, env_file: Path, user: pwd.struct_passwd) -> None:
    from .repository import Repository
    from .repository.keyfile import read_kek

    default = str(ETC / "repository.key")
    key = Path(read_env(env_file, "CARBOREWIND_AGENT_REPOSITORY_KEY_FILE", default))
    Repository.init(path, read_kek(key))
    _chown_tree(path, user)


def _write_status(path: Path, status: dict[str, Any], gid: int) -> None:
    """Atomic write into a directory the agent owns: a fresh name, never following links."""
    tmp = path.parent / f".status-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.write(fd, json.dumps(status, default=str).encode())
        os.fchown(fd, ROOT_UID, gid)
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _load_state(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int)}


def apply(
    request: dict[str, Any],
    env_file: Path,
    *,
    etc: Path = ETC,
    systemd: Path = SYSTEMD,
    run: Any = None,
    primary_mount: Path = DEFAULT_MOUNT,
    copy_mount: Path = COPY_MOUNT,
    local_repository: Path = LOCAL_REPOSITORY,
) -> dict[str, Any]:
    """Apply the changed sections; returns the status (raises StorageError on failure)."""
    runner = run if callable(run) else _run
    user = pwd.getpwnam(AGENT_USER)
    state_file = etc / "storage-applied.json"
    state = _load_state(state_file)
    revisions = {k: int(request.get(f"{k}_revision") or 0) for k in ("primary", "copy")}
    changed: list[str] = []
    current = Path(read_env(env_file, REPO_KEY, str(local_repository)))

    primary = request.get("primary")
    if revisions["primary"] and revisions["primary"] != state.get("primary"):
        if not isinstance(primary, dict) or primary.get("kind") not in ("local", "smb"):
            raise StorageError("invalid primary storage")
        migrate = primary.get("migrate") is not False
        if primary["kind"] == "smb":
            target = primary_mount / "repository"
            configure_smb(
                _smb(primary, primary_mount, migrate),
                env_file,
                current,
                etc,
                systemd,
                runner,
                remount=current == target,
            )
            if not is_repository(target):
                _init_repository(target, env_file, user)
        else:
            target = local_repository
            if current != target and migrate and is_repository(current):
                copy_repository(current, target)
                _chown_tree(target, user)
            configure_local(target, env_file, systemd, runner)
            if not is_repository(target):
                target.mkdir(parents=True, exist_ok=True)
                _init_repository(target, env_file, user)
        state["primary"] = revisions["primary"]
        changed.append("primary")

    copy = request.get("copy")
    if revisions["copy"] and revisions["copy"] != state.get("copy"):
        if copy is None:
            remove_replica(copy_mount, env_file, etc, systemd, runner)
        elif isinstance(copy, dict):
            already = read_env(env_file, REPLICA_KEY, "") == str(copy_mount / "repository")
            configure_replica(
                _smb(copy, copy_mount), env_file, etc, systemd, runner, remount=already
            )
        else:
            raise StorageError("invalid copy 02 storage")
        state["copy"] = revisions["copy"]
        changed.append("copy")

    etc.mkdir(parents=True, exist_ok=True)
    _write(state_file, json.dumps(state), 0o600)  # atomic: never follows a planted link
    if changed:
        runner("systemctl", "restart", "--no-block", "carborewind-agent.service")
    repository = Path(read_env(env_file, REPO_KEY, str(local_repository)))
    replica = read_env(env_file, REPLICA_KEY, "")
    return {
        "changed": changed,
        "repository": describe(repository),
        "replica": describe(Path(replica)) if replica else None,
    }


def apply_command(
    storage_dir: Path = STORAGE_DIR, env_file: Path = Path("/etc/carborewind/agent.env"), **kw: Any
) -> int:
    """Entry point of ``carborewind-agent storage apply`` (root, from the path unit)."""
    if os.geteuid() != 0:
        raise StorageError("run this command as root (sudo)")
    user = pwd.getpwnam(AGENT_USER)
    # /etc/carborewind is writable by the agent's group: refuse a substituted env file.
    info = os.lstat(env_file)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID:
        raise StorageError(f"{env_file} must be a regular file owned by root")
    request_file = storage_dir / "request.json"
    if not request_file.exists() and not request_file.is_symlink():
        return 0  # spurious trigger
    status: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
    try:
        request = read_request(request_file, user.pw_uid)
        status["revision"] = int(request.get("revision") or 0)
        status.update(apply(request, env_file, **kw))
        status["ok"] = True
    except (StorageError, OSError, ValueError) as exc:
        status.update(ok=False, error=str(exc)[:500])
    _write_status(storage_dir / "status.json", status, user.pw_gid)
    return 0 if status["ok"] else 1
