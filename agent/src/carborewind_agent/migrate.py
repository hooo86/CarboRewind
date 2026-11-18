"""Copy a mailbox from a remote IMAP server into a Carbonio account.

The source is only read (``EXAMINE`` + ``BODY.PEEK[]``); messages are written with
``AddMsgRequest`` like a restore, keeping the received date and the flags a user sees
(unread, flagged, answered, forwarded, draft; "sent" for the Sent folder).

Folders
-------
The personal namespace prefix (``INBOX.`` on Courier/Cyrus) is removed and the hierarchy
delimiter becomes ``/``. ``INBOX`` becomes ``/Inbox``; folders flagged ``\\Sent``,
``\\Drafts``, ``\\Trash``, ``\\Junk`` (RFC 6154) or carrying a well-known name in English or
French (``Sent Items``, ``Éléments envoyés``, ``Corbeille``, ``Spam``…) go to the Carbonio
system folder. Virtual folders (Gmail "All Mail", "Starred") are skipped: their messages are
already in the real folders. With a ``root`` option everything goes below ``/<root>/``.

Incremental copy
----------------
The state stored by the API after each run records, per source folder, its UIDVALIDITY, the
highest UID copied and the UIDs that failed. A new run only fetches UIDs above that mark (plus
the failed ones), so the copy can be repeated until the MX switch for a short final delta.
When a folder has no usable state (first run into a mailbox that already has mail, or the
server reset its UIDs) every message is checked by Message-ID against the target folder
first, so nothing is duplicated.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date
from email.parser import BytesHeaderParser
from email.policy import compat32
from functools import partial
from typing import Any

from .carbonio import CarbonioClient, SoapFaultError
from .imap import ImapError, ImapSource, RemoteFolder
from .restore import FATAL_FAULTS, _Folders
from .throttle import Throttle

log = logging.getLogger(__name__)

SourceFactory = Callable[[], AbstractContextManager[ImapSource]]
Checkpoint = Callable[[dict[str, Any]], None]

SPECIAL_USE = {"\\sent": "Sent", "\\drafts": "Drafts", "\\trash": "Trash", "\\junk": "Junk"}
VIRTUAL = frozenset({"\\all", "\\flagged"})
SPECIAL_NAMES = {
    name: target
    for target, names in {
        "Sent": (
            "sent",
            "sent items",
            "sent messages",
            "sent mail",
            "envoyés",
            "envoyes",
            "messages envoyés",
            "éléments envoyés",
            "elements envoyes",
        ),
        "Drafts": ("drafts", "draft", "brouillons", "brouillon"),
        "Trash": (
            "trash",
            "deleted items",
            "deleted messages",
            "corbeille",
            "éléments supprimés",
            "elements supprimes",
        ),
        "Junk": (
            "junk",
            "spam",
            "junk e-mail",
            "junk email",
            "courrier indésirable",
            "courrier indesirable",
            "pourriel",
            "pourriels",
        ),
    }.items()
    for name in names
}
MAX_FAILED_PER_FOLDER = 1000
MAX_HEADER = 64 * 1024


class MigrationError(Exception):
    """The request cannot be executed (bad options)."""


# ------------------------------------------------------------------ pure helpers


def target_path(folder: RemoteFolder, prefix: str = "", root: str | None = None) -> str:
    """Carbonio path of a source folder."""
    name = folder.name
    if prefix and name.upper() != "INBOX" and name.startswith(prefix) and name != prefix:
        name = name[len(prefix) :]
    delim = folder.delimiter
    parts = [p for p in (name.split(delim) if delim else [name]) if p.strip()]
    if not parts:
        parts = ["Inbox"]
    special = next((SPECIAL_USE[f] for f in sorted(folder.flags) if f in SPECIAL_USE), None)
    if special:
        parts = [special]
    elif parts[0].upper() == "INBOX":
        parts[0] = "Inbox"
    else:
        parts[0] = SPECIAL_NAMES.get(parts[0].strip().lower(), parts[0])
    clean = [p.replace("/", "_").strip() for p in parts]
    path = "/" + "/".join(clean)
    return f"/{root}{path}" if root else path


def zimbra_flags(imap_flags: frozenset[str], sent_folder: bool) -> str:
    flags = ""
    if "\\seen" not in imap_flags:
        flags += "u"
    if "\\flagged" in imap_flags:
        flags += "f"
    if "\\answered" in imap_flags:
        flags += "r"
    if "$forwarded" in imap_flags or "\\forwarded" in imap_flags:
        flags += "w"
    if "\\draft" in imap_flags:
        flags += "d"
    if sent_folder:
        flags += "s"
    return flags


def message_id(raw: bytes) -> str | None:
    head = raw[:MAX_HEADER]
    for sep in (b"\r\n\r\n", b"\n\n"):
        if sep in head:
            head = head.split(sep, 1)[0]
            break
    value = BytesHeaderParser(policy=compat32).parsebytes(head).get("Message-ID")
    text = " ".join(str(value).split()) if value else ""
    return text or None


def excluded(folder: RemoteFolder, relative_target: str, patterns: list[str]) -> bool:
    """True when the source name or the target path (without root) matches a pattern."""
    source = folder.name.lower()
    target = relative_target.lower()
    for raw in patterns:
        p = raw.strip().strip("/").lower()
        if not p:
            continue
        if source == p or (folder.delimiter and source.startswith(p + folder.delimiter.lower())):
            return True
        if target == "/" + p or target.startswith("/" + p + "/"):
            return True
    return False


def _date(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


# ------------------------------------------------------------------ result


@dataclass
class FolderReport:
    source: str
    target: str
    total: int = 0
    new: int = 0
    copied: int = 0
    duplicates: int = 0
    failed: int = 0
    error: str | None = None


@dataclass
class MigrationResult:
    dry_run: bool
    folders: list[FolderReport] = field(default_factory=list)
    copied: int = 0
    duplicates: int = 0
    bytes: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    folders_created: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Body of ``POST /api/agent/jobs/{id}/migrate-complete``."""
        return {
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
            "copied": self.copied,
            "duplicates": self.duplicates,
            "bytes": self.bytes,
            "failed": dict(list(self.failed.items())[:500]),
            "failed_count": len(self.failed),
            "folders": [f.__dict__ for f in self.folders],
            "folders_created": self.folders_created[:500],
            "state": self.state,
        }

    def progress(self, folder: str, index: int, total: int) -> dict[str, Any]:
        return {
            "folder": folder,
            "folders_done": index,
            "folders_total": total,
            "copied": self.copied,
            "duplicates": self.duplicates,
            "failed": len(self.failed),
            "bytes": self.bytes,
        }


