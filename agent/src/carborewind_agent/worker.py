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
from .repository import Repository
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


def account_payload(
    a: AccountInfo, cos: dict[str, str], used: dict[str, int] | None
) -> dict[str, Any]:
    """Inventory entry sent to the API (see ``AccountIn``)."""
    quota = _first(a.attrs, "zimbraMailQuota")
    cos_id = _first(a.attrs, "zimbraCOSId")
    payload: dict[str, Any] = {
        "carbonio_id": a.id,
        "email": a.name,
        "status": a.status or "active",
        "display_name": _first(a.attrs, "displayName"),
        "first_name": _first(a.attrs, "givenName"),
        "last_name": _first(a.attrs, "sn"),
        "aliases": sorted(a.attrs.get("zimbraMailAlias", [])),
        "cos": cos.get(cos_id, cos_id) if cos_id else None,
        "quota_bytes": int(quota) if quota and quota.isdigit() else None,
        "is_admin": _first(a.attrs, "zimbraIsAdminAccount") == "TRUE",
        "last_login_at": _generalized_time(_first(a.attrs, "zimbraLastLogonTimestamp")),
        "carbonio_created_at": _generalized_time(_first(a.attrs, "zimbraCreateTimestamp")),
    }
    if used is not None:
        payload["used_bytes"] = used.get(a.id, 0)
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
            cos = self._carbonio.cos_names()
        except SoapFaultError as exc:
            log.warning("cannot list classes of service: %s", exc)
            cos = {}
        try:
            used: dict[str, int] | None = self._carbonio.quota_usage()
        except SoapFaultError as exc:
            log.warning("cannot read quota usage: %s", exc)
            used = None
        accounts = [account_payload(a, cos, used) for a in self._carbonio.iter_accounts()]
        return self._api.inventory(accounts, complete=True, storage=self.storage_report())

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
        except (SoapFaultError, RestoreError, MigrationError, ImapError, ReplicaError) as exc:
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
        with self._repo.lock():
            result = replicate(self._repo.root, self._replica)
        self._api.replicate_complete(job["job_id"], result.to_payload())
        log.info(
            "job %s done: copy 02, %s file(s) copied (%s bytes), %s deleted, %s error(s)",
            job["job_id"],
            result.copied,
            result.copied_bytes,
            result.deleted,
            len(result.errors),
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
