"""Standby server: replay the backups of the main Carbonio into a second one (disaster recovery).

The standby is a clone of the main Carbonio VM with another IP address, with its own
CarboRewind agent in the *standby* role (set in the interface). That agent reads the same
repository (same share, same ``repository.key``) and, for each account, brings its Carbonio
in line with the latest snapshot of the main server:

* the account is created when missing (same zimbraId when Carbonio accepts it, a random
  password: users who never logged in on the clone must have theirs set again);
* messages: added, deleted, moved and their flags / tags updated, one by one, so a sync
  only costs what changed since the previous one;
* contacts, appointments and tasks cannot be identified on the standby after an import
  (the REST import returns no id): a folder of these is emptied and imported again when
  anything in it changed;
* folders: missing ones are created, those gone from the main server are deleted;
* documents (Files) are not replicated (not restorable by the CE path).

What was applied is kept per account in a local state file (main item id → standby item
id, content and metadata blobs, folder), saved as the sync goes, so an interrupted sync
resumes without duplicates.

The first sync after cloning has no state: an item of the clone with the same id and kind
as an item of the snapshot is the same item (a VM clone keeps the item ids); clone items
the snapshot does not have were deleted on the main server since the clone, and are deleted.
Flags of every message are written once in this first sync.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .carbonio import CarbonioClient, SoapFaultError
from .repository import Repository
from .restore import FATAL_FAULTS, IMPORT_FORMAT, KEPT_FLAGS, _Folders, _view
from .throttle import Throttle

log = logging.getLogger(__name__)

STATE_DIR = Path("/var/lib/carborewind/standby")
KINDS = ("message", "contact", "appointment", "task")
SAVE_EVERY = 200  # operations between two saves of the state file
DELETE_BATCH = 500
_SAFE = re.compile(r"^[A-Za-z0-9._@+-]{1,200}$")
# Account attributes copied when the standby must create an account.
CREATE_ATTRS = ("displayName", "givenName", "sn", "zimbraCOSId", "zimbraMailQuota")


class StandbyError(Exception):
    pass


@dataclass
class StandbyResult:
    account_created: bool = False
    items: int = 0
    added: int = 0
    deleted: int = 0
    moved: int = 0
    updated: int = 0
    reimported_folders: int = 0
    folders_created: int = 0
    folders_deleted: int = 0
    failed: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "account_created": self.account_created,
            "items": self.items,
            "added": self.added,
            "deleted": self.deleted,
            "moved": self.moved,
            "updated": self.updated,
            "reimported_folders": self.reimported_folders,
            "folders_created": self.folders_created,
            "folders_deleted": self.folders_deleted,
            "failed": dict(list(self.failed.items())[:200]),
            "failed_count": len(self.failed),
        }


class _State:
    """Per-account state file: ``{"snapshot", "target", "items": {main id: entry}}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.snapshot: str | None = None
        self.target: str | None = None
        self.items: dict[str, dict[str, Any]] = {}
        self.folders: dict[str, str] = {}  # non-message folder path -> fingerprint
        self.loaded = False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.snapshot = data.get("snapshot")
            self.target = data.get("target")
            self.items = data["items"]
            self.folders = dict(data.get("folders") or {})
            self.loaded = True

    def reset(self) -> None:
        self.snapshot, self.items, self.folders, self.loaded = None, {}, {}, False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "snapshot": self.snapshot,
                    "target": self.target,
                    "items": self.items,
                    "folders": self.folders,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
        self.loaded = True


