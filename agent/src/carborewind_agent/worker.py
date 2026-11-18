"""Agent main loop: inventory, claim a job, run it with heartbeats, report the result."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from .api_client import AgentApi, ApiError
from .backup import AccountBackup
from .carbonio import CarbonioClient, SoapFaultError
from .restore import AccountRestore, RestoreError

log = logging.getLogger(__name__)

# Carbonio errors that will not fix themselves by retrying the job.
PERMANENT_FAULTS = {"account.NO_SUCH_ACCOUNT", "service.PERM_DENIED", "account.AUTH_FAILED"}


class Worker:
    def __init__(
        self,
        api: AgentApi,
        carbonio: CarbonioClient,
        engine: AccountBackup,
        restore: AccountRestore | None = None,
        *,
        poll_seconds: float = 10.0,
        inventory_every: int = 60,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self._api = api
        self._carbonio = carbonio
        self._engine = engine
        self._restore = restore
        self._poll = poll_seconds
        self._inventory_every = inventory_every
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # returns True when stopping

    def stop(self) -> None:
        self._stop.set()

    def sync_inventory(self) -> dict[str, Any]:
        accounts = [
            {"carbonio_id": a.id, "email": a.name, "status": a.status or "active"}
            for a in self._carbonio.iter_accounts()
        ]
        return self._api.inventory(accounts, complete=True)

    def run_forever(self) -> None:
        loops = 0
        while not self._stop.is_set():
            if loops % self._inventory_every == 0:
                try:
                    self.sync_inventory()
                except (SoapFaultError, ApiError) as exc:
                    log.error("inventory failed", extra={"error": str(exc)})
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
        else:
            self._api.fail(job_id, f"unsupported job kind {job['kind']}", retryable=False)
            return True
        latest: dict[str, Any] = {"checkpoint": job.get("checkpoint")}
        stop_beat = threading.Event()

        def beat() -> None:
            interval = max(5.0, job["lease_seconds"] / 3)
            while not stop_beat.wait(interval):
                try:
                    self._api.heartbeat(job_id, latest["checkpoint"])
                except ApiError as exc:
                    log.warning("heartbeat failed", extra={"job": job_id, "error": str(exc)})

        beater = threading.Thread(target=beat, daemon=True)
        beater.start()
        try:
            runner(job, lambda state: latest.__setitem__("checkpoint", state))
        except (SoapFaultError, RestoreError) as exc:
            stop_beat.set()
            log.error("job %s failed: %s", job_id, exc)
            self._api.heartbeat(job_id, latest["checkpoint"])  # keep progress for the retry
            retryable = isinstance(exc, SoapFaultError) and exc.code not in PERMANENT_FAULTS
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
