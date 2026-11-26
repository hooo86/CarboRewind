"""Logs readable from the interface: file tail and journal."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent import logtail
from carborewind_agent.logtail import LogError, journal, tail


def test_tail_reads_the_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "carbonio.log"
    log.write_text("".join(f"line {n}\n" for n in range(100_000)))
    assert tail(log, 3) == "line 99997\nline 99998\nline 99999"
    assert len(tail(log, 10_000).splitlines()) == logtail.MAX_LINES  # bounded
    with pytest.raises(LogError, match="does not exist"):
        tail(tmp_path / "missing.log", 5)


def test_tail_reports_unreadable_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "open", refuse)
    with pytest.raises(LogError, match="not readable"):
        tail(tmp_path / "mailbox.log", 5)


def test_journal_uses_fixed_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"2026-11-19T10:00:00 agent started\\n", b"")

    monkeypatch.setattr(logtail.subprocess, "run", run)
    assert "agent started" in journal("carborewind-agent", 99_999)
    assert seen[0][:3] == ["/usr/bin/journalctl", "--unit", "carborewind-agent"]
    assert seen[0][seen[0].index("--lines") + 1] == str(logtail.MAX_LINES)

    def fail(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 1, b"", b"No journal files were found.")

    monkeypatch.setattr(logtail.subprocess, "run", fail)
    with pytest.raises(LogError, match="No journal files"):
        journal("carborewind-agent", 10)
