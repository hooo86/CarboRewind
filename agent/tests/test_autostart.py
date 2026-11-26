"""Automatic start of Carbonio, its root helper, and the sending test."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest

from carborewind_agent import control_apply
from carborewind_agent.autostart import AutoStart, core_down, package_manager_running
from carborewind_agent.mailtest import MailTest

NOW = 1_800_000_000.0
DOWN = [
    {"name": "port:mta", "ok": False, "source": "port", "port": 25},
    {"name": "port:lmtp", "ok": False, "source": "port", "port": 7025},
    {"name": "port:submission", "ok": False, "source": "port", "port": 587},
]
UP = [{"name": "port:mta", "ok": True, "source": "port", "port": 25}]


def _auto(tmp_path: Path, busy: str | None = None, **cfg: Any) -> AutoStart:
    auto = AutoStart(control_dir=tmp_path / "control", busy=lambda: busy)
    auto.configure({"autostart_enabled": True, "autostart_minutes": 5, **cfg})
    return auto


def test_core_down_ignores_what_mail_does_not_need() -> None:
    services = [
        *DOWN,
        {"name": "stats", "ok": False, "source": "carbonio"},
        {"name": "mailbox@mx2", "ok": False, "source": "carbonio"},
        {"name": "mta", "ok": None, "source": "carbonio"},
    ]
    assert core_down(services) == ["port:mta", "port:lmtp", "mailbox@mx2"]


def test_start_is_asked_after_the_delay_once(tmp_path: Path) -> None:
    auto = _auto(tmp_path)
    assert auto.evaluate(DOWN, NOW) is False and auto.note == "waiting"
    assert not (tmp_path / "control" / "request.json").exists()
    auto.evaluate(DOWN, NOW + 4 * 60)
    assert not (tmp_path / "control" / "request.json").exists()
    auto.evaluate(DOWN, NOW + 5 * 60)
    request = json.loads((tmp_path / "control" / "request.json").read_text())
    assert request["action"] == "start" and len(request["id"]) == 32
    assert auto.note == "start asked" and auto.last_request["down"] == ["port:mta", "port:lmtp"]
    auto.evaluate(DOWN, NOW + 6 * 60)
    assert auto.note == "started recently" and len(auto.requests) == 1
    # root answered
    (tmp_path / "control" / "status.json").write_text(
        json.dumps({"id": request["id"], "ok": True, "returncode": 0, "output": "Done."})
    )
    assert auto.evaluate(UP, NOW + 7 * 60) is True  # back: time for the sending test
    assert auto.last_result["ok"] is True and auto.report()["down_since"] is None
    assert auto.evaluate(UP, NOW + 8 * 60) is False  # only once


def test_start_is_not_asked_when_off_paused_or_upgrading(tmp_path: Path) -> None:
    off = AutoStart(control_dir=tmp_path / "c1", busy=lambda: None)
    off.evaluate(DOWN, NOW)
    off.evaluate(DOWN, NOW + 3600)
    assert off.note == "disabled" and not (tmp_path / "c1").exists()

    paused = _auto(tmp_path, autostart_paused_until="2027-01-15T10:00:00+00:00")
    paused.evaluate(DOWN, NOW)
    paused.evaluate(DOWN, NOW + 600)
    assert paused.note == "paused"

    upgrading = _auto(tmp_path, busy="dpkg")
    upgrading.evaluate(DOWN, NOW)
    upgrading.evaluate(DOWN, NOW + 600)
    assert upgrading.note == "packages being installed (dpkg)"
    assert not (tmp_path / "control").exists()


def test_starts_are_capped(tmp_path: Path) -> None:
    auto = _auto(tmp_path, autostart_minutes=1)
    t = NOW
    auto.evaluate(DOWN, t)
    for _ in range(4):
        t += 16 * 60
        auto.evaluate(DOWN, t)
    assert len(auto.requests) == 3 and auto.note == "too many starts: intervention needed"


def test_package_manager_running(tmp_path: Path) -> None:
    for pid, name in (("1", "systemd"), ("42", "dpkg"), ("self", "x")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(name + "\n")
    assert package_manager_running(tmp_path) == "dpkg"
    assert package_manager_running(tmp_path / "none") is None


# ------------------------------------------------------------------ root helper


@pytest.fixture
def control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import pwd

    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(control_apply.pwd, "getpwnam", lambda _name: me)
    monkeypatch.setattr(control_apply.os, "chown", lambda *_a: None)
    monkeypatch.setattr(control_apply, "package_manager_running", lambda: None)
    path = tmp_path / "control"
    path.mkdir()
    return path


def _ask(path: Path, request: dict[str, Any]) -> None:
    (path / "request.json").write_text(json.dumps(request))


def test_control_runs_only_the_fixed_start(control: Path) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "Starting mta...Done.\n", "")

    _ask(control, {"id": "a" * 32, "action": "start", "cmd": "rm -rf /"})
    status = control_apply.apply(control, run=run)
    assert calls == [["/usr/bin/su", "-", "zextras", "-c", "zmcontrol start"]]
    assert status["ok"] is True and status["id"] == "a" * 32
    assert not (control / "request.json").exists()
    saved = json.loads((control / "status.json").read_text())
    assert saved["output"].startswith("Starting mta")


def test_control_refuses_other_actions_and_upgrades(
    control: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not run")

    _ask(control, {"id": "../etc", "action": "stop"})
    status = control_apply.apply(control, run=run)
    assert status == {**status, "ok": False, "error": "unknown action", "id": None}
    monkeypatch.setattr(control_apply, "package_manager_running", lambda: "apt")
    _ask(control, {"id": "b" * 32, "action": "start"})
    assert "packages being installed" in control_apply.apply(control, run=run)["error"]
    (control / "request.json").symlink_to("/etc/passwd")
    assert "cannot open" in control_apply.apply(control, run=run)["error"]


# ------------------------------------------------------------------ sending test


class FakeSMTP:
    sent: ClassVar[list[Any]] = []
    refuse: ClassVar[dict[str, tuple[int, bytes]]] = {}

    def __init__(self, host: str, port: int, timeout: float) -> None:
        assert (host, port) == ("127.0.0.1", 25)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> dict[str, Any]:
        FakeSMTP.sent.append((msg, from_addr, to_addrs))
        return FakeSMTP.refuse


def test_mail_test_follows_both_deliveries() -> None:
    test = MailTest(
        "carborewind@lnhc.dz", {"internal": "h.badi@lnhc.dz", "external": "x@gmail.com"}
    )
    test.started = NOW
    test.send(smtp=FakeSMTP)
    msg, sender, to = FakeSMTP.sent[-1]
    assert sender == "carborewind@lnhc.dz" and to == ["h.badi@lnhc.dz", "x@gmail.com"]
    mid = test.message_id
    assert msg["Message-ID"] == f"<{mid}>" and test.done is False
    lines = [
        f"Nov 22 10:00:01 mail postfix/cleanup[1]: AAAA11111: message-id=<{mid}>",
        "Nov 22 10:00:01 mail postfix/cleanup[1]: OTHER1111: message-id=<other@x>",
        "Nov 22 10:00:02 mail postfix/smtp[2]: AAAA11111: to=<h.badi@lnhc.dz>, "
        "relay=127.0.0.1[127.0.0.1]:10024, delay=0.3, dsn=2.0.0, status=sent (250 queued)",
        f"Nov 22 10:00:03 mail postfix/cleanup[1]: BBBB22222: message-id=<{mid}>",
        "Nov 22 10:00:03 mail postfix/lmtp[3]: BBBB22222: to=<h.badi@lnhc.dz>, "
        "relay=mail.lnhc.dz[192.168.8.40]:7025, delay=0.2, dsn=2.1.5, status=sent (250 OK)",
        "Nov 22 10:00:04 mail postfix/smtp[4]: BBBB22222: to=<x@gmail.com>, "
        "relay=gmail-smtp-in.l.google.com[142.250.1.27]:25, delay=1, dsn=4.7.0, "
        "status=deferred (host said: 421 try again later)",
        "Nov 22 10:00:05 mail postfix/smtp[4]: OTHER1111: to=<x@gmail.com>, "
        "relay=gmail[1.2.3.4]:25, delay=1, dsn=2.0.0, status=sent (250 ok)",
    ]
    test.feed(lines, NOW + 10)
    assert test.results["internal"]["status"] == "sent"
    assert test.results["internal"]["relay"].endswith(":7025")
    assert test.results["external"]["status"] == "deferred" and not test.done
    test.feed(
        [
            "Nov 22 10:05:00 mail postfix/smtp[5]: BBBB22222: to=<x@gmail.com>, "
            "relay=gmail-smtp-in.l.google.com[142.250.1.27]:25, delay=300, dsn=2.0.0, "
            "status=sent (250 2.0.0 OK)"
        ],
        NOW + 300,
    )
    report = test.report()
    assert report["done"] is True and report["ok"] is True


def test_mail_test_errors_and_timeout() -> None:
    bad = MailTest("carborewind@lnhc.dz", {"internal": "not an address"})
    bad.send(smtp=FakeSMTP)
    assert bad.error == "no valid recipient" and bad.done and bad.report()["ok"] is False

    class Down(FakeSMTP):
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            raise ConnectionRefusedError("refused")

    down = MailTest("carborewind@lnhc.dz", {"internal": "a@lnhc.dz"})
    down.send(smtp=Down)
    assert down.results["internal"]["status"] == "error" and "refused" in down.error

    slow = MailTest("carborewind@lnhc.dz", {"external": "x@gmail.com"})
    slow.started = NOW
    slow.send(smtp=FakeSMTP)
    slow.feed([], NOW + 11 * 60)
    assert slow.results["external"]["status"] == "timeout" and slow.done and not slow.ok


def test_monitor_starts_the_test_when_carbonio_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carborewind_agent.servicewatch import ServiceMonitor

    sent: list[str] = []

    def fake_send(self: MailTest) -> None:
        sent.append(self.reason)
        self.results["internal"] = {"address": "a@lnhc.dz", "status": "pending"}

    monkeypatch.setattr(MailTest, "send", fake_send)
    monitor = ServiceMonitor(None, None, {}, sender="carborewind@lnhc.dz")  # type: ignore[arg-type]
    monitor.autostart = _auto(tmp_path)
    monitor.configure(
        True,
        {},
        {"autostart_enabled": True, "mail_test_internal": "a@lnhc.dz", "mail_test_external": ""},
    )
    assert monitor.recipients == {"internal": "a@lnhc.dz"}
    monitor.autostart.waiting_back = True
    report = monitor._extend({"services": UP})
    assert sent == ["after_start"] and report["mail_test"]["reason"] == "after_start"
    assert monitor.start_test("manual") is None  # one at a time
    monitor.recipients = {}
    monitor.test = None
    assert monitor.start_test("manual").error.startswith("no test address")
