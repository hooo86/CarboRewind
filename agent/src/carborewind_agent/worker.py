"""Agent main loop: inventory, claim a job, run it with heartbeats, report the result."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .api_client import AgentApi, ApiError
from .backup import AccountBackup
from .carbonio import AccountInfo, CarbonioClient, SoapFaultError
from .imap import ImapError, ImapSettings, ImapSource
from .migrate import MailboxMigration, MigrationError
from .purge import run_purge
from .replicate import ReplicaError, replicate
from .repository import Repository, RepositoryError
from .restore import AccountRestore, RestoreError
from .storage import describe

log = logging.getLogger(__name__)

# Carbonio errors that will not fix themselves by retrying the job.
PERMANENT_FAULTS = {"account.NO_SUCH_ACCOUNT", "service.PERM_DENIED", "account.AUTH_FAILED"}


def _first(attrs: dict[str, list[str]], name: str) -> str | None:
    values = attrs.get(name)
    return values[0] or None if values else None


def _generalized_time(value: str | None) -> str | None:
    """LDAP generalized time (``20261118093000Z`` or ``20261118093000.123Z``) -> ISO 8601."""
    if not value or len(value) < 14 or not value[:14].isdigit():
        return None
    v = value[:14]
    return f"{v[:4]}-{v[4:6]}-{v[6:8]}T{v[8:10]}:{v[10:12]}:{v[12:14]}+00:00"


# LDAP attribute -> profile field shown in the interface.
PROFILE_ATTRS = {
    "title": "title",
    "company": "company",
    "ou": "department",
    "telephoneNumber": "phone",
    "mobile": "mobile",
    "homePhone": "home_phone",
    "street": "street",
    "postalCode": "postal_code",
    "l": "city",
    "st": "state",
    "co": "country",
    "description": "description",
    "zimbraNotes": "notes",
    "employeeNumber": "employee_number",
}
# Mailboxes GetQuotaUsage did not list are measured one by one, this many per inventory
# (in turn: the next inventory continues where this one stopped), to spare Carbonio.
MAX_SIZE_LOOKUPS = 200
GIB = 1024**3


def account_payload(
    a: AccountInfo,
    cos: dict[str, tuple[str, int | None]] | dict[str, str],
    used: dict[str, int] | None,
    limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Inventory entry sent to the API (see ``AccountIn``).

    Quota: the account's own ``zimbraMailQuota``, else the effective limit reported by
    ``GetQuotaUsage``, else its class of service (0 = unlimited). Usage is only sent when it
    is known: an account missing from the answers is left unknown, never shown as 0.
    """
    quota = _first(a.attrs, "zimbraMailQuota")
    cos_id = _first(a.attrs, "zimbraCOSId")
    cos_entry = cos.get(cos_id) if cos_id else None
    cos_name, cos_quota = cos_entry if isinstance(cos_entry, tuple) else (cos_entry, None)
    if quota and quota.isdigit():
        quota_bytes: int | None = int(quota)
    elif limits and limits.get(a.id):
        quota_bytes = limits[a.id]
    else:
        quota_bytes = cos_quota
    payload: dict[str, Any] = {
        "carbonio_id": a.id,
        "email": a.name,
        "status": a.status or "active",
        "display_name": _first(a.attrs, "displayName"),
        "first_name": _first(a.attrs, "givenName"),
        "last_name": _first(a.attrs, "sn"),
        "aliases": sorted(a.attrs.get("zimbraMailAlias", [])),
        "cos": (cos_name or cos_id) if cos_id else None,
        "quota_bytes": quota_bytes,
        "is_admin": _first(a.attrs, "zimbraIsAdminAccount") == "TRUE",
        "last_login_at": _generalized_time(_first(a.attrs, "zimbraLastLogonTimestamp")),
        "carbonio_created_at": _generalized_time(_first(a.attrs, "zimbraCreateTimestamp")),
        "profile": {
            field: value[:1000]
            for attr, field in PROFILE_ATTRS.items()
            if (value := _first(a.attrs, attr))
        },
    }
    if used is not None and a.id in used:
        payload["used_bytes"] = used[a.id]
    return payload


