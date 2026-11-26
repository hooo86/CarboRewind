"""Start Carbonio as asked by the agent (runs as root, ``carborewind-control.service``).

The unprivileged agent writes ``/var/lib/carborewind/control/request.json`` when Carbonio
has stayed down (see :mod:`.autostart`); the ``carborewind-control.path`` unit then runs
``carborewind-agent control apply`` as root, which:

1. reads the request defensively (regular file of the agent's user, small) and deletes it;
2. accepts only ``{"action": "start"}``: nothing from the request reaches a command line,
   the one command run is fixed (``su - zextras -c "zmcontrol start"``);
3. refuses while apt / dpkg is running (an upgrade stops Carbonio on purpose);
4. writes ``status.json`` (root:carborewind 0640): outcome and the end of the output.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .autostart import CONTROL_DIR, package_manager_running
from .storage import AGENT_USER, StorageError
from .storage_apply import read_request

START = ["/usr/bin/su", "-", "zextras", "-c", "zmcontrol start"]
TIMEOUT = 15 * 60
OUTPUT_TAIL = 3000
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _write_status(path: Path, status: dict[str, Any], gid: int) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def apply(control_dir: Path = CONTROL_DIR, run: Any = subprocess.run) -> dict[str, Any]:
    user = pwd.getpwnam(AGENT_USER)
    now = datetime.now(UTC).isoformat()
    status: dict[str, Any] = {"at": now, "ok": False}
    try:
        request = read_request(control_dir / "request.json", user.pw_uid)
    except StorageError as exc:
        status["error"] = str(exc)
        _write_status(control_dir / "status.json", status, user.pw_gid)
        return status
    request_id = request.get("id")
    status["id"] = (
        request_id if isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id) else None
    )
    if request.get("action") != "start":
        status["error"] = "unknown action"
    elif tool := package_manager_running():
        status["error"] = f"packages being installed ({tool}): not started"
    else:
        try:
            done = run(START, capture_output=True, text=True, timeout=TIMEOUT, check=False)
            status["returncode"] = done.returncode
            status["ok"] = done.returncode == 0
            status["output"] = ((done.stdout or "") + (done.stderr or ""))[-OUTPUT_TAIL:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            status["error"] = f"zmcontrol start: {exc}"[:500]
    _write_status(control_dir / "status.json", status, user.pw_gid)
    return status


def apply_command() -> int:
    if os.geteuid() != 0:
        print("control apply must run as root (carborewind-control.service)")
        return 2
    status = apply()
    print(json.dumps({k: v for k, v in status.items() if k != "output"}))
    return 0 if status.get("ok") else 1
