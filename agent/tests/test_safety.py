"""Disk guard, bounded mailbox measurements, protected account, integrity verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from carborewind_agent import worker as worker_mod
from carborewind_agent.api_client import AgentApi
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.replicate import replicate
from carborewind_agent.repository import Repository, SpaceError, generate_key
from carborewind_agent.worker import Worker

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings


class Api:
    def __init__(self, jobs: list[dict[str, Any]] | None = None) -> None:
        self.jobs = jobs or []
        self.calls: list[tuple[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/agent")
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if path == "/jobs/claim":
            return httpx.Response(200, json=self.jobs.pop(0)) if self.jobs else httpx.Response(204)
        if path == "/inventory":
            return httpx.Response(200, json={})
        return httpx.Response(204)

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="https://api.test", transport=httpx.MockTransport(self.handler)
        )


def make(tmp_path: Path, api: Api, fake: FakeCarbonio, **kw: Any) -> tuple[Worker, Repository]:
    kek = generate_key()
    repo = Repository.init(tmp_path / "repo", kek)
    client = build_client(_settings(tmp_path), transport=fake.transport())
    w = Worker(
        AgentApi(_settings(tmp_path), http=api.client()),
        client,
        AccountBackup(client, repo),
        repo=repo,
        kek=kek,
        **kw,
    )
    return w, repo


def job(kind: str, **kw: Any) -> dict[str, Any]:
    return {"job_id": f"{kind}-1", "kind": kind, "attempt": 1, "lease_seconds": 300, **kw}


def test_backup_refused_when_the_disk_is_nearly_full(tmp_path: Path) -> None:
    fake = FakeCarbonio()
    box = fake.add_mailbox("ana@example.test")
    box.add("m", 2, b"Subject: x\r\n\r\nbody")
    backup = job(
        "backup",
        carbonio_id=box.id,
        account_email=box.name,
        snapshot_id="s1",
        seq=1,
        previous=None,
        params={},
    )
    api = Api([backup])
    w, repo = make(tmp_path, api, fake, space=(10**9, 15.0, 2.0))  # 1 EiB must stay free
    assert w.space_floor(repo.root) == 10**9 * 1024**3
    assert w.run_once()
    fail = next(b for p, b in api.calls if p.endswith("/fail"))
    assert fail["retryable"] is False and "not enough free space" in fail["error"]
    with pytest.raises(SpaceError):
        repo.check_space()
    repo.min_free_bytes = 1
    repo.check_space()
    repo.min_free_bytes = 10**30
    repo._writes = 0
    with pytest.raises(SpaceError):
        repo.put(b"new blob")
    repo.min_free_bytes = 0
    repo.put(b"new blob")  # no guard: written


def test_floor_on_a_network_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCarbonio()
    w, repo = make(tmp_path, Api(), fake, space=(20.0, 15.0, 2.0))
    monkeypatch.setattr(worker_mod, "describe", lambda p: {"kind": "network", "total_bytes": 10})
    assert w.space_floor(repo.root) == 2 * 1024**3
    monkeypatch.setattr(
        worker_mod, "describe", lambda p: {"kind": "local", "total_bytes": 1000 * 1024**3}
    )
    assert w.space_floor(repo.root) == 150 * 1024**3  # 15 % of 1000 GiB > 20 GiB


def test_mailbox_sizes_are_measured_in_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_mod, "MAX_SIZE_LOOKUPS", 2)
    fake = FakeCarbonio()
    boxes = [fake.add_mailbox(f"u{i}@example.test") for i in range(5)]
    fake.quota_denied = True
    api = Api()
    w, _ = make(tmp_path, api, fake)
    measured: list[set[str]] = []
    for _ in range(3):
        w.sync_inventory()
        accounts = api.calls[-1][1]["accounts"]
        measured.append({a["email"] for a in accounts if "used_bytes" in a})
    assert all(len(m) == 2 for m in measured)
    assert measured[0] | measured[1] | measured[2] == {b.name for b in boxes}


def test_verify_jobs_find_corruption(tmp_path: Path) -> None:
    fake = FakeCarbonio()
    replica = tmp_path / "copy02" / "repository"
    api = Api(
        [
            job("verify", params={"target": "primary", "sample": 1.0}),
            job("verify", params={"target": "replica", "sample": 1.0}),
            job("verify", params={"target": "replica"}),
        ]
    )
    w, repo = make(tmp_path, api, fake, replica=replica)
    ids = [repo.put(f"blob {i}".encode()).blob_id for i in range(3)]
    repo.put_manifest("acc", "s1", {"items": {}})
    # Corrupt one blob and one manifest.
    (repo.root / "blobs" / ids[0][:2] / ids[0]).write_bytes(b"tampered")
    (repo.root / "manifests" / "acc" / "s1.json").write_bytes(b"tampered")
    assert w.run_once()
    primary = next(b for p, b in api.calls if p == "/jobs/verify-1/verify-complete")
    assert primary["checked"] == 3 and primary["corrupted"] == [ids[0]]
    assert primary["manifests_corrupted"] == ["acc/s1"] and primary["target"] == "primary"
    # Copy 02 does not exist yet: the job fails clearly.
    assert w.run_once()
    fail = [b for p, b in api.calls if p.endswith("/fail")][-1]
    assert "no repository yet" in fail["error"]
    replicate(repo.root, replica, require_mount=False)
    assert w.run_once()
    second = [b for p, b in api.calls if p == "/jobs/verify-1/verify-complete"][-1]
    assert second["target"] == "replica" and second["corrupted_count"] == 1
    # Without copy 02 configured.
    api.jobs.append(job("verify", params={"target": "replica"}))
    bare, _ = make(tmp_path / "other", api, fake)
    assert bare.run_once()
    assert "not configured" in [b for p, b in api.calls if p.endswith("/fail")][-1]["error"]


def test_replica_stops_before_filling_copy02(tmp_path: Path) -> None:
    repo = Repository.init(tmp_path / "repo", generate_key())
    repo.put(b"x" * 1000)
    result = replicate(repo.root, tmp_path / "copy", require_mount=False, min_free_bytes=10**30)
    assert result.copied == 0 and "copy 02 is full" in result.errors[0]


def test_no_guard_without_configuration(tmp_path: Path) -> None:
    w, repo = make(tmp_path, Api(), FakeCarbonio())
    assert w.space_floor(repo.root) == 0