class Worker:
    def __init__(
        self,
        api: AgentApi,
        carbonio: CarbonioClient,
        engine: AccountBackup,
        restore: AccountRestore | None = None,
        repo: Repository | None = None,
        *,
        migration: MailboxMigration | None = None,
        storage_dir: Path | None = None,
        replica: Path | None = None,
        storage_every: int = 3,
        kek: bytes | None = None,
        space: tuple[float, float, float] = (20.0, 15.0, 2.0),
        poll_seconds: float = 10.0,
        inventory_every: int = 60,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self._api = api
        self._carbonio = carbonio
        self._engine = engine
        self._restore = restore
        self._repo = repo
        self._migration = migration
        self._storage_dir = storage_dir
        self._replica = replica
        self._storage_every = storage_every
        self._kek = kek
        self._space = space  # GiB local, % local, GiB network
        self._size_cursor = 0
        self._requested_revision: int | None = None
        self._reported_status: float | None = None
        # Set when the API answers 409 to a heartbeat: the job was cancelled meanwhile.
        self._cancelled = threading.Event()
        self._poll = poll_seconds
        self._inventory_every = inventory_every
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # returns True when stopping

    def stop(self) -> None:
        self._stop.set()

    def sync_inventory(self) -> dict[str, Any]:
        # Optional enrichments: an older Carbonio or a restricted admin may refuse them.
        try:
            cos: dict[str, tuple[str, int | None]] = self._carbonio.cos_info()
        except SoapFaultError as exc:
            log.warning("cannot list classes of service: %s", exc)
            cos = {}
        used: dict[str, int] = {}
        limits: dict[str, int] = {}
        try:
            for account_id, (bytes_used, limit) in self._carbonio.quota_usage_detail().items():
                used[account_id], limits[account_id] = bytes_used, limit
        except SoapFaultError as exc:
            log.warning("cannot read quota usage: %s", exc)
        infos = list(self._carbonio.iter_accounts())
        # GetQuotaUsage may leave accounts out (other server, cache): measure those directly.
        missing = [i for i in infos if i.id not in used]
        if missing:
            start = self._size_cursor % len(missing)
            for info in (missing[start:] + missing[:start])[:MAX_SIZE_LOOKUPS]:
                try:
                    used[info.id] = self._carbonio.mailbox_size(info.id)
                except SoapFaultError as exc:
                    log.debug("no mailbox size for %s: %s", info.name, exc)
            self._size_cursor = start + MAX_SIZE_LOOKUPS
        accounts = [account_payload(a, cos, used, limits) for a in infos]
        return self._api.inventory(accounts, complete=True, storage=self.storage_report())

    def space_floor(self, path: Path) -> int:
        """Bytes that must stay free on the file system holding ``path``."""
        info = describe(path)
        gib_local, pct_local, gib_net = self._space
        if info.get("kind") == "network":
            return int(gib_net * GIB)
        total = int(info.get("total_bytes") or 0)  # type: ignore[call-overload]
        return int(max(gib_local * GIB, total * pct_local / 100))

    # ------------------------------------------------------------------ storage settings

    def _status_file(self) -> Path | None:
        return self._storage_dir / "status.json" if self._storage_dir else None

    def storage_report(self) -> dict[str, Any] | None:
        """Where the repository and copy 02 are, and how the last settings were applied."""
        if self._repo is None:
            return None
        report: dict[str, Any] = describe(self._repo.root)
        report["replica"] = describe(self._replica) if self._replica else None
        status_file = self._status_file()
        if status_file is not None and status_file.exists():
            try:
                status = json.loads(status_file.read_text())
                report["apply"] = {
                    k: status.get(k) for k in ("revision", "ok", "error", "at", "changed")
                }
            except (OSError, ValueError):
                report["apply"] = {"ok": False, "error": "unreadable status file"}
        return report

    def sync_storage(self) -> None:
        """Hand new storage settings to the root helper; report its outcome."""
        if self._storage_dir is None:
            return
        desired = self._api.storage_config()
        status_file = self._status_file()
        assert status_file is not None  # noqa: S101 - storage_dir is set
        applied = 0
        if status_file.exists():
            try:
                applied = int(json.loads(status_file.read_text()).get("revision") or 0)
            except (OSError, ValueError):
                applied = 0
        revision = int(desired.get("revision") or 0) if desired else 0
        if desired and revision > applied and revision != self._requested_revision:
            self._storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self._storage_dir / f".request-{uuid.uuid4().hex}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, json.dumps(desired).encode())
            finally:
                os.close(fd)
            os.replace(tmp, self._storage_dir / "request.json")
            self._requested_revision = revision
            log.info("storage settings revision %s handed to the root helper", revision)
        mtime = status_file.stat().st_mtime if status_file.exists() else None
        if mtime is not None and mtime != self._reported_status:
            report = self.storage_report()
            if report is not None:
                self._api.storage_status(report)
            self._reported_status = mtime

    def run_forever(self) -> None:
        loops = 0
        while not self._stop.is_set():
            if loops % self._inventory_every == 0:
                try:
                    self.sync_inventory()
                except (SoapFaultError, ApiError) as exc:
                    log.error("inventory failed", extra={"error": str(exc)})
            if loops % self._storage_every == 0:
                try:
                    self.sync_storage()
                except (ApiError, OSError) as exc:
                    log.error("storage settings sync failed", extra={"error": str(exc)})
            loops += 1
            try:
                worked = self.run_once()
            except ApiError as exc:
                log.error("API unreachable", extra={"error": str(exc)})
                worked = False
            if not worked and self._sleep(self._poll):
                break

    def run_once(self) -> bool:
        """Claim and execute at most one job. Returns False when there was nothing to do."""
        job = self._api.claim()
        if job is None:
            return False
        job_id = job["job_id"]
        log.info(
            "job %s claimed: %s of %s (attempt %s)",
            job_id,
            job["kind"],
            job.get("account_email"),
            job.get("attempt"),
        )
        runner: Callable[[dict[str, Any], Callable[[dict[str, Any]], None]], None]
        if job["kind"] == "backup":
            runner = self._run_backup
        elif job["kind"] == "restore" and self._restore is not None:
            runner = self._run_restore
        elif job["kind"] == "purge" and self._repo is not None:
            runner = self._run_purge
        elif job["kind"] == "migrate" and self._migration is not None:
            runner = self._run_migrate
        elif job["kind"] == "replicate" and self._repo is not None and self._replica:
            runner = self._run_replicate
        elif job["kind"] == "verify" and self._repo is not None:
            runner = self._run_verify
        else:
            self._api.fail(job_id, f"unsupported job kind {job['kind']}", retryable=False)
            return True
        latest: dict[str, Any] = {"checkpoint": job.get("checkpoint")}
        stop_beat = threading.Event()
        self._cancelled.clear()

        def progress() -> dict[str, Any] | None:
            cp = latest["checkpoint"]
            value = cp.get("progress") if isinstance(cp, dict) else None
            return value if isinstance(value, dict) else None

        def beat() -> None:
            interval = max(5.0, job["lease_seconds"] / 3)
            while not stop_beat.wait(interval):
                try:
                    self._api.heartbeat(job_id, latest["checkpoint"], progress())
                except ApiError as exc:
                    if exc.status == 409:
                        self._cancelled.set()
                    log.warning("heartbeat failed", extra={"job": job_id, "error": str(exc)})

        beater = threading.Thread(target=beat, daemon=True)
        beater.start()
        try:
            runner(job, lambda state: latest.__setitem__("checkpoint", state))
        except (
            SoapFaultError,
            RestoreError,
            MigrationError,
            ImapError,
            ReplicaError,
            RepositoryError,
        ) as exc:
            stop_beat.set()
            log.error("job %s failed: %s", job_id, exc)
            self._api.heartbeat(job_id, latest["checkpoint"])  # keep progress for the retry
            if isinstance(exc, SoapFaultError):
                retryable = exc.code not in PERMANENT_FAULTS
            else:
                retryable = isinstance(exc, ImapError) and exc.retryable
            self._api.fail(job_id, str(exc), retryable=retryable)
        except Exception as exc:  # unexpected: retry, the checkpoint limits the cost
            stop_beat.set()
            log.exception("job crashed", extra={"job": job_id})
            self._api.heartbeat(job_id, latest["checkpoint"])
            self._api.fail(job_id, repr(exc), retryable=True)
        finally:
            stop_beat.set()
            beater.join(timeout=5)
        return True

    def _run_backup(
        self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]
    ) -> None:
        if self._repo is not None:
            self._repo.min_free_bytes = self.space_floor(self._repo.root)
            self._repo.check_space()
        previous = job.get("previous")
        result = self._engine.run(
            job["carbonio_id"],
            job["account_email"],
            job["snapshot_id"],
            int(job["seq"]),
            (previous["snapshot_id"], previous["token"]) if previous else None,
            resume=job.get("checkpoint"),
            checkpoint=checkpoint,
        )
        self._api.complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: %s backup of %s, seq %s, %s items, %s new bytes%s",
            job["job_id"],
            result.backup_type,
            job.get("account_email"),
            result.seq,
            result.item_count,
            result.new_bytes,
            f", {len(result.errors)} item error(s)" if result.errors else "",
        )

    def _run_purge(self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]) -> None:
        assert self._repo is not None  # noqa: S101 - checked by the caller
        result = run_purge(self._repo, job["params"])
        self._api.purge_complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: purge, %s manifest(s) and %s blob(s) deleted, %s bytes freed",
            job["job_id"],
            len(result.manifests_deleted),
            len(result.blobs_deleted),
            result.freed_bytes,
        )

    def _run_restore(
        self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]
    ) -> None:
        assert self._restore is not None  # noqa: S101 - checked by the caller
        p = job["params"]
        result = self._restore.run(
            p["source_carbonio_id"],
            p["snapshot_id"],
            job["carbonio_id"],
            job["account_email"],
            mode=p.get("mode", "new_folder"),
            label=p.get("label", "CarboRewind"),
            folder_ids=p.get("folder_ids") or [],
            item_ids=p.get("item_ids") or [],
            dry_run=bool(p.get("dry_run")),
            resume=job.get("checkpoint"),
            checkpoint=checkpoint,
        )
        self._api.restore_complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: %srestore into %s, %s restored, %s present, %s failed",
            job["job_id"],
            "dry-run " if result.dry_run else "",
            job.get("account_email"),
            result.restored,
            result.present,
            len(result.failed),
        )

    def _run_replicate(
        self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]
    ) -> None:
        assert self._repo is not None and self._replica is not None  # noqa: S101
        floor = int(self._space[2] * GIB)
        with self._repo.lock():
            result = replicate(self._repo.root, self._replica, min_free_bytes=floor)
        self._api.replicate_complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: copy 02, %s file(s) copied (%s bytes), %s deleted, %s error(s)",
            job["job_id"],
            result.copied,
            result.copied_bytes,
            result.deleted,
            len(result.errors),
        )

    def _run_verify(
        self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]
    ) -> None:
        """Decrypt and authenticate the repository (or copy 02), fully or a sample."""
        assert self._repo is not None  # noqa: S101 - checked by the caller
        params = job.get("params") or {}
        target = params.get("target", "primary")
        sample = float(params.get("sample", 1.0))
        if target == "replica":
            if not self._replica or self._kek is None:
                raise ReplicaError("copy 02 is not configured on this agent")
            if not (self._replica / "config.json").exists():
                raise ReplicaError("copy 02 holds no repository yet")
            repo = Repository.open(self._replica, self._kek)
        else:
            repo = self._repo
        with repo.lock():
            report = repo.verify(sample=sample)
        self._api.verify_complete(
            job["job_id"],
            {
                "target": target,
                "sample": sample,
                "checked": report.checked,
                "corrupted": report.corrupted[:100],
                "corrupted_count": len(report.corrupted),
                "manifests_checked": report.manifests_checked,
                "manifests_corrupted": report.manifests_corrupted[:100],
            },
        )
        log.info(
            "job %s done: verify %s (%s of the blobs), %s checked, %s corrupted",
            job["job_id"],
            target,
            sample,
            report.checked,
            len(report.corrupted) + len(report.manifests_corrupted),
        )

    def _run_migrate(
        self, job: dict[str, Any], checkpoint: Callable[[dict[str, Any]], None]
    ) -> None:
        assert self._migration is not None  # noqa: S101 - checked by the caller
        spec = job.get("migration") or {}
        if "imap" not in spec:
            raise MigrationError("migration job without source settings")
        imap = ImapSettings.from_job(spec["imap"])
        result = self._migration.run(
            lambda: ImapSource(imap),
            job["carbonio_id"],
            options=spec.get("options"),
            state=spec.get("state"),
            dry_run=bool(spec.get("dry_run")),
            resume=job.get("checkpoint"),
            checkpoint=checkpoint,
            should_stop=self._cancelled.is_set,
        )
        self._api.migrate_complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: %smigration into %s, %s copied, %s duplicate(s), %s failed%s",
            job["job_id"],
            "dry-run " if result.dry_run else "",
            job.get("account_email"),
            result.copied,
            result.duplicates,
            len(result.failed),
            " (cancelled)" if result.cancelled else "",
        )
