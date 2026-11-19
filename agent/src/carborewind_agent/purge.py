"""Garbage collection of the repository: delete what retention released.

The API decides *what* to delete (manifests of expired snapshots, blobs no remaining
catalogue row references) when this agent claims the job, i.e. while it runs nothing else.
Deletion happens under the repository's exclusive lock, so it also waits for any backup or
restore started by another process (``carborewind-agent`` CLI commands).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .repository import Repository, RepositoryError

log = logging.getLogger(__name__)


@dataclass
class PurgeResult:
    manifests_deleted: list[str] = field(default_factory=list)
    archives_deleted: list[str] = field(default_factory=list)
    blobs_deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0

    def to_payload(self) -> dict[str, Any]:
        """Body of ``POST /api/agent/jobs/{id}/purge-complete``."""
        return {
            "manifests_deleted": self.manifests_deleted,
            "archives_deleted": self.archives_deleted,
            "blobs_deleted": self.blobs_deleted,
            "freed_bytes": self.freed_bytes,
        }


def run_purge(repo: Repository, params: dict[str, Any]) -> PurgeResult:
    result = PurgeResult()
    with repo.lock(exclusive=True):
        for entry in params.get("manifests", []):
            try:
                repo.delete_manifest(entry["account"], entry["snapshot"])
            except (RepositoryError, KeyError) as exc:  # invalid id: skip, report the rest
                log.error("cannot delete manifest", extra={"entry": entry, "error": str(exc)})
                continue
            result.manifests_deleted.append(entry["snapshot"])
        for entry in params.get("archives", []):
            try:
                repo.delete_archive(entry["account"], entry["archive"])
            except (RepositoryError, KeyError) as exc:
                log.error("cannot delete archive", extra={"entry": entry, "error": str(exc)})
                continue
            result.archives_deleted.append(entry["archive"])
        for blob_id in params.get("blobs", []):
            try:
                result.freed_bytes += repo.delete(blob_id)
            except RepositoryError as exc:
                log.error("cannot delete blob", extra={"blob": blob_id, "error": str(exc)})
                continue
            result.blobs_deleted.append(blob_id)
    return result
