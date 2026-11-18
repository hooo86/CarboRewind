"""Agent worker loop and API client (API simulated with httpx.MockTransport)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from carborewind_agent.__main__ import main
from carborewind_agent.api_client import AgentApi, ApiError, enroll
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.config import AgentSettings
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.worker import Worker

from .fake_carbonio import FakeCarbonio, Mailbox
from .test_carbonio_client import _settings


class FakeApi:
    """Records the protocol calls; serves a scripted list of jobs."""

    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self.jobs = jobs
        self.calls: list[tuple[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/agent")
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if path == "/jobs/claim":
            return httpx.Response(200, json=self.jobs.pop(0)) if self.jobs else httpx.Response(204)
        if path == "/inventory":
            return httpx.Response(200, json={"accounts_created": len(body["accounts"])})
        if path == "/enroll":
            if body["token"] != "good-token-0123456789":
                return httpx.Response(401, text="invalid token")
            return httpx.Response(
                200,
                json={
                    "agent_id": "a-1",
                    "certificate_pem": "CERT",
                    "ca_pem": "CA",
                    "not_after": "2027-01-01T00:00:00Z",
                },
            )
        return httpx.Response(204)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://api.test", transport=httpx.MockTransport(self.handler)
        )

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def alice(fake: FakeCarbonio) -> Mailbox:
    box = fake.add_mailbox("alice@example.test")
    box.add("m", 2, b"Subject: one\r\n\r\nx")
    box.add("m", 2, b"Subject: two\r\n\r\ny")
    return box


@pytest.fixture
def settings(tmp_path: Path) -> AgentSettings:
    s = _settings(tmp_path)
    s.tls_cert, s.tls_key, s.tls_ca = tmp_path / "a.crt", tmp_path / "a.key", tmp_path / "ca.crt"
    return s


@pytest.fixture
def carbonio(settings: AgentSettings, fake: FakeCarbonio) -> CarbonioClient:
    return build_client(settings, transport=fake.transport())


@pytest.fixture
def engine(carbonio: CarbonioClient, tmp_path: Path) -> AccountBackup:
    return AccountBackup(carbonio, Repository.init(tmp_path / "repo", generate_key()))


def _job(box: Mailbox, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": "j-1",
        "kind": "backup",
        "attempt": 1,
        "lease_seconds": 30,
        "params": {},
        "checkpoint": None,
        "carbonio_id": box.id,
        "account_email": box.name,
        "snapshot_id": "snap-1",
        "seq": 1,
        "previous": None,
        **extra,
    }


def _worker(
    api: FakeApi, settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup
) -> Worker:
    return Worker(
        AgentApi(settings, http=api.client()),
        carbonio,
        engine,
        sleep=lambda _: True,
        inventory_every=1,
    )


def test_successful_job_is_completed(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, alice: Mailbox
) -> None:
    api = FakeApi([_job(alice)])
    worker = _worker(api, settings, carbonio, engine)
    assert worker.run_once() is True
    path, payload = api.calls[-1]
    assert path == "/jobs/j-1/complete"
    assert payload["item_count"] == 2
    assert payload["backup_type"] == "full"
    assert set(payload["added"]) == {str(i) for i in alice.items}
    assert all("meta_size" in v and "mod" not in v for v in payload["added"].values())
    assert worker.run_once() is False  # queue empty


def test_carbonio_fault_fails_job_retryable_or_not(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, alice: Mailbox
) -> None:
    api = FakeApi([_job(alice, carbonio_id="unknown")])
    _worker(api, settings, carbonio, engine).run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/j-1/fail"
    assert body["retryable"] is False  # NO_SUCH_ACCOUNT will not fix itself
    assert "/jobs/j-1/heartbeat" in api.paths()  # progress saved before failing


def test_crash_fails_job_as_retryable(
    settings: AgentSettings,
    carbonio: CarbonioClient,
    engine: AccountBackup,
    alice: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        engine, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    )
    api = FakeApi([_job(alice)])
    _worker(api, settings, carbonio, engine).run_once()
    assert api.calls[-1] == (
        "/jobs/j-1/fail",
        {"error": "RuntimeError('disk full')", "retryable": True},
    )


def test_unsupported_kind(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, alice: Mailbox
) -> None:
    api = FakeApi([_job(alice, kind="purge")])
    _worker(api, settings, carbonio, engine).run_once()
    assert api.calls[-1][0] == "/jobs/j-1/fail"
    assert api.calls[-1][1]["retryable"] is False


def test_resume_uses_checkpoint_and_previous(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, alice: Mailbox
) -> None:
    first = engine.run(alice.id, alice.name, "snap-1", 1)
    alice.add("m", 2, b"Subject: three\r\n\r\nz")
    api = FakeApi(
        [
            _job(
                alice,
                snapshot_id="snap-2",
                seq=2,
                previous={"snapshot_id": "snap-1", "token": first.token},
            )
        ]
    )
    _worker(api, settings, carbonio, engine).run_once()
    payload = api.calls[-1][1]
    assert payload["backup_type"] == "incremental"
    assert len(payload["added"]) == 1


def test_run_forever_syncs_inventory_and_stops(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, alice: Mailbox
) -> None:
    api = FakeApi([])
    _worker(api, settings, carbonio, engine).run_forever()  # sleep() says "stop" at once
    inventory = next(body for path, body in api.calls if path == "/inventory")
    assert inventory["complete"] is True
    assert inventory["accounts"][0]["email"] == alice.name


def test_run_forever_survives_api_errors(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(down))
    stops = iter([False, True])
    Worker(
        AgentApi(_settings_dummy(), http=http), carbonio, engine, sleep=lambda _: next(stops)
    ).run_forever()


def _settings_dummy() -> AgentSettings:
    return AgentSettings(api_url="https://api.test")  # type: ignore[arg-type]


def test_api_error_carries_status() -> None:
    api = FakeApi([])
    client = AgentApi(
        _settings_dummy(),
        http=httpx.Client(
            base_url="https://api.test",
            transport=httpx.MockTransport(lambda r: httpx.Response(409, text="busy")),
        ),
    )
    with pytest.raises(ApiError) as exc:
        client.heartbeat("j", None)
    assert exc.value.status == 409
    del api


def test_enroll_writes_private_files(settings: AgentSettings) -> None:
    api = FakeApi([])
    assert enroll(settings, "good-token-0123456789", http=api.client()) == "a-1"
    for path in (settings.tls_key, settings.tls_cert, settings.tls_ca):
        assert path.stat().st_mode & 0o077 == 0
    assert b"PRIVATE KEY" in settings.tls_key.read_bytes()
    csr = api.calls[0][1]["csr_pem"]
    assert "CERTIFICATE REQUEST" in csr and "PRIVATE" not in csr  # the key never leaves the host
    with pytest.raises(ApiError):
        enroll(settings, "bad-token-0123456789", http=api.client())


def test_cli_enroll_reports_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARBOREWIND_AGENT_API_URL", "https://127.0.0.1:9")
    token = tmp_path / "token"
    token.write_text("some-token-0123456789\n")
    os.chmod(token, 0o600)
    assert main(["enroll", "--token-file", str(token)]) == 1
    assert "error" in capsys.readouterr().err
