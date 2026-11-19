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
