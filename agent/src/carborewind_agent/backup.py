"""Hot backup of one Carbonio account into the encrypted repository.

Algorithm
---------
* **Full** (no previous snapshot, or Carbonio answers ``mail.MUST_RESYNC``): initial
  ``SyncRequest`` gives the folder tree and every item id; each item is read (REST raw content
  + ``GetItemRequest`` metadata) and stored as two blobs.
* **Incremental**: ``SyncRequest`` from the previous snapshot's token, paged with ``more``;
  changed items are re-read, deleted ids are dropped. The previous manifest is loaded from the
  repository and patched, so **every manifest is complete** (point-in-time restore of any
  snapshot without replaying a chain).
* The new token is only recorded in the manifest once every item is stored, so an interrupted
  job never skips changes.

Resumption
----------
``checkpoint`` is called every ``checkpoint_every`` items with a JSON-serialisable state
(items already stored). Passing that state back as ``resume`` skips them: blobs are
content-addressed, so re-storing is harmless, and skipping avoids re-reading Carbonio.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from .carbonio import MUST_RESYNC, CarbonioClient, FolderInfo, SoapFaultError
from .repository import Repository
from .throttle import Throttle

log = logging.getLogger(__name__)

MANIFEST_VERSION = 1

Checkpoint = Callable[[dict[str, Any]], None]


@dataclass
class ItemEntry:
    kind: str
    folder: int
    blob: str
    meta_blob: str
    size: int
    subject: str | None = None
    sender: str | None = None
    date: str | None = None  # ISO 8601
    mod: int | None = None  # Carbonio modified sequence when known

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ItemEntry:
        return cls(**data)


@dataclass
class BackupResult:
    account_id: str
    seq: int
    backup_type: str  # "full" | "incremental"
    manifest_ref: str
    token: str
    item_count: int
    size_bytes: int
    new_bytes: int
    errors: dict[int, str] = field(default_factory=dict)
    # Catalogue changes relative to the previous snapshot.
    added: dict[int, ItemEntry] = field(default_factory=dict)  # new or new version
    removed: list[int] = field(default_factory=list)  # deleted or superseded
    folders: list[dict[str, Any]] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.errors)


_HEADER_PARSER = email.parser.BytesHeaderParser(policy=email.policy.default)


def extract_metadata(kind: str, raw: bytes, meta: dict[str, Any]) -> dict[str, str | None]:
    """Searchable fields for the catalogue (subject, sender, date)."""
    subject = sender = date = None
    if kind == "message":
        try:
            headers = _HEADER_PARSER.parsebytes(raw)
            subject = str(headers.get("Subject", "")) or None
            sender = str(headers.get("From", "")) or None
            if headers.get("Date"):
                date = parsedate_to_datetime(str(headers["Date"])).astimezone(UTC).isoformat()
        except (ValueError, TypeError, IndexError):
            log.debug("unparseable headers", extra={"size": len(raw)})
    elif kind == "contact":
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.upper().startswith("FN:"):
                subject = line[3:].strip()
                break
    if date is None and meta.get("d"):
        date = datetime.fromtimestamp(int(meta["d"]) / 1000, tz=UTC).isoformat()
    return {"subject": subject, "sender": sender, "date": date}


@dataclass
class _Base:
    """Content of the previous snapshot (empty for a first backup)."""

    items: dict[int, ItemEntry] = field(default_factory=dict)
    folders: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Context:
    account_id: str
    account_name: str
    snapshot_id: str
    seq: int
    base: _Base
    checkpoint: Checkpoint | None


class AccountBackup:
    def __init__(
        self,
        client: CarbonioClient,
        repo: Repository,
        throttle: Throttle | None = None,
        *,
        checkpoint_every: int = 200,
        page_size: int = 500,
    ) -> None:
        self._client = client
        self._repo = repo
        self._throttle = throttle or Throttle()
        self._checkpoint_every = checkpoint_every
        self._page_size = page_size

    # ------------------------------------------------------------------ public API

    def run(
        self,
        account_id: str,
        account_name: str,
        snapshot_id: str,
        seq: int,
        previous: tuple[str, str] | None = None,
        *,
        resume: dict[str, Any] | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> BackupResult:
        """Back up one account as snapshot ``seq``.

        ``previous`` is ``(snapshot_id, token)`` of the last complete snapshot, if any.
        """
        with self._repo.lock():
            base = _Base()
            token: str | None = None
            backup_type = "full"
            if previous is not None:
                manifest = self._repo.get_manifest(account_id, previous[0])
                base = _Base(
                    {int(k): ItemEntry.from_json(v) for k, v in manifest["items"].items()},
                    list(manifest.get("folders", [])),
                )
                token = previous[1]
                backup_type = "incremental"
            ctx = _Context(account_id, account_name, snapshot_id, seq, base, checkpoint)
            try:
                return self._run(ctx, token, backup_type, resume or {})
            except SoapFaultError as exc:
                if exc.code != MUST_RESYNC or backup_type == "full":
                    raise
                log.warning(
                    "sync token expired, falling back to full backup", extra={"account": account_id}
                )
                # Compared with the previous manifest all the same, so the catalogue diff
                # stays minimal.
                return self._run(ctx, None, "full", {})

    # ------------------------------------------------------------------ internals

    def _run(
        self, ctx: _Context, token: str | None, backup_type: str, resume: dict[str, Any]
    ) -> BackupResult:
        account_id, account_name = ctx.account_id, ctx.account_name
        base, checkpoint = ctx.base.items, ctx.checkpoint
        done: dict[int, ItemEntry] = {
            int(k): ItemEntry.from_json(v) for k, v in resume.get("done", {}).items()
        }
        errors: dict[int, str] = {}
        new_bytes = 0
        folders: dict[int, FolderInfo] = {}
        to_fetch: dict[int, tuple[str, int, dict[str, Any]]] = {}
        deleted: set[int] = set()

        # 1. Collect the changes (all pages) before reading content.
        cursor = token
        while True:
            delta = self._client.sync(account_id, cursor, change_limit=self._page_size)
            for f in delta.folders:
                folders[f.id] = f
            for kind, item_id, folder_id, attrs in delta.changed:
                to_fetch[item_id] = (kind, folder_id, attrs)
                deleted.discard(item_id)
            for item_id in delta.deleted:
                deleted.add(item_id)
                to_fetch.pop(item_id, None)
            cursor = delta.token
            if not delta.more:
                break
        new_token = cursor or ""

        # 2. Start from the previous state (incremental) or from nothing (full).
        items: dict[int, ItemEntry] = dict(base) if backup_type == "incremental" else {}
        for item_id in deleted:
            items.pop(item_id, None)

        # 3. Read and store changed items, checkpointing as we go.
        processed = 0
        for item_id, (kind, folder_id, attrs) in sorted(to_fetch.items()):
            if item_id in done:
                items[item_id] = done[item_id]
                continue
            try:
                entry, created = self._store_item(
                    account_id, account_name, item_id, kind, folder_id, attrs
                )
            except SoapFaultError as exc:
                if exc.code == "mail.NO_SUCH_ITEM":  # deleted between sync and read
                    items.pop(item_id, None)
                    continue
                errors[item_id] = exc.code
                log.error(
                    "item backup failed",
                    extra={"account": account_id, "item": item_id, "code": exc.code},
                )
                if item_id in base:  # keep the previous version rather than losing it
                    items[item_id] = base[item_id]
                continue
            new_bytes += created
            items[item_id] = entry
            done[item_id] = entry
            processed += 1
            if checkpoint and processed % self._checkpoint_every == 0:
                checkpoint(
                    {
                        "done": {str(k): v.to_json() for k, v in done.items()},
                        "backup_type": backup_type,
                    }
                )

        # 4. Folder tree: full snapshots replace it, incrementals merge renames/moves.
        folder_list: list[dict[str, Any]] = [
            {"id": f.id, "parent": f.parent_id, "path": f.path, "view": f.view}
            for f in sorted(folders.values(), key=lambda f: f.id)
        ]
        if backup_type == "incremental":
            merged = {f["id"]: f for f in ctx.base.folders}
            merged.update({f["id"]: f for f in folder_list})
            folder_list = sorted(merged.values(), key=lambda f: int(f["id"]))

        manifest = {
            "version": MANIFEST_VERSION,
            "account_id": account_id,
            "account": account_name,
            "snapshot_id": ctx.snapshot_id,
            "seq": ctx.seq,
            "type": backup_type,
            "taken_at": datetime.now(UTC).isoformat(),
            "token": new_token,
            "folders": folder_list,
            "items": {str(k): v.to_json() for k, v in sorted(items.items())},
            "errors": {str(k): v for k, v in errors.items()},
        }
        ref = self._repo.put_manifest(account_id, ctx.snapshot_id, manifest)

        added = {k: v for k, v in items.items() if base.get(k) != v}
        removed = sorted(k for k, v in base.items() if items.get(k) != v)
        return BackupResult(
            account_id=account_id,
            seq=ctx.seq,
            backup_type=backup_type,
            manifest_ref=ref,
            token=new_token,
            item_count=len(items),
            size_bytes=sum(e.size for e in items.values()),
            new_bytes=new_bytes,
            errors=errors,
            added=added,
            removed=removed,
            folders=folder_list,
        )

    def _store_item(
        self,
        account_id: str,
        account_name: str,
        item_id: int,
        kind: str,
        folder_id: int,
        attrs: dict[str, Any],
    ) -> tuple[ItemEntry, int]:
        meta = (
            attrs if attrs.get("l") is not None else self._client.get_item_meta(account_id, item_id)
        )
        raw = self._client.fetch_raw(account_name, account_id, item_id)
        self._throttle.consume(len(raw))
        content = self._repo.put(raw)
        meta_doc = {
            "id": item_id,
            "kind": kind,
            "folder": int(meta.get("l", folder_id)),
            "flags": meta.get("f", ""),
            "tags": meta.get("tn", ""),
            "date_ms": meta.get("d"),
        }
        meta_blob = self._repo.put(json.dumps(meta_doc, sort_keys=True).encode())
        fields = extract_metadata(kind, raw, meta)
        entry = ItemEntry(
            kind=kind,
            folder=meta_doc["folder"],
            blob=content.blob_id,
            meta_blob=meta_blob.blob_id,
            size=len(raw),
            subject=fields["subject"],
            sender=fields["sender"],
            date=fields["date"],
            mod=int(meta["ms"]) if meta.get("ms") is not None else None,
        )
        created = (content.stored_size if content.created else 0) + (
            meta_blob.stored_size if meta_blob.created else 0
        )
        return entry, created
