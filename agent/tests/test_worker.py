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
        self.config: dict[str, Any] | None = None
        self.queue_actions: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/agent")
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if path == "/config":
            return httpx.Response(200, json=self.config) if self.config else httpx.Response(204)
        if path == "/services" and self.queue_actions:
            actions, self.queue_actions = self.queue_actions, []
            return httpx.Response(200, json={"queue_actions": actions})
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


def test_inventory_carries_carbonio_attributes(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, fake: FakeCarbonio
) -> None:
    box = fake.add_mailbox(
        "zoe@example.test",
        displayName="Zoé Martin",
        givenName="Zoé",
        sn="Martin",
        zimbraCOSId="cos-vip",
        zimbraMailQuota="1073741824",
        zimbraIsAdminAccount="TRUE",
        zimbraLastLogonTimestamp="20261118093000Z",
        zimbraCreateTimestamp="20250102030405.123Z",
    )
    box.attrs["zimbraMailAlias"] = "z@example.test"
    box.add("m", 2, b"Subject: x\r\n\r\n12345")
    other = fake.add_mailbox("bad@example.test", zimbraMailQuota="unlimited", zimbraCOSId="gone")
    api = FakeApi([])
    worker = _worker(api, settings, carbonio, engine)
    worker.sync_inventory()
    accounts = {a["email"]: a for a in api.calls[-1][1]["accounts"]}
    zoe = accounts["zoe@example.test"]
    assert zoe == {
        "carbonio_id": box.id,
        "email": "zoe@example.test",
        "status": "active",
        "display_name": "Zoé Martin",
        "first_name": "Zoé",
        "last_name": "Martin",
        "aliases": ["z@example.test"],
        "cos": "vip",
        "quota_bytes": 1073741824,
        "is_admin": True,
        "last_login_at": "2026-11-18T09:30:00+00:00",
        "carbonio_created_at": "2025-01-02T03:04:05+00:00",
        "used_bytes": len(b"Subject: x\r\n\r\n12345"),
        "profile": {},
        # never changed: since the creation; the vip COS has no maximum age
        "password_changed_at": "2025-01-02T03:04:05+00:00",
        "password_max_age_days": 0,
        "password_must_change": False,
    }
    bad = accounts[other.name]
    assert (bad["quota_bytes"], bad["cos"], bad["last_login_at"]) == (None, "gone", None)
    assert bad["password_max_age_days"] == 90  # unknown COS: the default one
    # Quota usage refused (restricted admin): each mailbox is measured instead.
    fake.quota_denied = True
    worker.sync_inventory()
    sizes = {a["email"]: a.get("used_bytes") for a in api.calls[-1][1]["accounts"]}
    assert sizes["zoe@example.test"] == len(b"Subject: x\r\n\r\n12345")
    assert "GetMailboxRequest" in fake.calls


def test_inventory_quota_from_cos_usage_fallback_and_profile(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup, fake: FakeCarbonio
) -> None:
    vip = fake.add_mailbox(
        "vip@example.test",
        zimbraCOSId="cos-vip",
        title="Directrice",
        company="LNHC",
        ou="Laboratoire",
        telephoneNumber="+213 21 00 00 00",
        mobile="+213 555 00 00 00",
        l="Alger",
        co="Algérie",
        zimbraNotes="Accès VIP",
    )
    vip.add("m", 2, b"Subject: y\r\n\r\n" + b"x" * 100)
    plain = fake.add_mailbox("plain@example.test", zimbraCOSId="cos-default")
    hidden = fake.add_mailbox("hidden@example.test")
    hidden.add("m", 2, b"Subject: z\r\n\r\nzz")
    fake.quota_missing.add(hidden.id)  # GetQuotaUsage forgets it (other server, cache)
    api = FakeApi([])
    _worker(api, settings, carbonio, engine).sync_inventory()
    accounts = {a["email"]: a for a in api.calls[-1][1]["accounts"]}
    assert accounts["vip@example.test"]["quota_bytes"] == 5 * 1024**3  # from its COS
    assert accounts["vip@example.test"]["profile"] == {
        "title": "Directrice",
        "company": "LNHC",
        "department": "Laboratoire",
        "phone": "+213 21 00 00 00",
        "mobile": "+213 555 00 00 00",
        "city": "Alger",
        "country": "Algérie",
        "notes": "Accès VIP",
    }
    assert accounts[plain.name]["quota_bytes"] == 0  # COS: unlimited
    assert accounts[hidden.name]["used_bytes"] == len(b"Subject: z\r\n\r\nzz")
    assert accounts[hidden.name]["quota_bytes"] is None


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


