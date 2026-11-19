"""Archiving: move the old messages of one mailbox into the repository.

A job archives one account, in phases:

1. **Selection**: ``SearchRequest`` ``before:<date>``, oldest first. Trash, Junk and Drafts
   (and their sub-folders) are never archived, nor the folders the policy excludes; flagged
   and unread messages are kept on request. The date Carbonio returns is checked again here,
   so a query parsed differently can only select fewer messages, never younger ones.
2. ``simulate`` stops there and reports counts and sizes per folder.
3. **Storage**: raw content and metadata go to the repository (deduplicated: a message
   already backed up costs nothing), are read back, decrypted and compared byte for byte.
   An archive manifest listing every message is sealed in the repository and the index is
   sent to the API **before** anything is deleted.
4. **Copy 2**: when configured, the repository is mirrored and every archived blob is read
   back from the copy.
5. **Deletion** (``archive`` mode only): ``MsgActionRequest op=delete`` by batches. A message
   that no longer exists counts as removed (it is in the archive either way).

Any error before step 5 leaves the mailbox untouched. Progress is checkpointed, so a retried
job neither stores nor deletes twice.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from .carbonio import CarbonioClient, SoapFaultError
from .repository import Repository
from .throttle import Throttle

log = logging.getLogger(__name__)

Checkpoint = Callable[[dict[str, Any]], None]

MODES = ("simulate", "copy", "archive")
# Trash, Junk and Drafts: never archived (nor their sub-folders).
SYSTEM_EXCLUDED = {3: "/Trash", 4: "/Junk", 6: "/Drafts"}
# A simulation looks at this many messages at most.
SCAN_LIMIT = 100_000
PAGE = 500
DELETE_BATCH = 100
# Faults meaning the job cannot go on for this account.
FATAL_FAULTS = {"account.NO_SUCH_ACCOUNT", "service.PERM_DENIED", "account.AUTH_FAILED"}

_HEADERS = email.parser.BytesHeaderParser(policy=email.policy.default)


class ArchiveError(Exception):
    """The job cannot run (bad parameters, protected account, copy 2 out of date…)."""


@dataclass
class ArchiveEntry:
    item_id: int
    folder: int
    folder_path: str
    blob: str
    meta_blob: str
    size: int
    meta_size: int
    flags: str = ""
    subject: str | None = None
    sender: str | None = None
    recipients: str | None = None
    date: str | None = None  # ISO 8601
    has_attachments: bool = False

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArchiveEntry:
        return cls(**data)


@dataclass
class ArchiveResult:
    mode: str
    scanned: int = 0
    candidates: int = 0
    candidate_bytes: int = 0
    truncated: bool = False  # more messages qualify than one run handles
    by_folder: dict[str, list[int]] = field(default_factory=dict)  # path -> [count, bytes]
    archived: int = 0
    archived_bytes: int = 0
    new_bytes: int = 0
    removed: list[int] = field(default_factory=list)
    removed_bytes: int = 0
    errors: dict[int, str] = field(default_factory=dict)
    manifest_ref: str | None = None
    replica_checked: bool = False
    cancelled: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Body of ``POST /api/agent/jobs/{id}/archive-complete``."""
        return {
            "mode": self.mode,
            "scanned": self.scanned,
            "candidates": self.candidates,
            "candidate_bytes": self.candidate_bytes,
            "truncated": self.truncated,
            "by_folder": dict(sorted(self.by_folder.items())[:500]),
            "archived": self.archived,
            "archived_bytes": self.archived_bytes,
            "new_bytes": self.new_bytes,
            "removed": self.removed,
            "removed_bytes": self.removed_bytes,
            "errors": {str(k): v for k, v in sorted(self.errors.items())[:1000]},
            "error_count": len(self.errors),
            "manifest_ref": self.manifest_ref,
            "replica_checked": self.replica_checked,
            "cancelled": self.cancelled,
        }


@dataclass
class _Candidate:
    item_id: int
    folder: int
    folder_path: str
    flags: str
    date_ms: int
    size: int
    tags: str = ""


def _header_fields(raw: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"subject": None, "sender": None, "recipients": None, "date": None}
    try:
        headers = _HEADERS.parsebytes(raw)
        out["subject"] = str(headers.get("Subject", ""))[:2000] or None
        out["sender"] = str(headers.get("From", ""))[:1000] or None
        addresses = getaddresses(
            [str(v) for name in ("To", "Cc") for v in headers.get_all(name, [])]
        )
        recipients = ", ".join(a for _, a in addresses if a)
        out["recipients"] = recipients[:2000] or None
        if headers.get("Date"):
            out["date"] = parsedate_to_datetime(str(headers["Date"])).astimezone(UTC).isoformat()
    except (ValueError, TypeError, IndexError):
        log.debug("unparseable headers", extra={"size": len(raw)})
    return out


