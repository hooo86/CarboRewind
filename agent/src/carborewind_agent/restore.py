"""Restore items of a snapshot into a Carbonio mailbox.

Selection
---------
A restore reads one complete manifest (any snapshot: point-in-time) and selects either
explicit item ids, every item below some folders, or the whole account.

Modes
-----
* ``new_folder`` (default, never touches existing data): items are recreated under a new
  folder named after the snapshot, keeping the original tree. Mail goes below
  ``/<label>/…``; contacts, calendars and tasks need folders of their own view, so they go
  below ``/<top folder> (<label>)/…``.
* ``original``: items go back to their original folder (recreated if it was deleted). Only
  for the source account itself; an item whose original id still exists is skipped, so this
  mode restores what was deleted without creating duplicates.

How items are written
---------------------
* messages: ``AddMsgRequest`` with the original flags, tags and received date;
* contacts: REST import ``?fmt=vcf``; appointments and tasks: ``?fmt=ics`` (``resolve=skip``
  makes a replay harmless: an event with the same UID is not duplicated);
* documents (Files) are not restorable by this CE path and are reported as unsupported.

A **dry run** computes the plan (folders to create, items to restore or skip) without any
write. Progress is checkpointed so that a retried job does not inject the same message twice.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .carbonio import CarbonioClient, SoapFaultError
from .repository import Repository
from .throttle import Throttle

log = logging.getLogger(__name__)

Checkpoint = Callable[[dict[str, Any]], None]

MODES = ("new_folder", "original")
# Flags a user can see and that AddMsgRequest accepts: unread, flagged, replied, sent,
# forwarded, draft, urgent, low priority. Derived flags (attachment…) are recomputed.
KEPT_FLAGS = frozenset("ufrswd!?")
IMPORT_FORMAT = {"contact": "vcf", "appointment": "ics", "task": "ics"}
FOLDER_VIEWS = {"contact", "appointment", "task", "document"}
# Faults meaning the whole job cannot go on (as opposed to one bad item).
FATAL_FAULTS = {
    "account.NO_SUCH_ACCOUNT",
    "service.PERM_DENIED",
    "account.AUTH_FAILED",
    "mail.QUOTA_EXCEEDED",
}


class RestoreError(Exception):
    """The request cannot be executed (bad selection, unknown snapshot…)."""


@dataclass
class RestoreResult:
    dry_run: bool
    mode: str
    root: str | None
    selected: int = 0
    restored: int = 0
    present: int = 0
    unsupported: int = 0
    failed: dict[int, str] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    folders_created: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Body of ``POST /api/agent/jobs/{id}/restore-complete``."""
        return {
            "dry_run": self.dry_run,
            "mode": self.mode,
            "root": self.root,
            "selected": self.selected,
            "restored": self.restored,
            "present": self.present,
            "unsupported": self.unsupported,
            "failed": {str(k): v for k, v in sorted(self.failed.items())},
            "by_kind": self.by_kind,
            "folders_created": self.folders_created,
        }


def _view(view: str | None) -> str:
    return view if view in FOLDER_VIEWS else "message"


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0]


class _Folders:
    """Target folder tree: resolves paths, creating what is missing (unless dry run)."""

    def __init__(self, client: CarbonioClient, account_id: str, dry_run: bool) -> None:
        self._client = client
        self._account = account_id
        self._dry_run = dry_run
        self._ids: dict[str, int] = {"": 1}
        for f in client.get_folders(account_id):
            self._ids[f.path.lower()] = f.id
        self.created: list[str] = []

    def ensure(self, path: str, view: str, views: dict[str, str]) -> int:
        """Folder id for ``path``; intermediate folders take their view from ``views``."""
        key = path.lower()
        if key in self._ids:
            return self._ids[key]
        parent = self.ensure(_parent(path), views.get(_parent(path).lower(), view), views)
        name = path.rsplit("/", 1)[1]
        if self._dry_run:
            folder_id = -len(self.created) - 2  # placeholder, never sent
        else:
            folder_id = self._client.create_folder(self._account, parent, name, view)
        self._ids[key] = folder_id
        self.created.append(path)
        return folder_id


