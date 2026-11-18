"""Agent enrolment, mTLS-by-proxy authentication, inventory and the job protocol."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from carborewind_api.config import Settings
from carborewind_api.db.models import (
    Account,
    AccountStatus,
    AuditEvent,
    AuthSource,
    Domain,
    ItemVersion,
    Job,
    Role,
    RoleAssignment,
    Snapshot,
    SnapshotStatus,
    User,
)
from carborewind_api.main import create_app
from carborewind_api.pki import init_ca
from carborewind_api.security.passwords import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "a-long-enough-password"


def _csr(key: Any = None) -> str:
    key = key or ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ignored")]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def settings(migrated: str, tmp_path: Path) -> Settings:
    init_ca(tmp_path / "pki")
    return Settings(  # type: ignore[call-arg]
        environment="test",
        database_url=migrated,
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["testclient"],
        job_lease_seconds=60,
    )


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def admin(api: TestClient, engine: Engine) -> dict[str, str]:
    username = f"root{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=username,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=Role.SUPER_ADMIN)],
            )
        )
    r = api.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


class AgentHandle:
    def __init__(self, api: TestClient, agent_id: str, cert_pem: str) -> None:
        self.api = api
        self.id = agent_id
        self.headers = {"X-Client-Verify": "SUCCESS", "X-Client-Cert": quote(cert_pem)}

    def post(self, path: str, json: Any = None) -> Any:
        return self.api.post(f"/api/agent{path}", json=json, headers=self.headers)


def _register(api: TestClient, admin: dict[str, str]) -> dict[str, Any]:
    r = api.post(
        "/api/agents",
        json={"name": f"mx-{uuid.uuid4().hex[:6]}", "hostname": "mail.example.test"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def agent(api: TestClient, admin: dict[str, str]) -> AgentHandle:
    created = _register(api, admin)
    r = api.post(
        "/api/agent/enroll",
        json={"token": created["enrollment_token"], "csr_pem": _csr(), "version": "0.0.0"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "BEGIN CERTIFICATE" in body["ca_pem"]
    return AgentHandle(api, body["agent_id"], body["certificate_pem"])


def _inventory(agent: AgentHandle, *emails: str) -> dict[str, str]:
    """Push accounts; returns email -> carbonio id."""
    ids = {e: uuid.uuid4().hex for e in emails}
    r = agent.post(
        "/inventory",
        {"accounts": [{"carbonio_id": i, "email": e, "status": "active"} for e, i in ids.items()]},
    )
    assert r.status_code == 200, r.text
    return ids


def _account_id(engine: Engine, email: str) -> uuid.UUID:
    with Session(engine) as s:
        account = s.scalar(select(Account).where(Account.email == email))
        assert account is not None
        return account.id


def _item(blob_byte: str, subject: str, folder: int = 2) -> dict[str, Any]:
    return {
        "kind": "message",
        "folder": folder,
        "blob": blob_byte * 64,
        "meta_blob": "f" * 63 + blob_byte,
        "size": 100,
        "meta_size": 40,
        "subject": subject,
        "sender": "bob@example.test",
        "date": "2026-11-17T09:00:00+00:00",
    }


FOLDERS = [
    {"id": 2, "parent": 1, "path": "/Inbox", "view": "message"},
    {"id": 5, "parent": 1, "path": "/Sent", "view": "message"},
]


# ------------------------------------------------------------------ enrolment & authentication


def test_enrollment_token_is_single_use(api: TestClient, admin: dict[str, str]) -> None:
    created = _register(api, admin)
    body = {"token": created["enrollment_token"], "csr_pem": _csr()}
    assert api.post("/api/agent/enroll", json=body).status_code == 200
    assert api.post("/api/agent/enroll", json=body).status_code == 401


def test_enrollment_rejects_expired_token_bad_csr_and_rsa(
    api: TestClient, admin: dict[str, str], engine: Engine
) -> None:
    created = _register(api, admin)
    bad = api.post(
        "/api/agent/enroll", json={"token": created["enrollment_token"], "csr_pem": "garbage"}
    )
    assert bad.status_code == 422
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_csr = api.post(
        "/api/agent/enroll", json={"token": created["enrollment_token"], "csr_pem": _csr(rsa_key)}
    )
    assert rsa_csr.status_code == 422
    from carborewind_api.db.models import Agent

    with Session(engine) as s, s.begin():
        s.execute(
            update(Agent)
            .where(Agent.id == uuid.UUID(created["id"]))
            .values(enrollment_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = api.post(
        "/api/agent/enroll", json={"token": created["enrollment_token"], "csr_pem": _csr()}
    )
    assert expired.status_code == 401


def test_agent_authentication(
    api: TestClient, agent: AgentHandle, admin: dict[str, str], settings: Settings
) -> None:
    assert agent.post("/jobs/claim").status_code in (200, 204)
    # Missing verification header, or a certificate we never issued.
    assert (
        api.post(
            "/api/agent/jobs/claim", headers={"X-Client-Cert": agent.headers["X-Client-Cert"]}
        ).status_code
        == 401
    )
    other = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rogue")])
    rogue = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(other.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(other, hashes.SHA256())
    )
    rogue_pem = rogue.public_bytes(serialization.Encoding.PEM).decode()
    assert (
        api.post(
            "/api/agent/jobs/claim",
            headers={"X-Client-Verify": "SUCCESS", "X-Client-Cert": quote(rogue_pem)},
        ).status_code
        == 401
    )
    # Revocation takes effect immediately.
    assert api.post(f"/api/agents/{agent.id}/revoke", headers=admin).status_code == 200
    assert agent.post("/jobs/claim").status_code == 401


def test_agent_headers_only_accepted_from_proxy(
    migrated: str, tmp_path: Path, agent: AgentHandle
) -> None:
    settings = Settings(
        environment="test",
        database_url=migrated,  # type: ignore[arg-type]
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=["10.0.0.1"],
    )
    with TestClient(create_app(settings), base_url="https://testserver") as direct:
        r = direct.post("/api/agent/jobs/claim", headers=agent.headers)
    assert r.status_code == 403


def test_only_super_admin_manages_agents(api: TestClient, engine: Engine) -> None:
    username = f"v{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=username,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=Role.VIEWER)],
            )
        )
    csrf = api.post("/api/auth/login", json={"username": username, "password": PASSWORD}).json()[
        "csrf_token"
    ]
    assert api.get("/api/agents").status_code == 403
    assert (
        api.post(
            "/api/agents", json={"name": "x", "hostname": "h"}, headers={"X-CSRF-Token": csrf}
        ).status_code
        == 403
    )


# ------------------------------------------------------------------ inventory


def test_inventory_upsert_and_complete_sync(agent: AgentHandle, engine: Engine) -> None:
    domain = f"i{uuid.uuid4().hex[:6]}.test"
    ids = _inventory(agent, f"a@{domain}", f"b@{domain}")
    r = agent.post(
        "/inventory",
        {
            "accounts": [
                {"carbonio_id": ids[f"a@{domain}"], "email": f"a@{domain}", "status": "locked"}
            ],
            "complete": True,
        },
    )
    assert r.json()["accounts_updated"] == 1
    assert r.json()["accounts_deleted"] >= 1
    with Session(engine) as s:
        statuses = {
            a.email: a.status
            for a in s.scalars(select(Account).join(Domain).where(Domain.name == domain))
        }
    assert statuses == {f"a@{domain}": "locked", f"b@{domain}": "deleted"}


# ------------------------------------------------------------------ job protocol


def _drain(agent: AgentHandle) -> None:
    """Fail any job left over by other tests so each test starts from an empty queue."""
    while (r := agent.post("/jobs/claim")).status_code == 200:
        agent.post(f"/jobs/{r.json()['job_id']}/fail", {"error": "drain", "retryable": False})


def test_full_then_incremental_backup_updates_catalogue(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    _drain(agent)
    email = f"alice@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    account_id = _account_id(engine, email)

    # 1. A user requests a backup; the agent claims it.
    job = api.post("/api/backups", json={"account_id": str(account_id)}, headers=admin)
    assert job.status_code == 202, job.text
    claimed = agent.post("/jobs/claim").json()
    assert claimed["job_id"] == job.json()["id"]
    assert claimed["seq"] == 1 and claimed["previous"] is None
    assert claimed["account_email"] == email
    assert (
        agent.post(
            f"/jobs/{claimed['job_id']}/heartbeat",
            {"checkpoint": {"done": {}}, "progress": {"items": 2}},
        ).status_code
        == 204
    )
    full = {
        "backup_type": "full",
        "manifest_ref": "m/1",
        "token": "100",
        "item_count": 3,
        "size_bytes": 300,
        "new_bytes": 250,
        "folders": FOLDERS,
        "added": {
            "257": _item("a", "Facture 1"),
            "258": _item("b", "Facture 2"),
            "259": _item("c", "Réponse", 5),
        },
    }
    assert agent.post(f"/jobs/{claimed['job_id']}/complete", full).status_code == 204
    # Retried complete (lost answer) is idempotent.
    assert agent.post(f"/jobs/{claimed['job_id']}/complete", full).status_code == 204
    assert api.get(f"/api/jobs/{claimed['job_id']}").json()["status"] == "succeeded"

    # 2. Incremental: 257 modified, 258 deleted, 260 added.
    api.post("/api/backups", json={"account_id": str(account_id)}, headers=admin)
    second = agent.post("/jobs/claim").json()
    assert second["seq"] == 2
    assert second["previous"]["token"] == "100"
    incr = {
        "backup_type": "incremental",
        "manifest_ref": "m/2",
        "token": "120",
        "item_count": 3,
        "size_bytes": 300,
        "new_bytes": 40,
        "folders": FOLDERS,
        "removed": [257, 258],
        "added": {"257": _item("d", "Facture 1 (lue)"), "260": _item("e", "Nouveau")},
    }
    assert agent.post(f"/jobs/{second['job_id']}/complete", incr).status_code == 204

    with Session(engine) as s:

        def at(seq: int) -> dict[int, str | None]:
            rows = s.execute(
                select(ItemVersion.carbonio_item_id, ItemVersion.subject).where(
                    ItemVersion.account_id == account_id,
                    ItemVersion.from_seq <= seq,
                    (ItemVersion.to_seq.is_(None)) | (ItemVersion.to_seq > seq),
                )
            )
            return dict(rows.tuples().all())

        assert at(1) == {257: "Facture 1", 258: "Facture 2", 259: "Réponse"}
        assert at(2) == {257: "Facture 1 (lue)", 259: "Réponse", 260: "Nouveau"}
        snaps = s.scalars(
            select(Snapshot).where(Snapshot.account_id == account_id).order_by(Snapshot.seq)
        ).all()
        assert [(x.seq, x.backup_type, x.status, x.sync_token) for x in snaps] == [
            (1, "full", "complete", "100"),
            (2, "incremental", "complete", "120"),
        ]


def test_abandoned_job_is_resumed_with_its_checkpoint(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    _drain(agent)
    email = f"bob@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    job_id = api.post(
        "/api/backups", json={"account_id": str(_account_id(engine, email))}, headers=admin
    ).json()["id"]
    first = agent.post("/jobs/claim").json()
    agent.post(f"/jobs/{job_id}/heartbeat", {"checkpoint": {"done": {"257": {"x": 1}}}})
    # The agent dies: nothing happens until the lease expires.
    assert agent.post("/jobs/claim").status_code == 204
    with Session(engine) as s, s.begin():
        s.execute(
            update(Job)
            .where(Job.id == uuid.UUID(job_id))
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    resumed = agent.post("/jobs/claim").json()
    assert resumed["job_id"] == job_id
    assert resumed["attempt"] == 2
    assert resumed["checkpoint"] == {"done": {"257": {"x": 1}}}
    assert (resumed["snapshot_id"], resumed["seq"]) == (first["snapshot_id"], first["seq"])


def test_lease_expiring_too_often_fails_the_job(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    _drain(agent)
    email = f"carl@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    job_id = api.post(
        "/api/backups", json={"account_id": str(_account_id(engine, email))}, headers=admin
    ).json()["id"]
    for _ in range(3):
        assert agent.post("/jobs/claim").status_code == 200
        with Session(engine) as s, s.begin():
            s.execute(
                update(Job)
                .where(Job.id == uuid.UUID(job_id))
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert agent.post("/jobs/claim").status_code == 204
    status = api.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "failed"
    assert "lease expired" in status["error"]


def test_failure_is_retried_then_final(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    _drain(agent)
    email = f"dan@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    job_id = api.post(
        "/api/backups", json={"account_id": str(_account_id(engine, email))}, headers=admin
    ).json()["id"]
    agent.post("/jobs/claim")
    agent.post(f"/jobs/{job_id}/fail", {"error": "carbonio busy", "retryable": True})
    assert api.get(f"/api/jobs/{job_id}").json()["status"] == "queued"
    agent.post("/jobs/claim")
    agent.post(f"/jobs/{job_id}/fail", {"error": "account.NO_SUCH_ACCOUNT", "retryable": False})
    final = api.get(f"/api/jobs/{job_id}").json()
    assert final["status"] == "failed"
    # A finished job cannot be completed or heart-beaten any more.
    assert agent.post(f"/jobs/{job_id}/heartbeat", {}).status_code == 409


def test_jobs_of_other_agents_are_not_accessible(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    _drain(agent)
    email = f"eve@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    api.post("/api/backups", json={"account_id": str(_account_id(engine, email))}, headers=admin)
    job_id = agent.post("/jobs/claim").json()["job_id"]
    created = _register(api, admin)
    body = api.post(
        "/api/agent/enroll", json={"token": created["enrollment_token"], "csr_pem": _csr()}
    ).json()
    other = AgentHandle(api, body["agent_id"], body["certificate_pem"])
    assert other.post(f"/jobs/{job_id}/heartbeat", {}).status_code == 404
    assert other.post(f"/jobs/{uuid.uuid4()}/fail", {"error": "x"}).status_code == 404


def test_backup_request_rbac(api: TestClient, agent: AgentHandle, engine: Engine) -> None:
    email = f"fay@{uuid.uuid4().hex[:6]}.test"
    _inventory(agent, email)
    account_id = _account_id(engine, email)
    with Session(engine) as s:
        domain_id = s.scalar(select(Account.domain_id).where(Account.id == account_id))
        other_domain = Domain(name=f"o{uuid.uuid4().hex[:6]}.test")
        s.add(other_domain)
        s.commit()
        other_id = other_domain.id

    def login(role: Role, domain: uuid.UUID | None) -> dict[str, str]:
        name = f"r{uuid.uuid4().hex[:8]}"
        with Session(engine) as s, s.begin():
            s.add(
                User(
                    username=name,
                    auth_source=AuthSource.LOCAL,
                    password_hash=hash_password(PASSWORD),
                    roles=[RoleAssignment(role=role, domain_id=domain)],
                )
            )
        api.cookies.clear()
        r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
        return {"X-CSRF-Token": r.json()["csrf_token"]}

    body = {"account_id": str(account_id)}
    assert (
        api.post("/api/backups", json=body, headers=login(Role.DOMAIN_ADMIN, other_id)).status_code
        == 403
    )
    assert api.post("/api/backups", json=body, headers=login(Role.VIEWER, None)).status_code == 403
    assert (
        api.post(
            "/api/backups",
            json={"account_id": str(uuid.uuid4())},
            headers=login(Role.DOMAIN_ADMIN, domain_id),
        ).status_code
        == 403
    )
    assert (
        api.post("/api/backups", json=body, headers=login(Role.DOMAIN_ADMIN, domain_id)).status_code
        == 202
    )


def test_pki_init_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from carborewind_api import cli
    from carborewind_api.config import get_settings

    monkeypatch.setenv("CARBOREWIND_PKI_DIR", str(tmp_path / "ca"))
    get_settings.cache_clear()
    try:
        assert cli.main(["pki-init"]) == 0
        assert (tmp_path / "ca" / "ca.key").stat().st_mode & 0o077 == 0
        assert cli.main(["pki-init"]) == 1  # never overwrites a CA
    finally:
        get_settings.cache_clear()


def test_proxy_recognised_by_shared_secret(
    migrated: str, tmp_path: Path, agent: AgentHandle
) -> None:
    settings = Settings(
        environment="test",
        database_url=migrated,  # type: ignore[arg-type]
        pki_dir=tmp_path / "pki",
        agent_proxy_ips=[],
        agent_proxy_secret="proxy-secret-for-tests",
    )
    with TestClient(create_app(settings), base_url="https://testserver") as api:
        assert api.post("/api/agent/jobs/claim", headers=agent.headers).status_code == 403
        wrong = {**agent.headers, "X-Proxy-Auth": "nope"}
        assert api.post("/api/agent/jobs/claim", headers=wrong).status_code == 403
        good = {**agent.headers, "X-Proxy-Auth": "proxy-secret-for-tests"}
        assert api.post("/api/agent/jobs/claim", headers=good).status_code in (200, 204)


def test_cli_agent_create_and_backup(
    api: TestClient,
    settings: Settings,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from carborewind_api import cli
    from carborewind_api.config import get_settings

    monkeypatch.setenv("CARBOREWIND_DATABASE_URL", str(settings.database_url.get_secret_value()))  # type: ignore[union-attr]
    get_settings.cache_clear()
    try:
        assert cli.main(["agent-create", f"cli-{uuid.uuid4().hex[:6]}", "mx.example.test"]) == 0
        token = capsys.readouterr().out.strip().splitlines()[-1]
        enrolled = api.post("/api/agent/enroll", json={"token": token, "csr_pem": _csr()})
        assert enrolled.status_code == 200
        body = enrolled.json()
        agent = AgentHandle(api, body["agent_id"], body["certificate_pem"])
        _drain(agent)
        email = f"cli@{uuid.uuid4().hex[:6]}.test"
        _inventory(agent, email)
        assert cli.main(["backup", email.upper(), "--full"]) == 0
        claimed = agent.post("/jobs/claim").json()
        assert claimed["account_email"] == email
        assert claimed["params"]["backup_type"] == "full"
        with pytest.raises(SystemExit, match="unknown account"):
            cli.main(["backup", "nobody@nowhere.test"])
    finally:
        get_settings.cache_clear()


def test_cli_agent_create_reissues_pending_token_and_protects_enrolled(
    api: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from carborewind_api import cli
    from carborewind_api.config import get_settings

    monkeypatch.setenv("CARBOREWIND_DATABASE_URL", str(settings.database_url.get_secret_value()))  # type: ignore[union-attr]
    get_settings.cache_clear()
    name = f"re-{uuid.uuid4().hex[:6]}"
    try:
        assert cli.main(["agent-create", name, "mx.example.test"]) == 0
        first = capsys.readouterr().out.strip().splitlines()[-1]
        # Same name again while still pending: new token, the old one stops working.
        assert cli.main(["agent-create", name, "mx2.example.test"]) == 0
        second = capsys.readouterr().out.strip().splitlines()[-1]
        assert second != first
        assert (
            api.post("/api/agent/enroll", json={"token": first, "csr_pem": _csr()}).status_code
            == 401
        )
        assert (
            api.post("/api/agent/enroll", json={"token": second, "csr_pem": _csr()}).status_code
            == 200
        )
        # Once enrolled, the name is protected.
        with pytest.raises(SystemExit, match="is active"):
            cli.main(["agent-create", name, "evil.example.test"])
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------ restore


def _login_as(
    api: TestClient, engine: Engine, role: Role, domain: uuid.UUID | None
) -> dict[str, str]:
    name = f"r{uuid.uuid4().hex[:8]}"
    with Session(engine) as s, s.begin():
        s.add(
            User(
                username=name,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password(PASSWORD),
                roles=[RoleAssignment(role=role, domain_id=domain)],
            )
        )
    api.cookies.clear()
    r = api.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def _two_snapshots(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine, email: str
) -> tuple[uuid.UUID, list[str]]:
    """Full backup (257, 258 in Inbox, 259 in Sent) then incremental (257 changed, 258 gone)."""
    _drain(agent)
    _inventory(agent, email)
    account_id = _account_id(engine, email)
    snapshots = []
    for body in (
        {
            "backup_type": "full",
            "manifest_ref": "m/1",
            "token": "100",
            "item_count": 3,
            "size_bytes": 300,
            "new_bytes": 300,
            "folders": FOLDERS,
            "added": {
                "257": _item("a", "Facture 1"),
                "258": _item("b", "Facture 2"),
                "259": _item("c", "Réponse", 5),
            },
        },
        {
            "backup_type": "incremental",
            "manifest_ref": "m/2",
            "token": "120",
            "item_count": 2,
            "size_bytes": 200,
            "new_bytes": 100,
            "folders": FOLDERS,
            "removed": [257, 258],
            "added": {"257": _item("d", "Facture 1 (lue)")},
        },
    ):
        r = api.post("/api/backups", json={"account_id": str(account_id)}, headers=admin)
        assert r.status_code == 202, r.text
        claimed = agent.post("/jobs/claim").json()
        assert agent.post(f"/jobs/{claimed['job_id']}/complete", body).status_code == 204
        snapshots.append(claimed["snapshot_id"])
    return account_id, snapshots


def test_snapshot_catalogue_is_point_in_time(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    account_id, (s1, s2) = _two_snapshots(
        api, admin, agent, engine, f"hugo@{uuid.uuid4().hex[:6]}.test"
    )
    base = f"/api/accounts/{account_id}/snapshots"
    folders = {f["path"]: f for f in api.get(f"{base}/{s1}/folders").json()}
    assert folders["/Inbox"]["item_count"] == 2 and folders["/Sent"]["item_count"] == 1
    assert folders["/Inbox"]["view"] == "message"
    assert api.get(f"{base}/{s2}/folders").json()[0]["item_count"] == 1

    def subjects(snap: str, **params: Any) -> list[str]:
        page = api.get(f"{base}/{snap}/items", params=params).json()
        return sorted(i["subject"] for i in page["items"])

    assert subjects(s1) == ["Facture 1", "Facture 2", "Réponse"]
    assert subjects(s2) == ["Facture 1 (lue)", "Réponse"]
    assert subjects(s1, q="FACTURE") == ["Facture 1", "Facture 2"]
    assert subjects(s1, q="%") == []  # wildcards are literal
    assert subjects(s1, folder_id=5) == ["Réponse"]
    page = api.get(f"{base}/{s1}/items", params={"limit": 1}).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    # A snapshot of another account is refused like an unknown one.
    other, _ = _two_snapshots(api, admin, agent, engine, f"ines@{uuid.uuid4().hex[:6]}.test")
    assert api.get(f"/api/accounts/{other}/snapshots/{s1}/folders").status_code == 403
    assert api.get(f"{base}/{uuid.uuid4()}/items").status_code == 403


def test_restore_request_claim_and_result(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    email = f"jade@{uuid.uuid4().hex[:6]}.test"
    account_id, (s1, _) = _two_snapshots(api, admin, agent, engine, email)
    r = api.post(
        "/api/restores",
        json={"account_id": str(account_id), "snapshot_id": s1, "item_ids": [258, 258]},
        headers=admin,
    )
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["kind"] == "restore" and job["status"] == "queued"
    assert (job["restore_mode"], job["dry_run"], job["snapshot_seq"]) == ("new_folder", False, 1)
    assert job["source_email"] == job["account_email"] == email

    claimed = agent.post("/jobs/claim").json()
    assert claimed["job_id"] == job["id"] and claimed["kind"] == "restore"
    params = claimed["params"]
    assert params["item_ids"] == [258] and params["snapshot_id"] == s1
    assert params["label"].startswith("CarboRewind ") and params["label"].endswith(" UTC #1")
    assert "/" not in params["label"]
    assert claimed["carbonio_id"] == params["source_carbonio_id"]

    result = {
        "dry_run": False,
        "mode": "new_folder",
        "root": "/" + params["label"],
        "selected": 1,
        "restored": 0,
        "present": 0,
        "unsupported": 0,
        "failed": {"258": "mail.MESSAGE_TOO_BIG"},
        "by_kind": {},
        "folders_created": ["/" + params["label"], "/" + params["label"] + "/Inbox"],
    }
    assert agent.post(f"/jobs/{job['id']}/restore-complete", result).status_code == 204
    assert agent.post(f"/jobs/{job['id']}/restore-complete", result).status_code == 204
    done = api.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "succeeded"
    assert done["error"] == "1 item(s) could not be restored"
    assert done["progress"]["failed_count"] == 1
    with Session(engine) as s:
        event = s.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "restore.complete")
            .order_by(AuditEvent.id.desc())
        )
        assert event is not None and event.outcome == "failure"
        assert event.details["source"] == email

    # A backup job cannot be completed as a restore.
    api.post("/api/backups", json={"account_id": str(account_id)}, headers=admin)
    backup = agent.post("/jobs/claim").json()
    assert agent.post(f"/jobs/{backup['job_id']}/restore-complete", result).status_code == 422


def test_restore_request_validation_and_rbac(
    api: TestClient, admin: dict[str, str], agent: AgentHandle, engine: Engine
) -> None:
    email = f"kim@{uuid.uuid4().hex[:6]}.test"
    account_id, (s1, _) = _two_snapshots(api, admin, agent, engine, email)
    other_email = f"lea@{email.split('@')[1]}"
    _inventory(agent, other_email)
    other = _account_id(engine, other_email)
    base = {"account_id": str(account_id), "snapshot_id": s1}

    def post(body: dict[str, Any], headers: dict[str, str] = admin) -> int:
        return api.post("/api/restores", json=body, headers=headers).status_code

    assert post({**base, "target_account_id": str(other), "mode": "original"}) == 422
    assert post({**base, "folder_ids": [2], "item_ids": [257]}) == 422
    assert post({**base, "mode": "overwrite"}) == 422
    assert post({**base, "target_account_id": str(uuid.uuid4())}) == 403
    assert post({**base, "target_account_id": str(other), "dry_run": True}) == 202
    with Session(engine) as s, s.begin():
        s.execute(update(Account).where(Account.id == other).values(status=AccountStatus.DELETED))
    assert post({**base, "target_account_id": str(other)}) == 409
    with Session(engine) as s, s.begin():
        s.execute(
            update(Snapshot)
            .where(Snapshot.id == uuid.UUID(s1))
            .values(status=SnapshotStatus.EXPIRED)
        )
    assert post(base) == 410

    with Session(engine) as s:
        domain_id = s.scalar(select(Account.domain_id).where(Account.id == account_id))
    s2 = api.get(f"/api/accounts/{account_id}/snapshots").json()["items"][0]["id"]
    ok = {**base, "snapshot_id": s2, "dry_run": True}
    # Viewers see the folder tree but neither item subjects nor the restore button.
    viewer = _login_as(api, engine, Role.VIEWER, domain_id)
    assert api.get(f"/api/accounts/{account_id}/snapshots/{s2}/folders").status_code == 200
    assert api.get(f"/api/accounts/{account_id}/snapshots/{s2}/items").status_code == 403
    assert post(ok, viewer) == 403
    with Session(engine) as s, s.begin():
        elsewhere = Domain(name=f"x{uuid.uuid4().hex[:6]}.test")
        s.add(elsewhere)
        s.flush()
        elsewhere_id = elsewhere.id
    outsider = _login_as(api, engine, Role.RESTORE_OPERATOR, elsewhere_id)
    assert post(ok, outsider) == 403
    operator = _login_as(api, engine, Role.RESTORE_OPERATOR, domain_id)
    assert api.get(f"/api/accounts/{account_id}/snapshots/{s2}/items").status_code == 200
    assert post(ok, operator) == 202