def _excluded(path: str, prefixes: list[str]) -> bool:
    low = path.lower()
    return any(low == p or low.startswith(p + "/") for p in prefixes)


class MailboxArchiver:
    def __init__(
        self,
        client: CarbonioClient,
        repo: Repository,
        throttle: Throttle | None = None,
        *,
        protected: set[str] | None = None,
        checkpoint_every: int = 100,
    ) -> None:
        self._client = client
        self._repo = repo
        self._throttle = throttle or Throttle()
        self._protected = {p.lower() for p in (protected or set())}
        self._checkpoint_every = checkpoint_every

    # ------------------------------------------------------------------ public API

    def run(
        self,
        account_id: str,
        account_name: str,
        archive_id: str,
        params: dict[str, Any],
        *,
        report: Callable[[list[dict[str, Any]]], None] | None = None,
        replica_check: Callable[[list[str]], None] | None = None,
        resume: dict[str, Any] | None = None,
        checkpoint: Checkpoint | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ArchiveResult:
        mode = str(params.get("mode", "simulate"))
        if mode not in MODES:
            raise ArchiveError(f"unknown archive mode {mode!r}")
        if account_name.lower() in self._protected:
            raise ArchiveError("this account is protected from archiving")
        try:
            cutoff = datetime.fromisoformat(str(params["before"]))
        except (KeyError, ValueError) as exc:
            raise ArchiveError("archive job without a valid cut-off date") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        limit = max(1, int(params.get("max_messages", 5000)))
        stop = should_stop or (lambda: False)
        state = resume or {}
        done = {int(k): ArchiveEntry.from_json(v) for k, v in state.get("done", {}).items()}
        removed = {int(i) for i in state.get("removed", [])}
        result = ArchiveResult(mode=mode)

        candidates = self._select(account_id, cutoff, params, mode, limit, done, result)
        if mode == "simulate":
            return result

        with self._repo.lock():
            # Storage: everything read back and compared before it is trusted.
            processed = 0
            for cand in candidates:
                if cand.item_id in done:
                    continue
                if stop():
                    result.cancelled = True
                    break
                try:
                    done[cand.item_id] = self._store(account_id, account_name, cand, result)
                except SoapFaultError as exc:
                    if exc.code in FATAL_FAULTS:
                        raise
                    if exc.code != "mail.NO_SUCH_ITEM":  # deleted meanwhile: nothing to do
                        result.errors[cand.item_id] = exc.code
                        log.error(
                            "message archive failed",
                            extra={"account": account_id, "item": cand.item_id, "code": exc.code},
                        )
                    continue
                processed += 1
                if checkpoint and processed % self._checkpoint_every == 0:
                    checkpoint(self._state(done, removed))
            entries = [done[k] for k in sorted(done)]
            result.archived = len(entries)
            result.archived_bytes = sum(e.size for e in entries)
            if not entries:
                return result
            manifest = {
                "version": 1,
                "account_id": account_id,
                "account": account_name,
                "archive_id": archive_id,
                "taken_at": datetime.now(UTC).isoformat(),
                "items": {str(e.item_id): e.to_json() for e in entries},
            }
            result.manifest_ref = self._repo.put_archive(account_id, archive_id, manifest)
        if checkpoint:
            checkpoint(self._state(done, removed))
        # The index is known to the API before anything leaves the mailbox.
        if report is not None:
            payload = [e.to_json() for e in entries]
            for start in range(0, len(payload), 500):
                report(payload[start : start + 500])
        if mode != "archive" or result.cancelled or stop():
            result.cancelled = result.cancelled or stop()
            return result

        if replica_check is not None:
            replica_check(sorted({b for e in entries for b in (e.blob, e.meta_blob)}))
            result.replica_checked = True

        sizes = {e.item_id: e.size for e in entries}
        pending = [i for i in sorted(sizes) if i not in removed]
        for start in range(0, len(pending), DELETE_BATCH):
            if stop():
                result.cancelled = True
                break
            batch = pending[start : start + DELETE_BATCH]
            removed.update(self._delete(account_id, batch, result))
            if checkpoint:
                checkpoint(self._state(done, removed))
        result.removed = sorted(removed)
        result.removed_bytes = sum(sizes.get(i, 0) for i in removed)
        return result

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _state(done: dict[int, ArchiveEntry], removed: set[int]) -> dict[str, Any]:
        return {
            "done": {str(k): v.to_json() for k, v in done.items()},
            "removed": sorted(removed),
            "progress": {"archived": len(done), "removed": len(removed)},
        }

    def _select(
        self,
        account_id: str,
        cutoff: datetime,
        params: dict[str, Any],
        mode: str,
        limit: int,
        done: dict[int, ArchiveEntry],
        result: ArchiveResult,
    ) -> list[_Candidate]:
        folders = {f.id: f for f in self._client.get_folders(account_id)}
        prefixes = [p.lower().rstrip("/") for p in SYSTEM_EXCLUDED.values()]
        prefixes += [
            "/" + str(p).strip().strip("/").lower()
            for p in params.get("exclude_folders", [])
            if str(p).strip().strip("/")
        ]
        excluded = set(SYSTEM_EXCLUDED)
        excluded.update(fid for fid, f in folders.items() if _excluded(f.path, prefixes))
        keep_flagged = bool(params.get("keep_flagged", True))
        keep_unread = bool(params.get("keep_unread", False))
        skip = {int(i) for i in params.get("skip_ids", [])} if mode == "copy" else set()
        cutoff_ms = int(cutoff.timestamp() * 1000)
        query = f"before:{cutoff.month}/{cutoff.day}/{cutoff.year}"
        wanted = limit - len(done)
        selected: list[_Candidate] = []
        offset = 0
        while result.scanned < SCAN_LIMIT:
            hits, more = self._client.search_messages(account_id, query, offset=offset, limit=PAGE)
            offset += len(hits)
            for hit in hits:
                result.scanned += 1
                try:
                    item_id, folder_id = int(hit["id"]), int(hit.get("l", 0))
                    date_ms, size = int(hit.get("d", 0)), int(hit.get("s", 0))
                except (KeyError, TypeError, ValueError):
                    continue  # shared folder ("<zid>:<id>") or odd answer: not ours
                flags = str(hit.get("f") or "")
                folder = folders.get(folder_id)
                if (
                    folder is None
                    or folder_id in excluded
                    or not date_ms
                    or date_ms >= cutoff_ms
                    or (keep_flagged and "f" in flags)
                    or (keep_unread and "u" in flags)
                    or item_id in skip
                    or item_id in done
                ):
                    continue
                result.candidates += 1
                result.candidate_bytes += size
                count = result.by_folder.setdefault(folder.path, [0, 0])
                count[0] += 1
                count[1] += size
                if len(selected) < wanted:
                    selected.append(
                        _Candidate(
                            item_id,
                            folder_id,
                            folder.path,
                            flags,
                            date_ms,
                            size,
                            str(hit.get("tn") or ""),
                        )
                    )
                elif mode != "simulate":
                    result.truncated = True
            if not more or not hits or (mode != "simulate" and result.truncated):
                break
        if result.scanned >= SCAN_LIMIT:
            result.truncated = True
        return selected

    def _store(
        self, account_id: str, account_name: str, cand: _Candidate, result: ArchiveResult
    ) -> ArchiveEntry:
        raw = self._client.fetch_raw(account_name, account_id, cand.item_id)
        self._throttle.consume(len(raw))
        content = self._repo.put(raw)
        meta_doc = {
            "id": cand.item_id,
            "kind": "message",
            "folder": cand.folder,
            "flags": cand.flags,
            "tags": cand.tags,
            "date_ms": cand.date_ms,
        }
        meta_raw = json.dumps(meta_doc, sort_keys=True).encode()
        meta = self._repo.put(meta_raw)
        # Read back: decrypts, authenticates and checks the content address.
        if self._repo.get(content.blob_id) != raw or self._repo.get(meta.blob_id) != meta_raw:
            raise ArchiveError(f"message {cand.item_id}: archived copy differs from the original")
        result.new_bytes += (content.stored_size if content.created else 0) + (
            meta.stored_size if meta.created else 0
        )
        fields = _header_fields(raw)
        if fields["date"] is None:
            fields["date"] = datetime.fromtimestamp(cand.date_ms / 1000, tz=UTC).isoformat()
        return ArchiveEntry(
            item_id=cand.item_id,
            folder=cand.folder,
            folder_path=cand.folder_path,
            blob=content.blob_id,
            meta_blob=meta.blob_id,
            size=len(raw),
            meta_size=len(meta_raw),
            flags=cand.flags[:32],
            has_attachments="a" in cand.flags,
            **fields,
        )

    def _delete(self, account_id: str, batch: list[int], result: ArchiveResult) -> list[int]:
        try:
            self._client.delete_messages(account_id, batch)
            return batch
        except SoapFaultError as exc:
            if exc.code in FATAL_FAULTS:
                raise
        gone: list[int] = []
        for item_id in batch:  # one by one, to tell the bad one from the rest
            try:
                self._client.delete_messages(account_id, [item_id])
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                if exc.code != "mail.NO_SUCH_ITEM":
                    result.errors[item_id] = exc.code
                    continue
            gone.append(item_id)
        return gone
