"""Carbonio services watcher: admin API status, local ports, mail queue."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.servicewatch import ServiceMonitor, collect, port_open

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings
from .test_worker import FakeApi

NOW = 1_800_000_000


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    from carborewind_agent.carbonio.factory import build_client

    return build_client(_settings(tmp_path), transport=fake.transport())


def test_collect(client: CarbonioClient, fake: FakeCarbonio) -> None:
    fake.services = {
        "mta": (True, NOW - 60),
        "amavis": (False, NOW - 60),
        "stats": (True, NOW - 3600),  # stale record: unknown
    }
    fake.queues = {"deferred": 42, "active": 3}
    report = collect(client, {"amavis": 10024, "mta": 25}, now=NOW, probe=lambda p: p == 25)
    by = {s["name"]: s for s in report["services"]}
    assert by["mta"]["ok"] is True and by["amavis"]["ok"] is False and by["stats"]["ok"] is None
    assert by["port:amavis"] == {
        "name": "port:amavis",
        "ok": False,
        "source": "port",
        "port": 10024,
    }
    assert by["port:mta"]["ok"] is True
    assert report["queue"] == {"deferred": 42, "active": 3}
    assert report["errors"] == []


def test_collect_degrades_without_the_admin_api(client: CarbonioClient, fake: FakeCarbonio) -> None:
    fake.services = {"mta": (True, NOW)}
    fake.queues = None
    report = collect(client, {}, now=NOW)
    assert report["queue"] is None and "mail queue" in report["errors"][0]
    fake.fail_next = [503, 503, 503, 503]
    report = collect(client, {"ldap": 1}, now=NOW, probe=lambda p: False)
    assert [s["name"] for s in report["services"]] == ["port:ldap"]
    assert report["queue"] is None and report["errors"]


def test_port_open() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert port_open(port)
        assert port_open(port, "127.0.0.1")
    finally:
        server.close()
    assert not port_open(port, timeout=0.5)


def test_monitor_reports_and_survives_errors(
    tmp_path: Path, client: CarbonioClient, fake: FakeCarbonio
) -> None:
    fake.services = {"mta": (True, NOW)}
    api = FakeApi([])
    monitor = ServiceMonitor(AgentApi(_settings(tmp_path), http=api.client()), client, {})
    report: dict[str, Any] = monitor.tick()
    assert api.paths() == ["/services"] and api.calls[0][1] == report
    stop = threading.Event()
    calls: list[int] = []

    def boom() -> dict[str, Any]:
        calls.append(1)
        stop.set()
        raise RuntimeError("unexpected")

    monitor.tick = boom  # type: ignore[method-assign]
    monitor.run(stop)
    assert calls == [1]


def test_port_open_tries_the_server_name(monkeypatch: pytest.MonkeyPatch) -> None:
    import carborewind_agent.servicewatch as sw

    tried: list[str] = []

    def connect(address: tuple[str, int], timeout: float) -> socket.socket:
        tried.append(address[0])
        if address[0] != "mail.example.test":
            raise ConnectionRefusedError
        return socket.socket()

    monkeypatch.setattr(sw.socket, "getfqdn", lambda: "mail.example.test")
    monkeypatch.setattr(sw.socket, "gethostname", lambda: "mail")
    monkeypatch.setattr(sw.socket, "create_connection", connect)
    assert port_open(389)
    assert tried == ["127.0.0.1", "mail.example.test"]


# ------------------------------------------------------------------ Postfix health

DEFERRED = (
    "Nov 21 05:56:22 mail postfix/qmgr[341568]: warning: connect to transport "
    "private/smtp-amavis: Connection refused\n"
    "Nov 21 05:56:22 mail postfix/error[343515]: 6AE741413C3: to=<zextras@lnhc.dz>, "
    "relay=none, delay=6381, dsn=4.3.0, status=deferred (mail transport unavailable)\n"
)
TO_AMAVIS = (
    "Nov 21 06:07:28 mail postfix/smtp[361070]: 3F64F141722: to=<a@lnhc.dz>, "
    "relay=127.0.0.1[127.0.0.1]:10024, delay=0.4, dsn=2.0.0, status=sent "
    "(250 2.0.0 from MTA(smtp:[127.0.0.1]:10025): 250 2.0.0 Ok: queued as 1A2B)\n"
)
MILTER = (
    "Nov 21 05:52:55 mail postfix/smtpd[319638]: warning: connect to Milter service "
    "inet:127.0.0.1:7026: Connection refused\n"
)


def _mta(tmp_path: Path, text: str = "") -> tuple[Any, Path]:
    from carborewind_agent.mtahealth import MtaHealth

    log = tmp_path / "carbonio.log"
    log.write_text(text)
    return MtaHealth([tmp_path / "missing.log", log], tmp_path / "guard.json"), log


def _by(services: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in services}


def test_mta_health_detects_the_broken_antivirus_circuit(tmp_path: Path) -> None:
    mta, log = _mta(tmp_path, DEFERRED + MILTER)
    services, errors = mta.collect(NOW)
    by = _by(services)
    assert errors == []
    assert by["mta:antivirus"]["ok"] is False and by["mta:antivirus"]["source"] == "mta"
    assert "mail transport unavailable" in by["mta:antivirus"]["detail"]
    assert by["mta:milter"]["ok"] is False and by["mta:policy"]["ok"] is True
    # nothing new: still down within the window
    assert _by(mta.collect(NOW + 60)[0])["mta:antivirus"]["ok"] is False
    # a message handed to Amavis again: back up at once
    with log.open("a") as fh:
        fh.write(TO_AMAVIS)
    by = _by(mta.collect(NOW + 120)[0])
    assert by["mta:antivirus"]["ok"] is True and "detail" not in by["mta:antivirus"]
    # the milter error fades out after the window without new failures
    assert _by(mta.collect(NOW + 60 + 11 * 60)[0])["mta:milter"]["ok"] is True


def test_mta_health_follows_rotation_and_reads_only_new_lines(tmp_path: Path) -> None:
    mta, log = _mta(tmp_path, DEFERRED)
    assert _by(mta.collect(NOW)[0])["mta:antivirus"]["ok"] is False
    log.unlink()
    log.write_text(TO_AMAVIS)  # rotated: new file read from its start
    assert _by(mta.collect(NOW + 60)[0])["mta:antivirus"]["ok"] is True


def test_mta_health_reports_an_unreadable_log(tmp_path: Path) -> None:
    from carborewind_agent.mtahealth import MtaHealth

    services, errors = MtaHealth([tmp_path / "none.log"], tmp_path / "g.json").collect(NOW)
    assert services == [] and errors == ["MTA log not found"]


def test_mta_health_reads_the_guard_status(tmp_path: Path) -> None:
    import json

    mta, _ = _mta(tmp_path)
    guard = tmp_path / "guard.json"
    guard.write_text(
        json.dumps(
            {
                "checked_at": NOW - 120,
                "circuit": 3,
                "diverted": True,
                "last_repair": "2026-11-21 05:17",
                "vendor_pending": True,
            }
        )
    )
    g = _by(mta.collect(NOW)[0])["guard:master.cf"]
    assert g["ok"] is True and g["source"] == "guard"
    assert "dpkg-divert: oui" in g["detail"] and "à comparer" in g["detail"]
    guard.write_text(json.dumps({"checked_at": NOW - 60, "circuit": 0}))
    assert _by(mta.collect(NOW)[0])["guard:master.cf"]["ok"] is False
    guard.write_text(json.dumps({"checked_at": NOW - 3600, "circuit": 0}))  # stale
    assert _by(mta.collect(NOW)[0])["guard:master.cf"]["ok"] is None


def test_collect_merges_the_postfix_checks(
    tmp_path: Path, client: CarbonioClient, fake: FakeCarbonio
) -> None:
    fake.services = {"mta": (True, NOW)}
    mta, _ = _mta(tmp_path, DEFERRED)
    report = collect(client, {}, now=NOW, mta=mta)
    by = _by(report["services"])
    assert by["mta"]["ok"] is True and by["mta:antivirus"]["ok"] is False


def test_mta_health_first_read_starts_on_a_whole_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import carborewind_agent.mtahealth as mh

    monkeypatch.setattr(mh, "FIRST_READ", len(TO_AMAVIS) + 10)
    mta, log = _mta(tmp_path, "x" * 500 + "\n" + DEFERRED + TO_AMAVIS)
    by = _by(mta.collect(NOW)[0])
    assert by["mta:antivirus"]["ok"] is True  # only the recent past, cut on a line break
    with log.open("a") as fh:
        fh.write(DEFERRED)
    assert _by(mta.collect(NOW + 60)[0])["mta:antivirus"]["ok"] is False
