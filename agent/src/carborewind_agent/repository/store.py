"""On-disk backup repository (local disk or NFS mount).

Layout::

    <root>/
      config.json                      format version, repository id (plain, no secret)

Every directory is 0700 and every file 0600 (owner: the agent's system user).
      keys/master.json                 master key wrapped by the KEK
      blobs/<id[:2]>/<id>              sealed objects, content-addressed
      manifests/<account>/<snap>.json  sealed snapshot manifests
      archives/<account>/<id>.json     sealed archive manifests (messages moved to the archive)
      locks/repo.lock                  advisory lock (shared: backup/restore, exclusive: purge)

Writes are atomic (temporary file in the same directory, ``fsync``, ``rename``, ``fsync`` of
the directory) and idempotent: writing a blob that already exists is a no-op, which makes an
interrupted backup safely resumable.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import random
import re
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crypto import IntegrityError, Keys, generate_key, unwrap_master, wrap_master

FORMAT_VERSION = 1
_BLOB_ID = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RepositoryError(Exception):
    """Invalid repository layout, version or arguments."""


class SpaceError(RepositoryError):
    """Not enough free space left: writing more could fill the disk Carbonio runs on."""


@dataclass(frozen=True)
class PutResult:
    blob_id: str
    size: int
    stored_size: int
    created: bool  # False when the blob already existed (deduplicated)


@dataclass
class VerifyReport:
    checked: int = 0
    corrupted: list[str] = field(default_factory=list)
    manifests_checked: int = 0
    manifests_corrupted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.corrupted and not self.manifests_corrupted


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, owner-only (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _segment(value: str, what: str) -> str:
    """Reject path traversal in identifiers used as path components."""
    if not _SAFE_SEGMENT.fullmatch(value):
        raise RepositoryError(f"invalid {what}: {value!r}")
    return value


class Repository:
    def __init__(self, root: Path, keys: Keys, repo_id: str) -> None:
        self.root = root
        self.repo_id = repo_id
        self._keys = keys

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def init(cls, root: Path, kek: bytes) -> Repository:
        """Create a new repository in an empty (or absent) directory."""
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(root.iterdir()):
            raise RepositoryError(f"{root} is not empty")
        repo_id = str(uuid.uuid4())
        master = generate_key()
        config = {
            "format": FORMAT_VERSION,
            "repository_id": repo_id,
            "created": datetime.now(UTC).isoformat(),
            "cipher": "AES-256-GCM",
            "compression": "zstd",
        }
        for sub in ("blobs", "manifests", "locks", "keys"):
            (root / sub).mkdir(mode=0o700)
        _atomic_write(root / "keys" / "master.json", json.dumps(wrap_master(kek, master)).encode())
        _atomic_write(root / "config.json", json.dumps(config, indent=2).encode())
        return cls(root, Keys.from_master(master), repo_id)

    @classmethod
    def open(cls, root: Path, kek: bytes) -> Repository:
        try:
            config = json.loads((root / "config.json").read_text())
            wrapped = json.loads((root / "keys" / "master.json").read_text())
        except FileNotFoundError as exc:
            raise RepositoryError(f"{root} is not a CarboRewind repository") from exc
        if config.get("format") != FORMAT_VERSION:
            raise RepositoryError(f"unsupported repository format {config.get('format')!r}")
        return cls(root, Keys.from_master(unwrap_master(kek, wrapped)), config["repository_id"])

    @staticmethod
    def rotate_kek(root: Path, old_kek: bytes, new_kek: bytes) -> None:
        """Re-wrap the master key with a new KEK (data is not re-encrypted)."""
        path = root / "keys" / "master.json"
        master = unwrap_master(old_kek, json.loads(path.read_text()))
        _atomic_write(path, json.dumps(wrap_master(new_kek, master)).encode())

    # ------------------------------------------------------------------ locking

    @contextlib.contextmanager
    def lock(self, exclusive: bool = False) -> Iterator[None]:
        """Advisory lock: many backups/restores in parallel, purge alone.

        Uses ``flock``; on NFS this requires NFSv4 (or v3 with lockd), see the admin guide.
        """
        path = self.root / "locks" / "repo.lock"
        with open(path, "a+b") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------ blobs

    def _blob_path(self, blob_id: str) -> Path:
        if not _BLOB_ID.fullmatch(blob_id):
            raise RepositoryError(f"invalid blob id {blob_id!r}")
        return self.root / "blobs" / blob_id[:2] / blob_id

    # Free space that must remain on the repository's file system (0 = no check). Checked
    # every SPACE_CHECK_EVERY new blobs and by ``check_space``.
    min_free_bytes = 0
    SPACE_CHECK_EVERY = 64
    _writes = 0

    def check_space(self, needed: int = 0) -> None:
        if not self.min_free_bytes:
            return
        st = os.statvfs(self.root)
        free = st.f_bavail * st.f_frsize
        if free - needed < self.min_free_bytes:
            raise SpaceError(
                f"not enough free space on {self.root}: {free // 2**20} MiB free, "
                f"{self.min_free_bytes // 2**20} MiB must stay free"
            )

    def put(self, data: bytes) -> PutResult:
        blob_id = self._keys.blob_id(data)
        path = self._blob_path(blob_id)
        if path.exists():
            return PutResult(blob_id, len(data), path.stat().st_size, created=False)
        if self._writes % self.SPACE_CHECK_EVERY == 0:
            self.check_space(len(data))
        self._writes += 1
        sealed = self._keys.seal(data, aad=blob_id.encode())
        _atomic_write(path, sealed)
        return PutResult(blob_id, len(data), len(sealed), created=True)

    def has(self, blob_id: str) -> bool:
        return self._blob_path(blob_id).exists()

    def get(self, blob_id: str) -> bytes:
        try:
            sealed = self._blob_path(blob_id).read_bytes()
        except FileNotFoundError as exc:
            raise RepositoryError(f"blob {blob_id} not found") from exc
        data = self._keys.open(sealed, aad=blob_id.encode())
        if self._keys.blob_id(data) != blob_id:  # defence in depth; GCM already authenticates
            raise IntegrityError(f"blob {blob_id}: content does not match its id")
        return data

    def delete(self, blob_id: str) -> int:
        """Remove a blob (garbage collection, under the exclusive lock); bytes freed."""
        path = self._blob_path(blob_id)
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            return 0
        return size

    def blob_ids(self) -> Iterator[str]:
        for prefix in sorted((self.root / "blobs").iterdir()):
            for path in sorted(prefix.iterdir()):
                if _BLOB_ID.fullmatch(path.name):
                    yield path.name

    # ------------------------------------------------------------------ manifests

    def _manifest_path(self, account: str, snapshot: str) -> tuple[Path, bytes]:
        rel = f"manifests/{_segment(account, 'account')}/{_segment(snapshot, 'snapshot')}.json"
        return self.root / rel, rel.encode()

    def put_manifest(self, account: str, snapshot: str, manifest: dict[str, Any]) -> str:
        path, aad = self._manifest_path(account, snapshot)
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        _atomic_write(path, self._keys.seal(payload, aad=aad))
        return aad.decode()

    def get_manifest(self, account: str, snapshot: str) -> dict[str, Any]:
        path, aad = self._manifest_path(account, snapshot)
        try:
            sealed = path.read_bytes()
        except FileNotFoundError as exc:
            raise RepositoryError(f"manifest {aad.decode()} not found") from exc
        result: dict[str, Any] = json.loads(self._keys.open(sealed, aad=aad))
        return result

    def delete_manifest(self, account: str, snapshot: str) -> None:
        path, _ = self._manifest_path(account, snapshot)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    # ------------------------------------------------------------------ archive manifests

    def _archive_path(self, account: str, archive: str) -> tuple[Path, bytes]:
        rel = f"archives/{_segment(account, 'account')}/{_segment(archive, 'archive')}.json"
        return self.root / rel, rel.encode()

    def put_archive(self, account: str, archive: str, manifest: dict[str, Any]) -> str:
        """Seal the list of messages one archive run took out of a mailbox."""
        path, aad = self._archive_path(account, archive)
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        _atomic_write(path, self._keys.seal(payload, aad=aad))
        return aad.decode()

    def get_archive(self, account: str, archive: str) -> dict[str, Any]:
        path, aad = self._archive_path(account, archive)
        try:
            sealed = path.read_bytes()
        except FileNotFoundError as exc:
            raise RepositoryError(f"archive {aad.decode()} not found") from exc
        result: dict[str, Any] = json.loads(self._keys.open(sealed, aad=aad))
        return result

    def delete_archive(self, account: str, archive: str) -> None:
        path, _ = self._archive_path(account, archive)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    # ------------------------------------------------------------------ verification

    def verify(self, sample: float = 1.0, seed: int | None = None) -> VerifyReport:
        """Decrypt and authenticate all blobs (``sample=1``) or a random fraction of them."""
        if not 0 < sample <= 1:
            raise RepositoryError("sample must be in (0, 1]")
        rng = random.Random(seed)  # noqa: S311 - sampling, not cryptography
        report = VerifyReport()
        for blob_id in self.blob_ids():
            if sample < 1 and rng.random() >= sample:
                continue
            report.checked += 1
            try:
                self.get(blob_id)
            except (IntegrityError, RepositoryError):
                report.corrupted.append(blob_id)
        for sub, read in (("manifests", self.get_manifest), ("archives", self.get_archive)):
            base = self.root / sub
            for path in sorted(base.glob("*/*.json")) if base.exists() else []:
                if sample < 1 and rng.random() >= sample:
                    continue
                report.manifests_checked += 1
                try:
                    read(path.parent.name, path.stem)
                except (IntegrityError, RepositoryError, ValueError):
                    name = f"{path.parent.name}/{path.stem}"
                    report.manifests_corrupted.append(
                        name if sub == "manifests" else f"archives/{name}"
                    )
        return report
