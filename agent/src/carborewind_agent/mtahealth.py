"""Postfix health, read from the MTA log and from the optional Postfix guard.

The ports and the Carbonio service list say that Postfix *runs*; they do not say that it
can *deliver*. After a Carbonio upgrade, ``master.cf`` may come back as the stock Postfix
file: Postfix and Amavis both run, port 25 answers, yet every message stays deferred with
"mail transport unavailable". These checks catch that from the log the agent already reads
for the mail statistics (``/var/log/carbonio.log``, group ``adm``), with no extra rights:

* ``mta:antivirus`` — the content filter circuit (Postfix → Amavis :10024 → Postfix
  :10025). Down on "connect to transport private/smtp-amavis … refused" or "mail transport
  unavailable"; back as soon as a message is handed to Amavis again.
* ``mta:milter`` — a milter Postfix must call does not answer (messages refused with 451).
* ``mta:policy`` — the policy server (cbpolicyd, :10031) does not answer (mail refused).

A check is down when its last failure is less than :data:`WINDOW` old and no success came
after it; it is reported up otherwise (the server then sends the "back" e-mail).

``guard:master.cf`` reports the status file of the Postfix guard, when installed (a root
script that restores a known-good ``master.cf``): circuit lines, ``dpkg-divert`` in place,
last automatic repair, new ``master.cf`` shipped by an upgrade.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WINDOW = 10 * 60  # seconds
FIRST_READ = 256 * 1024  # bytes read back from the end on the first pass
MAX_READ = 4 * 2**20  # bytes read per pass at most (a huge backlog is skipped)
GUARD_STATUS = Path("/var/lib/postfix-guard/status.json")
GUARD_STALE = 30 * 60  # seconds: the guard runs every 5 minutes

# (check, failure pattern, success pattern or None)
CHECKS: tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        "mta:antivirus",
        re.compile(
            r"connect to transport private/smtp-amavis\b|\(mail transport unavailable\)", re.I
        ),
        re.compile(r"relay=[^ ,]*\[127\.0\.0\.1\]:10024,.*status=sent", re.I),
    ),
    (
        "mta:milter",
        re.compile(r"connect to Milter service .*(refused|timed out)", re.I),
        None,
    ),
    (
        "mta:policy",
        re.compile(r"(problem talking to server|connect to) [^ ]*:10031\b", re.I),
        None,
    ),
)


@dataclass
class _Check:
    last_fail: float | None = None
    last_ok: float | None = None
    detail: str | None = None

    def down(self, now: float) -> bool:
        if self.last_fail is None or now - self.last_fail > WINDOW:
            return False
        return self.last_ok is None or self.last_ok < self.last_fail


@dataclass
class MtaHealth:
    """Follows the MTA log between two reports (offset and inode kept in memory)."""

    logs: list[Path]
    guard_status: Path = GUARD_STATUS
    _path: Path | None = None
    _inode: int | None = None
    _offset: int = 0
    _aligned: bool = False  # the last read ended on a line break
    _checks: dict[str, _Check] = field(default_factory=dict)
    error: str | None = None

    def _log(self) -> Path | None:
        for path in self.logs:
            if path.exists():
                return path
        return None

    def _read_new(self) -> list[str]:
        path = self._log()
        if path is None:
            self.error = "MTA log not found"
            return []
        try:
            st = path.stat()
            with path.open("rb") as fh:
                if path != self._path or st.st_ino != self._inode or st.st_size < self._offset:
                    first = path != self._path or self._inode is None
                    self._path, self._inode = path, st.st_ino
                    # First pass: the recent past tells the current state; after a log
                    # rotation, the new file is read from its start.
                    self._offset = max(0, st.st_size - FIRST_READ) if first else 0
                    self._aligned = self._offset == 0
                start = max(self._offset, st.st_size - MAX_READ)
                # Reading from the middle of a line (first pass, skipped backlog)?
                partial = start > 0 and (start != self._offset or not self._aligned)
                fh.seek(start)
                data = fh.read(st.st_size - start)
                self._offset = start + len(data)
                self._aligned = data.endswith(b"\n") or (not data and self._aligned)
        except PermissionError:
            self.error = f"{path} is not readable by the agent (group adm)"
            return []
        except OSError as exc:
            self.error = f"{path}: {exc}"
            return []
        self.error = None
        if partial and data:
            data = data.split(b"\n", 1)[-1]  # drop a partial first line
        return data.decode("utf-8", "replace").splitlines()

    def _scan(self, lines: list[str], now: float) -> None:
        for name, fail, ok in CHECKS:
            check = self._checks.setdefault(name, _Check())
            last: str | None = None  # "fail" / "ok": the latest relevant line wins
            for line in lines:
                if fail.search(line):
                    last = "fail"
                    check.detail = line.split(": ", 1)[-1][:200]
                elif ok is not None and ok.search(line):
                    last = "ok"
            if last == "fail":
                check.last_fail = now
            elif last == "ok":
                check.last_ok = now

    def _guard(self, now: float) -> dict[str, Any] | None:
        try:
            data = json.loads(self.guard_status.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            return {
                "name": "guard:master.cf",
                "ok": None,
                "source": "guard",
                "detail": f"status unreadable: {exc}"[:300],
            }
        if not isinstance(data, dict):
            return None
        checked = data.get("checked_at")
        fresh = isinstance(checked, (int, float)) and now - checked <= GUARD_STALE
        circuit = data.get("circuit")
        parts = [f"amavis: {circuit}"]
        if "diverted" in data:
            parts.append("dpkg-divert: " + ("oui" if data.get("diverted") else "non"))
        if data.get("last_repair"):
            parts.append(f"dernière réparation: {data['last_repair']}")
        if data.get("vendor_pending"):
            parts.append("nouveau master.cf livré par une mise à jour (à comparer)")
        return {
            "name": "guard:master.cf",
            "ok": (isinstance(circuit, int) and circuit >= 2) if fresh else None,
            "source": "guard",
            "detail": " · ".join(parts)[:300],
        }

    def collect(self, now: float | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        """``(services, errors)`` to merge into the services report."""
        now = time.time() if now is None else now
        self._scan(self._read_new(), now)
        services: list[dict[str, Any]] = []
        if self.error is None:
            for name, *_ in CHECKS:
                check = self._checks.get(name, _Check())
                down = check.down(now)
                services.append(
                    {
                        "name": name,
                        "ok": not down,
                        "source": "mta",
                        **({"detail": check.detail} if down and check.detail else {}),
                    }
                )
        guard = self._guard(now)
        if guard:
            services.append(guard)
        return services, ([self.error] if self.error else [])
