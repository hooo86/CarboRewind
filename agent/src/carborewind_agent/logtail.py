"""Last lines of the logs an administrator may read from the interface.

Only fixed sources are offered (the interface names one, never a path or a command): the
agent's own journal and Carbonio's MTA and mailbox logs. Files are read from the end, at
most :data:`MAX_BYTES`; the journal is read with ``journalctl`` called with fixed arguments.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

MAX_LINES = 2000
MAX_BYTES = 2 * 2**20
BLOCK = 64 * 1024


class LogError(Exception):
    pass


def tail(path: Path, lines: int) -> str:
    """The last ``lines`` lines of a text file."""
    lines = max(1, min(MAX_LINES, lines))
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            data = b""
            pos = end
            while pos > 0 and data.count(b"\n") <= lines and len(data) < MAX_BYTES:
                step = min(BLOCK, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
    except FileNotFoundError as exc:
        raise LogError(f"{path} does not exist on this server") from exc
    except PermissionError as exc:
        raise LogError(
            f"{path} is not readable by the agent (add the carborewind user to its group)"
        ) from exc
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def journal(unit: str, lines: int) -> str:
    """The last ``lines`` lines of a systemd unit's journal (fixed arguments, no shell)."""
    lines = max(1, min(MAX_LINES, lines))
    try:
        done = subprocess.run(  # noqa: S603 - fixed program and arguments
            [
                "/usr/bin/journalctl",
                "--unit",
                unit,
                "--lines",
                str(lines),
                "--no-pager",
                "--output",
                "short-iso",
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LogError(f"journalctl failed: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise LogError(done.stderr.decode("utf-8", "replace").strip()[:300] or "journalctl failed")
    return done.stdout.decode("utf-8", "replace")[-MAX_BYTES:]