# ------------------------------------------------------------------ engine


class MailboxMigration:
    def __init__(
        self,
        client: CarbonioClient,
        throttle: Throttle | None = None,
        *,
        checkpoint_every: int = 25,
    ) -> None:
        self._client = client
        self._throttle = throttle or Throttle()
        self._every = checkpoint_every

    def run(
        self,
        source: SourceFactory,
        account_id: str,
        *,
        options: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        dry_run: bool = False,
        resume: dict[str, Any] | None = None,
        checkpoint: Checkpoint | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> MigrationResult:
        opts = options or {}
        root = str(opts.get("root") or "").strip().strip("/") or None
        if root and ("/" in root or len(root) > 128):
            raise MigrationError("invalid root folder")
        patterns = [str(p) for p in opts.get("exclude_folders") or []]
        since, before = _date(opts.get("since")), _date(opts.get("before"))
        stop = should_stop or (lambda: False)

        result = MigrationResult(dry_run=dry_run)
        if resume and not dry_run:
            result.state = copy.deepcopy(resume.get("state") or {})
            totals = resume.get("totals") or {}
            result.copied = int(totals.get("copied", 0))
            result.duplicates = int(totals.get("duplicates", 0))
            result.bytes = int(totals.get("bytes", 0))
            result.failed = dict(totals.get("failed") or {})
        else:
            result.state = copy.deepcopy(state or {})
        states: dict[str, Any] = result.state.setdefault("folders", {})

        def save(folder: str, index: int, total: int) -> None:
            if checkpoint and not dry_run:
                checkpoint(
                    {
                        "state": copy.deepcopy(result.state),
                        "totals": {
                            "copied": result.copied,
                            "duplicates": result.duplicates,
                            "bytes": result.bytes,
                            "failed": dict(list(result.failed.items())[:500]),
                        },
                        "progress": result.progress(folder, index, total),
                    }
                )

        with source() as src:
            prefix = src.personal_prefix()
            remote = [f for f in src.folders() if f.selectable and not (VIRTUAL & f.flags)]
            plan = []
            for f in remote:
                relative = target_path(f, prefix)
                if not excluded(f, relative, patterns):
                    plan.append((f, target_path(f, prefix, root)))
            target = _Folders(self._client, account_id, dry_run)
            existing = {p for p in target._ids if p}
            for index, (folder, path) in enumerate(plan):
                if stop():
                    result.cancelled = True
                    break
                report = FolderReport(folder.name, path)
                result.folders.append(report)
                try:
                    self._copy_folder(
                        src,
                        folder,
                        path,
                        target,
                        existing,
                        states,
                        report,
                        result,
                        account_id,
                        since,
                        before,
                        dry_run,
                        stop,
                        partial(save, folder.name, index, len(plan)),
                    )
                except ImapError as exc:
                    if exc.retryable:
                        save(folder.name, index, len(plan))
                        raise
                    report.error = str(exc)[:300]
                    log.warning("folder skipped", extra={"folder": folder.name, "error": str(exc)})
                save(folder.name, index + 1, len(plan))
            result.folders_created = target.created
        return result

    def _copy_folder(
        self,
        src: ImapSource,
        folder: RemoteFolder,
        path: str,
        target: _Folders,
        existing: set[str],
        states: dict[str, Any],
        report: FolderReport,
        result: MigrationResult,
        account_id: str,
        since: date | None,
        before: date | None,
        dry_run: bool,
        stop: Callable[[], bool],
        save: Callable[[], None],
    ) -> None:
        validity, report.total = src.examine(folder)
        previous = states.get(folder.raw) or {}
        known = bool(previous) and previous.get("uidvalidity") == validity
        last_uid = int(previous.get("last_uid", 0)) if known else 0
        retry = [int(u) for u in previous.get("failed", [])] if known else []
        candidates = sorted(set(retry) | set(src.uids(last_uid + 1, since, before)))
        report.new = len(candidates)
        if dry_run or not candidates:
            if not dry_run and not known:
                states[folder.raw] = {"uidvalidity": validity, "last_uid": 0, "failed": []}
            return
        dedupe = not known and path.lower() in existing
        folder_id = target.ensure(path, "message", {})
        sent = path.rsplit("/", 1)[-1] == "Sent"
        current: dict[str, Any] = {"uidvalidity": validity, "last_uid": last_uid, "failed": []}
        states[folder.raw] = current
        processed = 0
        for item in src.iter_fetch(candidates):
            if stop():
                result.cancelled = True
                return
            if isinstance(item, int):  # expunged meanwhile
                current["last_uid"] = max(current["last_uid"], item)
                continue
            uid = item.uid
            current["last_uid"] = max(current["last_uid"], uid)
            if "\\deleted" in item.flags:
                continue
            if dedupe:
                mid = message_id(item.raw)
                if mid and self._client.has_message_id(account_id, folder_id, mid):
                    report.duplicates += 1
                    result.duplicates += 1
                    continue
            self._throttle.consume(len(item.raw))
            try:
                self._client.add_message(
                    account_id,
                    folder_id,
                    item.raw,
                    flags=zimbra_flags(item.flags, sent),
                    received_ms=item.internal_ms,
                )
            except SoapFaultError as exc:
                if exc.code in FATAL_FAULTS:
                    raise
                report.failed += 1
                result.failed[f"{folder.name}:{uid}"] = exc.code
                if len(current["failed"]) < MAX_FAILED_PER_FOLDER:
                    current["failed"].append(uid)
                log.error("message copy failed", extra={"uid": uid, "code": exc.code})
                continue
            report.copied += 1
            result.copied += 1
            result.bytes += len(item.raw)
            processed += 1
            if processed % self._every == 0:
                save()
