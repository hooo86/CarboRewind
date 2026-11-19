"""Copy 02: mirror the repository onto a second share.

The repository is made of immutable, encrypted, content-addressed files, so a mirror is a
plain file copy: files missing on the copy (or of a different size, e.g. left by a crash)
are copied atomically (temporary name, ``fsync``, rename), and blobs or manifests that the
purge removed from the repository are removed from the copy too, so that both follow the
same retention. Nothing is decrypted: the copy is as useless without the key file as the
original. To restore from it, point ``CARBOREWIND_AGENT_REPOSITORY_PATH`` at the copy.

The copy must be a network mount: if the share is not mounted, its mount point is a plain
local directory and writing there would silently fill the Carbonio server's disk.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import NETWORK_FS, mount_info

MIRRORED = ("blobs", "manifests")  # deletions are mirrored only below these
SKIPPED = ("locks",)


class ReplicaError(Exception):
    pass


@dataclass
class ReplicaResult:
    files: int = 0
    copied: int = 0
    copied_bytes: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "copied": self.copied,
            "copied_bytes": self.copied_bytes,
            "deleted": self.deleted,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
        }


def _files(root: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts[:1] and rel.parts[0] in SKIPPED:
            dirnames[:] = []
            continue
        for name in filenames:
            if name.startswith(".tmp"):
                continue
            path = Path(dirpath) / name
            out[str(rel / name)] = path.stat().st_size
    return out


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.parent / f".tmp-{uuid.uuid4().hex}"
    try:
        with open(source, "rb") as src, open(tmp, "xb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def replicate(
    source: Path, target: Path, *, require_mount: bool = True, min_free_bytes: int = 0
) -> ReplicaResult:
    """Make ``target`` a copy of the repository ``source``."""
    if not (source / "config.json").exists():
        raise ReplicaError(f"{source} is not a repository")
    if require_mount:
        info = mount_info(target.parent if not target.exists() else target)
        if info is None or info.fstype not in NETWORK_FS:
            raise ReplicaError(f"copy 02 is not mounted ({target.parent}): nothing written")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = ReplicaResult()
    wanted = _files(source)
    present = _files(target)
    result.files = len(wanted)
    # config.json last: a copy is only "a repository" once its content is there.
    order = sorted(wanted, key=lambda rel: (rel == "config.json", rel))
    for rel in order:
        size = wanted[rel]
        if present.get(rel) == size:
            continue
        if min_free_bytes:
            st = os.statvfs(target)
            if st.f_bavail * st.f_frsize - size < min_free_bytes:
                result.errors.append(f"copy 02 is full: stopped before {rel}")
                break
        try:
            _copy(source / rel, target / rel)
        except OSError as exc:
            result.errors.append(f"{rel}: {exc.strerror or exc}")
            continue
        result.copied += 1
        result.copied_bytes += size
    for rel in present.keys() - wanted.keys():
        if Path(rel).parts[:1] and Path(rel).parts[0] in MIRRORED:
            try:
                (target / rel).unlink()
                result.deleted += 1
            except OSError as exc:
                result.errors.append(f"{rel}: {exc.strerror or exc}")
    (target / "locks").mkdir(exist_ok=True, mode=0o700)
    return result
