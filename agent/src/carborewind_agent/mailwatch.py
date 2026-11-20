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
from .authlog import AuthAggregator
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
        mailbox_log: Path | None = None,
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
        self._filter_dirty = True
        self.agg = Aggregator()
        path = first_existing(log_paths) or log_paths[0]
        self.reader = LogReader(path)
        # Failed logins and IPs blocked by Carbonio (mailbox log + SASL lines of the MTA log).
        self.auth = AuthAggregator()
        self.mailbox_reader = LogReader(mailbox_log) if mailbox_log else None
        self._mailbox_denied = False
        extra = load_state(state_file, self.reader, self.agg)
        if self.mailbox_reader is not None:
            self.mailbox_reader.restore(extra.get("mailbox_reader") or {})
        self.auth.restore(extra.get("auth") or {})
        self.auth.dirty = set(extra.get("auth_dirty") or [])

    def mailbox_status(self) -> dict[str, Any] | None:
        """Whether mailbox.log can be read, shown in the interface."""
        if self.mailbox_reader is None:
            return None
        path = self.mailbox_reader.path
        return {
            "path": str(path),
            "exists": path.exists(),
            "readable": path.exists() and not self._mailbox_denied,
        }

    def _read_mailbox(self) -> list[str]:
        if self.mailbox_reader is None:
            return []
        try:
            lines = self.mailbox_reader.read_new()
        except PermissionError:
            if not self._mailbox_denied:
                log.warning(
                    "cannot read %s (add the carborewind user to the zextras group): "
                    "failed logins and blocked IPs are not reported",
                    self.mailbox_reader.path,
                )
            self._mailbox_denied = True
            return []
        self._mailbox_denied = False
        return lines

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
        login_filter = None
        if self._ticks % self._refresh_every == 0 or self._filter_dirty:
            try:
                login_filter = self._carbonio.login_filter()
                self._filter_dirty = False
            except SoapFaultError as exc:
                log.warning("cannot read the invalid login filter settings: %s", exc)
        self._ticks += 1
        path = first_existing(self._paths)
        if path is not None and path != self.reader.path:
            self.reader = LogReader(path)
        lines = self.reader.read_new() if path is not None else []
        for line in lines:
            self.agg.feed(line, now)
            self.auth.feed_mta(line, now)
        for line in self._read_mailbox():
            self.auth.feed_mailbox(line)
        self.agg.prune(now)
        self.auth.prune(now)
        answer = self._api.mail_stats(
            self.agg.payload(),
            str(path) if path else None,
            self.auth.payload(),
            self.mailbox_status(),
            login_filter,
        )
        self.agg.dirty.clear()
        self.auth.dirty.clear()
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        save_state(
            self._state_file,
            self.reader,
            self.agg,
            {
                "mailbox_reader": self.mailbox_reader.state() if self.mailbox_reader else {},
                "auth": self.auth.to_json(),
                "auth_dirty": sorted(self.auth.dirty),
            },
        )
        for action in answer.get("actions", []):
            self.apply(action)
        for action in answer.get("ip_actions", []):
            self.apply_ip(action)
        return {"lines": len(lines), "actions": len(answer.get("actions", []))}

    def apply_ip(self, action: dict[str, Any]) -> None:
        """Trust (never block) or untrust an address in Carbonio's login filter."""
        error = None
        kind, ip = action.get("action"), str(action.get("ip", ""))
        if kind not in ("trust", "untrust"):
            error = f"unknown action {kind!r}"
        else:
            try:
                self._carbonio.set_trusted_ip(ip, kind == "trust")
                log.warning("address %s %sed in the invalid login filter", ip, kind)
            except SoapFaultError as exc:
                error = f"{exc.code}: {exc}"[:500]
        self._filter_dirty = True  # report the new list on the next pass
        self._api.ip_action_done(str(action["id"]), error)

    def apply(self, action: dict[str, Any]) -> None:
        kind = str(action.get("action"))
        status = STATUSES.get(kind)
        error = None
        protected = str(action.get("email", "")).lower() in self._protected
        if kind == "delete":
            if protected:
                error = "protected account (used by CarboRewind itself): not deleted"
            else:
                try:
                    self._carbonio.delete_account(str(action["carbonio_id"]))
                    log.warning("account %s deleted: %s", action.get("email"), action.get("reason"))
                except SoapFaultError as exc:
                    error = f"{exc.code}: {exc}"[:500]
            self._api.mail_action_done(str(action["id"]), error)
            return
        if status is None:
            error = f"unknown action {action.get('action')!r}"
        elif status == "locked" and protected:
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

    enabled = True  # Settings → Agent can turn the watcher off and on again

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.enabled:
                    self.tick()
            except (ApiError, SoapFaultError, OSError) as exc:
                log.error("mail statistics failed: %s", exc)
            except Exception:  # never let the watcher die silently
                log.exception("mail statistics crashed")
            if stop.wait(self._interval):
                break
