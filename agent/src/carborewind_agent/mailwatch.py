"""Mail traffic watcher: a thread of the agent, independent of the job loop.

Every minute it reads the new MTA log lines, sends the hourly counters to the server and
applies the account actions the server answers with (lock a mailbox that went over a
sending limit, unlock it on request). Running beside the job loop matters: a spamming
account must be stopped within minutes, even while a long backup is running.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .api_client import AgentApi, ApiError
from .carbonio import CarbonioClient, SoapFaultError
from .maillog import Aggregator, LogReader, first_existing, load_state, save_state

log = logging.getLogger(__name__)
STATUSES = {"lock": "locked", "unlock": "active"}


class MailMonitor:
    def __init__(
        self,
        api: AgentApi,
        carbonio: CarbonioClient,
        log_paths: list[Path],
        state_file: Path,
        *,
        interval: float = 60.0,
        refresh_every: int = 10,
        protected: set[str] | None = None,
    ) -> None:
        self._api = api
        self._carbonio = carbonio
        self._paths = log_paths
        self._state_file = state_file
        self._interval = interval
        self._refresh_every = refresh_every
        # Never locked from here: the agent's own Carbonio admin account (locking it would
        # cut CarboRewind off from Carbonio).
        self._protected = {p.lower() for p in protected or set()}
        self._ticks = 0
        self.agg = Aggregator()
        path = first_existing(log_paths) or log_paths[0]
        self.reader = LogReader(path)
        load_state(state_file, self.reader, self.agg)

    def refresh_addresses(self) -> None:
        local: dict[str, str] = {}
        for account in self._carbonio.iter_accounts():
            name = account.name.lower()
            local[name] = name
            for alias in account.attrs.get("zimbraMailAlias", []):
                local.setdefault(alias.lower(), name)
        self.agg.local = local

    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        if self._ticks % self._refresh_every == 0 or not self.agg.local:
            try:
                self.refresh_addresses()
            except SoapFaultError as exc:
                log.warning("cannot list accounts for mail statistics: %s", exc)
        self._ticks += 1
        path = first_existing(self._paths)
        if path is not None and path != self.reader.path:
            self.reader = LogReader(path)
        lines = self.reader.read_new() if path is not None else []
        for line in lines:
            self.agg.feed(line, now)
        self.agg.prune(now)
        answer = self._api.mail_stats(self.agg.payload(), str(path) if path else None)
        self.agg.dirty.clear()
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        save_state(self._state_file, self.reader, self.agg)
        for action in answer.get("actions", []):
            self.apply(action)
        return {"lines": len(lines), "actions": len(answer.get("actions", []))}

    def apply(self, action: dict[str, Any]) -> None:
        status = STATUSES.get(str(action.get("action")))
        error = None
        if status is None:
            error = f"unknown action {action.get('action')!r}"
        elif status == "locked" and str(action.get("email", "")).lower() in self._protected:
            error = "protected account (used by CarboRewind itself): not locked"
        else:
            try:
                self._carbonio.set_account_status(str(action["carbonio_id"]), status)
                log.warning(
                    "account %s set to %s: %s", action.get("email"), status, action.get("reason")
                )
            except SoapFaultError as exc:
                error = f"{exc.code}: {exc}"[:500]
        self._api.mail_action_done(str(action["id"]), error)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except (ApiError, SoapFaultError, OSError) as exc:
                log.error("mail statistics failed: %s", exc)
            except Exception:  # never let the watcher die silently
                log.exception("mail statistics crashed")
            if stop.wait(self._interval):
                break