class StandbySync:
    def __init__(
        self,
        client: CarbonioClient,
        repo: Repository,
        throttle: Throttle | None = None,
        *,
        state_dir: Path = STATE_DIR,
    ) -> None:
        self._client = client
        self._repo = repo
        self._throttle = throttle or Throttle()
        self._state_dir = state_dir

    # ------------------------------------------------------------------ account

    def _account(self, main_id: str, name: str, attrs: dict[str, str]) -> tuple[str, bool]:
        """Id of the account on the standby, created when missing."""
        found = self._client.find_account("id", main_id) or self._client.find_account("name", name)
        if found is not None:
            return found.id, False
        wanted = {k: str(v) for k, v in attrs.items() if k in CREATE_ATTRS and v}
        password = secrets.token_urlsafe(24)
        try:
            new_id = self._client.create_account(name, password, {**wanted, "zimbraId": main_id})
        except SoapFaultError as exc:
            if exc.code in ("account.ACCOUNT_EXISTS",) or exc.code in FATAL_FAULTS:
                raise
            # Carbonio may refuse to choose the id (or an attribute): create it plainly.
            log.warning("account created without its original id: %s", exc)
            new_id = self._client.create_account(name, password, {})
        log.warning("standby: account %s created (password to set again)", name)
        return new_id, True

    # ------------------------------------------------------------------ sync

    def run(
        self,
        main_id: str,
        name: str,
        snapshot_id: str,
        *,
        attrs: dict[str, str] | None = None,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
    ) -> StandbyResult:
        if not _SAFE.match(main_id):
            raise StandbyError(f"invalid account id {main_id!r}")
        result = StandbyResult()
        target, result.account_created = self._account(main_id, name, attrs or {})
        state = _State(self._state_dir / f"{main_id}.json")
        if state.target and state.target != target:
            state.reset()  # account replaced on the standby: start over
        state.target = target
        with self._repo.lock():
            manifest = self._repo.get_manifest(main_id, snapshot_id)
        folders = {int(f["id"]): f for f in manifest.get("folders", [])}
        paths = {fid: str(f["path"]) for fid, f in folders.items()}
        views = {f["path"].lower(): _view(f.get("view")) for f in folders.values()}
        wanted = {
            k: v
            for k, v in manifest.get("items", {}).items()
            if v.get("kind") in KINDS and str(k).isdigit()
        }
        result.items = len(wanted)
        tree = _Folders(self._client, target, dry_run=False)
        ops = 0

        def tick() -> None:
            nonlocal ops
            ops += 1
            if ops % SAVE_EVERY == 0:
                state.save()
                if checkpoint:
                    checkpoint({"progress": {"items": ops, "total": len(wanted)}})

        # First sync of an account that already exists on the standby: a clone keeps the item
        # ids, so its items are adopted; an account with another id (recreated since the
        # clone) is a stale copy whose messages are replaced.
        adopting = not state.loaded and not result.account_created
        clone = adopting and target == main_id
        if adopting:
            self._adopt_clone(target, wanted if clone else {}, state, result)

        # 1. Messages gone from the main server.
        gone = [k for k, st in state.items.items() if k not in wanted and st["kind"] == "message"]
        for start in range(0, len(gone), DELETE_BATCH):
            batch = gone[start : start + DELETE_BATCH]
            sids = [int(state.items[k]["sid"]) for k in batch]
            try:
                self._client.item_action(target, sids, "delete")
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                for k, sid in zip(batch, sids, strict=True):  # one by one: skip vanished ones
                    try:
                        self._client.item_action(target, [sid], "delete")
                    except SoapFaultError as one:
                        if one.code != "mail.NO_SUCH_ITEM":
                            result.failed[k] = one.code
                            continue
                    state.items.pop(k, None)
                    result.deleted += 1
                continue
            for k in batch:
                state.items.pop(k, None)
            result.deleted += len(batch)
            tick()

        # 2. Messages: add, replace, move; flags gathered to be written in batches.
        flag_groups: dict[tuple[str, str], list[int]] = {}
        for key in sorted(wanted, key=int):
            entry = wanted[key]
            if entry["kind"] != "message":
                continue
            path = paths.get(int(entry["folder"]), "/Inbox")
            st = state.items.get(key)
            try:
                if st is not None and st.get("blob") != entry["blob"]:
                    self._client.item_action(target, [int(st["sid"])], "delete")
                    state.items.pop(key)
                    st = None
                if st is None:
                    folder_id = tree.ensure(path, "message", views)
                    sid = self._add_message(target, entry, folder_id)
                    state.items[key] = {
                        "sid": sid,
                        "kind": "message",
                        "blob": entry["blob"],
                        "meta": entry.get("meta_blob"),
                        "folder": path.lower(),
                    }
                    result.added += 1
                    tick()
                    continue
                if st.get("folder") != path.lower():
                    folder_id = tree.ensure(path, "message", views)
                    self._client.item_action(target, [int(st["sid"])], "move", l=folder_id)
                    st["folder"] = path.lower()
                    result.moved += 1
                    tick()
                if st.get("meta") != entry.get("meta_blob"):
                    flags, tags = self._flags(entry)
                    flag_groups.setdefault((flags, tags), []).append(int(st["sid"]))
                    st["meta"] = entry.get("meta_blob")
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                result.failed[key] = exc.code
                log.error("standby: message %s not replicated: %s", key, exc)
        for (flags, tags), sids in flag_groups.items():
            for start in range(0, len(sids), DELETE_BATCH):
                chunk = sids[start : start + DELETE_BATCH]
                try:
                    self._client.item_action(target, chunk, "update", f=flags, tn=tags)
                    result.updated += len(chunk)
                except SoapFaultError as exc:
                    if exc.code in FATAL_FAULTS:
                        raise
                    log.warning("standby: flags not updated (%s)", exc)
                tick()

        # 3. Contacts, appointments, tasks: a folder is rebuilt when its content changed.
        self._sync_other_kinds(target, name, wanted, paths, views, tree, state, result)

        # 4. Folders gone from the main server (deepest first; system folders never).
        keep = {p.lower() for p in paths.values()}
        keep |= {p.rsplit("/", i)[0] for p in keep for i in range(1, p.count("/"))}
        for f in sorted(self._client.get_folders(target), key=lambda f: -f.path.count("/")):
            if f.id < 256 or f.path.lower() in keep:
                continue
            try:
                self._client.delete_folder(target, f.id)
                result.folders_deleted += 1
            except SoapFaultError as exc:
                if exc.code != "mail.NO_SUCH_FOLDER":
                    log.warning("standby: folder %s not deleted (%s)", f.path, exc)
        result.folders_created = len(tree.created)
        state.snapshot = snapshot_id
        state.save()
        return result

    # ------------------------------------------------------------------ internals

    def _adopt_clone(
        self,
        target: str,
        wanted: dict[str, dict[str, Any]],
        state: _State,
        result: StandbyResult,
    ) -> None:
        """First sync on a clone: map the items it already has (same id, same kind)."""
        on_clone: dict[int, tuple[str, int]] = {}
        folder_paths: dict[int, str] = {}
        cursor: str | None = None
        while True:
            delta = self._client.sync(target, cursor, change_limit=1000)
            for f in delta.folders:
                folder_paths[f.id] = f.path
            for kind, item_id, folder_id, _attrs in delta.changed:
                on_clone[item_id] = (kind, folder_id)
            cursor = delta.token
            if not delta.more:
                break
        extra: list[int] = []
        for item_id, (kind, folder_id) in on_clone.items():
            entry = wanted.get(str(item_id))
            if kind not in KINDS:
                continue
            if entry is None or entry["kind"] != kind:
                if kind == "message":
                    extra.append(item_id)  # deleted on the main server since the clone
                continue
            if kind == "message":
                state.items[str(item_id)] = {
                    "sid": item_id,
                    "kind": kind,
                    "blob": entry["blob"],
                    "meta": None,  # flags written once
                    "folder": folder_paths.get(folder_id, "").lower(),
                }
        for start in range(0, len(extra), DELETE_BATCH):
            batch = extra[start : start + DELETE_BATCH]
            try:
                self._client.item_action(target, batch, "delete")
                result.deleted += len(batch)
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                log.warning("standby: clone items not deleted (%s)", exc)
        log.info(
            "standby: clone adopted, %s message(s) mapped, %s deleted",
            len(state.items),
            len(extra),
        )

    def _flags(self, entry: dict[str, Any]) -> tuple[str, str]:
        if not entry.get("meta_blob"):
            return "", ""
        meta = json.loads(self._repo.get(entry["meta_blob"]))
        flags = "".join(c for c in str(meta.get("flags") or "") if c in KEPT_FLAGS)
        return flags, str(meta.get("tags") or "")

    def _add_message(self, target: str, entry: dict[str, Any], folder_id: int) -> int:
        raw = self._repo.get(entry["blob"])
        self._throttle.consume(len(raw))
        meta = json.loads(self._repo.get(entry["meta_blob"])) if entry.get("meta_blob") else {}
        flags = "".join(c for c in str(meta.get("flags") or "") if c in KEPT_FLAGS)
        tags = str(meta.get("tags") or "")
        received = int(meta["date_ms"]) if meta.get("date_ms") else None
        try:
            return self._client.add_message(
                target, folder_id, raw, flags=flags, tags=tags, received_ms=received
            )
        except SoapFaultError as exc:
            if not tags or exc.code in FATAL_FAULTS:
                raise
            return self._client.add_message(
                target, folder_id, raw, flags=flags, received_ms=received
            )

    def _sync_other_kinds(
        self,
        target: str,
        name: str,
        wanted: dict[str, dict[str, Any]],
        paths: dict[int, str],
        views: dict[str, str],
        tree: _Folders,
        state: _State,
        result: StandbyResult,
    ) -> None:
        by_folder: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for key, entry in wanted.items():
            if entry["kind"] in IMPORT_FORMAT:
                path = paths.get(int(entry["folder"])) or (
                    "/Contacts" if entry["kind"] == "contact" else "/Calendar"
                )
                by_folder.setdefault(path, []).append((key, entry))
        for path, entries in sorted(by_folder.items()):
            fingerprint = ",".join(sorted(f"{k}:{e['blob']}" for k, e in entries))
            if state.folders.get(path.lower()) == fingerprint:
                continue
            kind = entries[0][1]["kind"]
            view = _view(kind)
            try:
                folder_id = tree.ensure(path, view, {**views, path.lower(): view})
                self._client.empty_folder(target, folder_id)
                for key, entry in sorted(entries, key=lambda e: int(e[0])):
                    raw = self._repo.get(entry["blob"])
                    self._throttle.consume(len(raw))
                    try:
                        self._client.import_rest(
                            name, target, path, IMPORT_FORMAT[entry["kind"]], raw
                        )
                    except (SoapFaultError, httpx.HTTPError) as exc:  # one item, not the folder
                        result.failed[key] = getattr(exc, "code", type(exc).__name__)
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                log.error("standby: folder %s not replicated: %s", path, exc)
                continue
            state.folders[path.lower()] = fingerprint
            result.reimported_folders += 1
        # Folders of these kinds now empty on the main server.
        present = {k.lower() for k in by_folder}
        stale = [p for p in state.folders if p not in present]
        current = {f.path.lower(): f.id for f in self._client.get_folders(target)} if stale else {}
        for path in stale:
            stale_id = current.get(path)
            if stale_id is not None:
                try:
                    self._client.empty_folder(target, stale_id)
                except SoapFaultError as exc:
                    log.warning("standby: folder %s not emptied (%s)", path, exc)
            state.folders.pop(path, None)
