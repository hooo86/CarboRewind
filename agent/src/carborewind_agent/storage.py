"""Where the repository lives: local disk, or an SMB/CIFS share (NAS, Windows server).

``carborewind-agent storage smb`` (run once, as root) prepares a network share:

1. writes the credentials to ``/etc/carborewind/smb.credentials`` (root, 0600): the password
   is read from the terminal or a file, never from the command line;
2. writes a systemd mount unit for ``/var/lib/carborewind/nas`` (files owned by the
   ``carborewind`` user, ``nosuid,nodev,noexec``, SMB 3) and makes the agent service require
   it (``RequiresMountsFor``), so backups never land on the local disk by mistake;
3. mounts it and checks that the agent's user can create, rename and delete a file;
4. points ``CARBOREWIND_AGENT_REPOSITORY_PATH`` at ``<mount>/repository``. An existing
   repository is either copied there (``--migrate``) or explicitly left behind
   (``--start-empty``): without one of them nothing is switched, because the snapshots of
   the old repository would no longer be restorable.

Data is already encrypted by the agent before it reaches the share; ``--seal`` additionally
asks for SMB 3 transport encryption.
"""

from __future__ import annotations

import getpass
import os
import pwd
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ETC = Path("/etc/carborewind")
SYSTEMD = Path("/etc/systemd/system")
DEFAULT_MOUNT = Path("/var/lib/carborewind/nas")
COPY_MOUNT = Path("/var/lib/carborewind/copy02")
NETWORK_FS = frozenset({"cifs", "smb3", "nfs", "nfs4"})
# The agent's systemd sandbox can only write under this directory.
MOUNT_ROOT = "/var/lib/carborewind/"
AGENT_USER = "carborewind"
SHARE_RE = re.compile(r"^//[A-Za-z0-9._-]+(/[^/\x00-\x1f\\,]+)+/?$")
SAFE_FIELD = re.compile(r"^[^\x00-\x1f,=\\]*$")


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class MountInfo:
    mount_point: str
    source: str
    fstype: str