class AccountRestore:
    def __init__(
        self,
        client: CarbonioClient,
        repo: Repository,
        throttle: Throttle | None = None,
        *,
        checkpoint_every: int = 50,
    ) -> None:
        self._client = client
        self._repo = repo
        self._throttle = throttle or Throttle()
        self._checkpoint_every = checkpoint_every

    def run(
        self,
        source_account_id: str,
        snapshot_id: str,
        target_account_id: str,
        target_name: str,
        *,
        mode: str = "new_folder",
        label: str = "CarboRewind",
        folder_ids: list[int] | None = None,
        item_ids: list[int] | None = None,
        dry_run: bool = False,
        resume: dict[str, Any] | None = None,
        checkpoint: Checkpoint | None = None,
    ) -> RestoreResult:
        if mode not in MODES:
            raise RestoreError(f"unknown restore mode {mode!r}")
        same_account = source_account_id == target_account_id
        if mode == "original" and not same_account:
            raise RestoreError("mode 'original' restores into the source account only")
        if "/" in label or not label.strip():
            raise RestoreError("invalid label")
        with self._repo.lock():
            manifest = self._repo.get_manifest(source_account_id, snapshot_id)
            folders = {int(f["id"]): f for f in manifest.get("folders", [])}
            items = {int(k): v for k, v in manifest.get("items", {}).items()}
            selected = self._select(items, folders, folder_ids or [], item_ids or [])
            root = f"/{label}" if mode == "new_folder" else None
            result = RestoreResult(dry_run=dry_run, mode=mode, root=root, selected=len(selected))
            state = resume or {}
            done = {int(i) for i in state.get("done", [])}
            present = {int(i) for i in state.get("present", [])}
            target = _Folders(self._client, target_account_id, dry_run)
            views = {f["path"].lower(): _view(f.get("view")) for f in folders.values()}
            processed = 0

            for item_id in selected:
                entry = items[item_id]
                kind = entry["kind"]
                if item_id in done:
                    result.restored += 1
                    result.by_kind[kind] = result.by_kind.get(kind, 0) + 1
                    continue
                if item_id in present:
                    result.present += 1
                    continue
                if kind not in IMPORT_FORMAT and kind != "message":
                    result.unsupported += 1
                    continue
                if mode == "original" and self._exists(target_account_id, item_id):
                    present.add(item_id)
                    result.present += 1
                    continue
                source = folders.get(int(entry["folder"]))
                path = source["path"] if source else "/Inbox"
                view = _view(source.get("view") if source else None)
                if view != _view(kind):
                    view = _view(kind)  # an item in an odd folder: give it a folder it fits
                dest, dest_views = self._destination(path, view, root, views)
                try:
                    folder_id = target.ensure(dest, view, dest_views)
                    if not dry_run:
                        self._write(target_account_id, target_name, entry, kind, folder_id, dest)
                except SoapFaultError as exc:
                    if exc.code in FATAL_FAULTS:
                        raise
                    result.failed[item_id] = exc.code
                    log.error(
                        "item restore failed",
                        extra={"item": item_id, "code": exc.code, "account": target_account_id},
                    )
                    continue
                done.add(item_id)
                result.restored += 1
                result.by_kind[kind] = result.by_kind.get(kind, 0) + 1
                processed += 1
                if checkpoint and not dry_run and processed % self._checkpoint_every == 0:
                    checkpoint({"done": sorted(done), "present": sorted(present)})
            result.folders_created = target.created
            return result

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _select(
        items: dict[int, dict[str, Any]],
        folders: dict[int, dict[str, Any]],
        folder_ids: list[int],
        item_ids: list[int],
    ) -> list[int]:
        if item_ids:
            missing = [i for i in item_ids if i not in items]
            if missing:
                raise RestoreError(f"{len(missing)} item(s) not in this snapshot")
            return sorted(set(item_ids))
        if folder_ids:
            unknown = [f for f in folder_ids if f not in folders]
            if unknown:
                raise RestoreError(f"{len(unknown)} folder(s) not in this snapshot")
            prefixes = [folders[f]["path"].lower() for f in folder_ids]
            wanted = {
                fid
                for fid, f in folders.items()
                if any(
                    f["path"].lower() == p or f["path"].lower().startswith(p + "/")
                    for p in prefixes
                )
            }
            return sorted(i for i, e in items.items() if int(e["folder"]) in wanted)
        return sorted(items)

    @staticmethod
    def _destination(
        path: str, view: str, root: str | None, views: dict[str, str]
    ) -> tuple[str, dict[str, str]]:
        """Target path of a source folder, and the views to give to new intermediate folders."""
        if root is None:  # original mode
            return path, views
        if view == "message":
            dest = root + path
            return dest, {
                root.lower(): "message",
                **{root.lower() + k: v for k, v in views.items()},
            }
        top, _, rest = path.lstrip("/").partition("/")
        base = f"/{top} ({root.lstrip('/')})"
        dest = base + (f"/{rest}" if rest else "")
        return dest, {base.lower(): view}

    def _exists(self, account_id: str, item_id: int) -> bool:
        try:
            self._client.get_item_meta(account_id, item_id)
        except SoapFaultError as exc:
            if exc.code == "mail.NO_SUCH_ITEM":
                return False
            raise
        return True

    def _write(
        self,
        account_id: str,
        account_name: str,
        entry: dict[str, Any],
        kind: str,
        folder_id: int,
        folder_path: str,
    ) -> None:
        raw = self._repo.get(entry["blob"])
        self._throttle.consume(len(raw))
        if kind != "message":
            self._client.import_rest(
                account_name, account_id, folder_path, IMPORT_FORMAT[kind], raw
            )
            return
        meta = json.loads(self._repo.get(entry["meta_blob"])) if entry.get("meta_blob") else {}
        flags = "".join(c for c in str(meta.get("flags") or "") if c in KEPT_FLAGS)
        tags = str(meta.get("tags") or "")
        received = int(meta["date_ms"]) if meta.get("date_ms") else None
        try:
            self._client.add_message(
                account_id, folder_id, raw, flags=flags, tags=tags, received_ms=received
            )
        except SoapFaultError as exc:
            if not tags or exc.code in FATAL_FAULTS:
                raise
            # Tags may not exist (any more) in the target mailbox: keep the message anyway.
            log.warning("restoring without tags", extra={"code": exc.code})
            self._client.add_message(account_id, folder_id, raw, flags=flags, received_ms=received)