def test_tls_context_loads_client_certificate(tmp_path: Path) -> None:
    """Regression: the client certificate must be part of the TLS context itself."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from carborewind_agent.api_client import tls_context

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")])
    from datetime import UTC, datetime, timedelta

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    crt, pem_key = tmp_path / "a.crt", tmp_path / "a.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = tls_context(crt, (crt, pem_key))  # self-signed cert doubles as the test CA
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    assert ctx.check_hostname
    assert ctx.cert_store_stats()["x509"] == 1  # the given CA file, not the system store
    with pytest.raises(FileNotFoundError):
        tls_context(None, (tmp_path / "missing.crt", pem_key))


def test_quota_report(carbonio: CarbonioClient, fake: FakeCarbonio) -> None:
    from carborewind_agent.carbonio.probe import quota_report

    box = fake.add_mailbox("q@example.test", zimbraCOSId="cos-vip")
    box.add("m", 2, b"Subject: q\r\n\r\nq")
    text = "\n".join(quota_report(carbonio, "Q@example.test"))
    assert "class of service: vip" in text and "GetMailboxRequest size: 15 bytes" in text
    assert "(used, limit) = (15, 0)" in text
    fake.quota_denied = True
    assert "GetQuotaUsageRequest refused" in "\n".join(quota_report(carbonio, box.name))
    assert quota_report(carbonio, "nobody@example.test") == [
        "nobody@example.test: not found by SearchDirectoryRequest"
    ]


def test_settings_from_the_interface_are_applied(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup
) -> None:
    api = FakeApi([])
    seen: list[dict[str, Any]] = []
    worker = Worker(AgentApi(settings, http=api.client()), carbonio, engine, on_config=seen.append)
    worker.sync_config()  # nothing saved yet: agent.env stays
    assert worker.concurrency == 1 and seen == []
    api.config = {
        "revision": 3,
        "max_concurrent_accounts": 3,
        "min_free_gb_local": 5,
        "min_free_percent_local": 10,
        "min_free_gb_network": 1,
        "max_bandwidth_mbps": 100,
        "max_load_per_cpu": 2,
    }
    worker.sync_config()
    assert worker.concurrency == 3 and seen[0]["max_bandwidth_mbps"] == 100
    worker.sync_config()  # same revision: not applied twice
    assert len(seen) == 1
    api.config = {**api.config, "revision": 4, "max_concurrent_accounts": 99}
    worker.sync_config()
    assert worker.concurrency == 8  # bounded


def test_jobs_run_in_parallel_slots(
    settings: AgentSettings, carbonio: CarbonioClient, fake: FakeCarbonio, tmp_path: Path
) -> None:
    boxes = [fake.add_mailbox(f"u{n}@example.test") for n in range(3)]
    for box in boxes:
        box.add("m", 2, b"Subject: x\r\n\r\nbody")
    jobs = [
        {**_job(box), "job_id": f"j-{n}", "snapshot_id": f"s-{n}"} for n, box in enumerate(boxes)
    ]
    api = FakeApi(jobs)
    api.config = {"revision": 1, "max_concurrent_accounts": 2}
    engine = AccountBackup(carbonio, Repository.init(tmp_path / "repo2", generate_key()))
    import time

    def until_done(_: float) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if sum(1 for p, _ in api.calls if p.endswith("/complete")) == 3:
                return True
            time.sleep(0.05)
        return True

    worker = Worker(
        AgentApi(settings, http=api.client()),
        carbonio,
        engine,
        sleep=until_done,
        poll_seconds=0.05,
    )
    worker.run_forever()
    assert worker.concurrency == 2
    done = sorted(p for p, _ in api.calls if p.endswith("/complete"))
    assert done == ["/jobs/j-0/complete", "/jobs/j-1/complete", "/jobs/j-2/complete"]


def test_stopping_hands_the_running_jobs_back(
    settings: AgentSettings, carbonio: CarbonioClient, engine: AccountBackup
) -> None:
    api = FakeApi([])
    worker = Worker(AgentApi(settings, http=api.client()), carbonio, engine)
    worker._running["j-1"] = {"checkpoint": {"done": ["m1"]}}  # a job in progress
    worker.release_running()
    assert api.calls == [
        ("/jobs/j-1/heartbeat", {"checkpoint": {"done": ["m1"]}, "progress": None}),
        ("/jobs/j-1/release", None),
    ]


def test_api_retries_while_the_server_restarts() -> None:
    answers = [503, 502, 200]
    seen: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        code = answers.pop(0)
        seen.append(code)
        return httpx.Response(code, json={})

    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(flaky))
    api = AgentApi(_settings_dummy(), http=http)
    assert api._post("/x").status_code == 200  # waited through 503 and 502
    assert seen == [503, 502, 200]
    answers[:] = [503, 503, 503]
    with pytest.raises(ApiError):  # tests allow two retries: gives up on the third 503
        api._post("/x")
    answers[:] = [503]
    with pytest.raises(ApiError) as err:
        api._post("/x", retry=False)
    assert err.value.status == 503

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    down = AgentApi(
        _settings_dummy(),
        http=httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(refused)),
    )
    with pytest.raises(ApiError) as err:
        down._post("/x")
    assert err.value.status == 0 and "unreachable" in str(err.value)