def mount_info(path: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> MountInfo | None:
    """Mount that contains ``path`` (longest mount point prefix)."""
    target = os.path.realpath(path)
    best: MountInfo | None = None
    try:
        lines = mountinfo.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split(" - ")
        if len(parts) != 2:
            continue
        left, right = parts[0].split(), parts[1].split()
        if len(left) < 5 or len(right) < 2:
            continue
        point = left[4].replace("\\040", " ")
        if (target == point or target.startswith(point.rstrip("/") + "/")) and (
            best is None or len(point) > len(best.mount_point)
        ):
            best = MountInfo(point, right[1], right[0])
    return best


def describe(path: Path) -> dict[str, object]:
    """Storage summary sent with the inventory and shown by ``storage status``."""
    info = mount_info(path)
    out: dict[str, object] = {
        "path": str(path),
        "kind": "network" if info and info.fstype in NETWORK_FS else "local",
        "fstype": info.fstype if info else None,
        "source": info.source if info else None,
    }
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        out["error"] = "unreachable"
        return out
    out.update(total_bytes=usage.total, free_bytes=usage.free)
    return out


def systemd_escape_path(path: Path) -> str:
    """Same as ``systemd-escape --path``: the unit name of a mount point."""
    text = str(path).strip("/")
    if not text:
        return "-"
    out = []
    for i, ch in enumerate(text):
        if ch == "/":
            out.append("-")
        elif (ch.isalnum() and ch.isascii()) or ch in ":_" or (ch == "." and i > 0):
            out.append(ch)
        else:
            out.extend(f"\\x{b:02x}" for b in ch.encode())
    return "".join(out)


def mount_unit(
    share: str, mount_point: Path, credentials: Path, uid: int, gid: int, seal: bool
) -> str:
    options = [
        f"credentials={credentials}",
        f"uid={uid}",
        f"gid={gid}",
        "file_mode=0600",
        "dir_mode=0700",
        "vers=3.0",
        "nosuid",
        "nodev",
        "noexec",
        "_netdev",
        "serverino",
    ]
    if seal:
        options.append("seal")
    return (
        "# Written by carborewind-agent storage smb — do not edit, run the command again.\n"
        "[Unit]\n"
        "Description=CarboRewind backup repository (SMB)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Mount]\n"
        f"What={share}\n"
        f"Where={mount_point}\n"
        "Type=cifs\n"
        f"Options={','.join(options)}\n"
        "TimeoutSec=60\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_env(env_file: Path, key: str, default: str) -> str:
    """Value of ``key`` in the agent's environment file (sudo does not load it)."""
    try:
        lines = env_file.read_text().splitlines()
    except OSError:
        return default
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name == key:
            return value.strip().strip('"').strip("'")
    return default


def set_env(env_file: Path, key: str, value: str) -> None:
    """Set ``key=value`` in the agent's environment file, keeping everything else."""
    lines = env_file.read_text().splitlines() if env_file.exists() else []
    done = False
    for i, line in enumerate(lines):
        if re.match(rf"^\s*#?\s*{re.escape(key)}=", line):
            lines[i] = f"{key}={value}"
            done = True
            break
    if not done:
        lines.append(f"{key}={value}")
    # Keep the mode the package gave it (0640 root:carborewind); new files stay private.
    mode = env_file.stat().st_mode & 0o777 if env_file.exists() else 0o600
    _write(env_file, "\n".join(lines) + "\n", mode)


def unset_env(env_file: Path, key: str) -> None:
    if not env_file.exists():
        return
    lines = [
        line
        for line in env_file.read_text().splitlines()
        if not re.match(rf"^\s*{re.escape(key)}=", line)
    ]
    _write(env_file, "\n".join(lines) + "\n", env_file.stat().st_mode & 0o777)


def _run(*cmd: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if result.returncode != 0:
        raise StorageError(f"{' '.join(cmd)}: {(result.stderr or result.stdout).strip()}")


def check_writable(directory: Path, uid: int, gid: int) -> None:
    """Create, rename and delete a file as the agent's user would (files are owned by it)."""
    probe = directory / f".carborewind-write-test-{uuid.uuid4().hex}"
    renamed = probe.with_suffix(".renamed")
    try:
        probe.write_bytes(b"carborewind")
        os.replace(probe, renamed)
        stat = renamed.stat()
        if stat.st_uid != uid:
            raise StorageError(
                f"files on the share belong to uid {stat.st_uid}, not {AGENT_USER} ({uid})"
            )
        renamed.unlink()
    except OSError as exc:
        raise StorageError(f"the share is not writable: {exc}") from exc
    finally:
        for leftover in (probe, renamed):
            leftover.unlink(missing_ok=True)


def is_repository(path: Path) -> bool:
    return (path / "config.json").exists()


def copy_repository(source: Path, target: Path) -> int:
    """Copy every file of ``source`` that ``target`` lacks; returns the number copied."""
    copied = 0
    for root, _dirs, files in os.walk(source):
        rel = Path(root).relative_to(source)
        (target / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.startswith(".tmp") or rel.parts[:1] == ("locks",):
                continue
            dest = target / rel / name
            if not dest.exists():
                shutil.copy2(Path(root) / name, dest)
                copied += 1
    (target / "locks").mkdir(exist_ok=True)
    return copied


@dataclass
class SmbRequest:
    share: str
    username: str
    password: str
    domain: str = ""
    mount_point: Path = DEFAULT_MOUNT
    seal: bool = False
    migrate: bool = False
    start_empty: bool = False


def _validate(req: SmbRequest) -> pwd.struct_passwd:
    if os.geteuid() != 0:
        raise StorageError("run this command as root (sudo)")
    if shutil.which("mount.cifs") is None:
        raise StorageError("mount.cifs is missing: apt install cifs-utils")
    if not SHARE_RE.match(req.share) or any(part in {".", ".."} for part in req.share.split("/")):
        raise StorageError("share must look like //server/share[/folder]")
    for name, value in (("username", req.username), ("domain", req.domain)):
        if not SAFE_FIELD.match(value) or "\n" in value:
            raise StorageError(f"invalid {name}")
    if not req.username or not req.password or "\n" in req.password:
        raise StorageError("username and password are required")
    if not str(req.mount_point).startswith(MOUNT_ROOT):
        raise StorageError(f"the mount point must be under {MOUNT_ROOT}")
    try:
        return pwd.getpwnam(AGENT_USER)
    except KeyError as exc:
        raise StorageError(f"user {AGENT_USER} does not exist (is the agent installed?)") from exc


def _mount(
    req: SmbRequest,
    user: pwd.struct_passwd,
    credentials: Path,
    systemd: Path,
    runner: Any,
    remount: bool = False,
) -> str:
    """Write the credentials file and the mount unit, mount, check the agent can write."""
    lines = [f"username={req.username}", f"password={req.password}"]
    if req.domain:
        lines.append(f"domain={req.domain}")
    _write(credentials, "\n".join(lines) + "\n", 0o600)
    unit_name = systemd_escape_path(req.mount_point) + ".mount"
    _write(
        systemd / unit_name,
        mount_unit(req.share, req.mount_point, credentials, user.pw_uid, user.pw_gid, req.seal),
        0o600,  # systemd reads units as root
    )
    req.mount_point.mkdir(parents=True, exist_ok=True)
    runner("systemctl", "daemon-reload")
    runner("systemctl", "enable", "--now", unit_name)
    if remount:  # new credentials or share on an already mounted unit
        runner("systemctl", "restart", unit_name)
    check_writable(req.mount_point, user.pw_uid, user.pw_gid)
    return unit_name


def configure_smb(
    req: SmbRequest,
    env_file: Path,
    current_repository: Path,
    etc: Path = ETC,
    systemd: Path = SYSTEMD,
    run: object = None,
    *,
    remount: bool = False,
) -> dict[str, object]:
    """Do steps 1-4 of the module docstring; returns what was done (for display)."""
    runner = run if callable(run) else _run
    user = _validate(req)
    target = req.mount_point / "repository"
    if (
        is_repository(current_repository)
        and current_repository != target
        and not (req.migrate or req.start_empty)
    ):
        raise StorageError(
            f"{current_repository} holds a repository: add --migrate to copy it to the "
            "share, or --start-empty to leave it behind (its snapshots would no longer be "
            "restorable)"
        )
    _write(
        systemd / "carborewind-agent.service.d" / "storage.conf",
        "# Written by carborewind-agent storage smb.\n"
        "[Unit]\n"
        f"RequiresMountsFor={req.mount_point}\n",
        0o600,  # systemd reads units as root
    )
    unit_name = _mount(req, user, etc / "smb.credentials", systemd, runner, remount)

    copied = 0
    if is_repository(current_repository) and req.migrate and current_repository != target:
        copied = copy_repository(current_repository, target)
    target.mkdir(exist_ok=True)
    set_env(env_file, "CARBOREWIND_AGENT_REPOSITORY_PATH", str(target))
    return {
        "unit": unit_name,
        "repository": str(target),
        "copied_files": copied,
        "initialised": is_repository(target),
    }


def configure_replica(
    req: SmbRequest,
    env_file: Path,
    etc: Path = ETC,
    systemd: Path = SYSTEMD,
    run: object = None,
    *,
    remount: bool = False,
) -> dict[str, object]:
    """Mount the second share (copy 02). The agent does not depend on it: a missing copy
    must never stop backups, so there is no ``RequiresMountsFor``."""
    runner = run if callable(run) else _run
    user = _validate(req)
    unit_name = _mount(req, user, etc / "smb-copy02.credentials", systemd, runner, remount)
    target = req.mount_point / "repository"
    target.mkdir(exist_ok=True)
    set_env(env_file, "CARBOREWIND_AGENT_REPLICA_PATH", str(target))
    return {"unit": unit_name, "replica": str(target)}


def remove_replica(
    mount_point: Path,
    env_file: Path,
    etc: Path = ETC,
    systemd: Path = SYSTEMD,
    run: object = None,
) -> None:
    """Stop using copy 02 (the files on the share are left untouched)."""
    runner = run if callable(run) else _run
    if os.geteuid() != 0:
        raise StorageError("run this command as root (sudo)")
    unit_name = systemd_escape_path(mount_point) + ".mount"
    if (systemd / unit_name).exists():
        runner("systemctl", "disable", "--now", unit_name)
        (systemd / unit_name).unlink()
        runner("systemctl", "daemon-reload")
    (etc / "smb-copy02.credentials").unlink(missing_ok=True)
    unset_env(env_file, "CARBOREWIND_AGENT_REPLICA_PATH")


def configure_local(
    path: Path, env_file: Path, systemd: Path = SYSTEMD, run: object = None
) -> None:
    """Back to a local directory: drop the agent's mount requirement."""
    runner = run if callable(run) else _run
    if os.geteuid() != 0:
        raise StorageError("run this command as root (sudo)")
    dropin = systemd / "carborewind-agent.service.d" / "storage.conf"
    if dropin.exists():
        dropin.unlink()
        runner("systemctl", "daemon-reload")
    set_env(env_file, "CARBOREWIND_AGENT_REPOSITORY_PATH", str(path))


def read_password(password_file: str | None) -> str:
    if password_file:
        return Path(password_file).read_text().rstrip("\n")
    first = getpass.getpass("SMB password: ")
    if getpass.getpass("Confirm: ") != first:
        raise StorageError("passwords do not match")
    return first
