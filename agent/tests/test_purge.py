"""Repository garbage collection driven by a purge job."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.repository import Repository, RepositoryError, generate_key
from carborewind_agent.worker import Worker

from .fake_carbonio import FakeCarbonio, Mailbox
from .test_backup import eml
from .test_carbonio_client import _settings
from .test_worker import FakeApi


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    from carborewind_agent.carbonio.factory import build_client

    return build_client(_settings(tmp_path), transport=fake.transport())


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository.init(tmp_path / "repo", generate_key())


def test_purge_job_deletes_manifests_and_blobs(
    tmp_path: Path, fake: FakeCarbonio, client: CarbonioClient, repo: Repository
) -> None:
    box: Mailbox = fake.add_mailbox("alice@example.test")
    first = box.add("m", 2, eml("Ancien"))
    box.add("m", 2, eml("Gardé"))
    engine = AccountBackup(client, repo)
    s1 = engine.run(box.id, box.name, "s1", 1)
    box.delete(first.id)
    s2 = engine.run(box.id, box.name, "s2", 2, ("s1", s1.token))
    # What the API would plan: snapshot 1 expired, blobs only it referenced.
    old = repo.get_manifest(box.id, "s1")["items"][str(first.id)]
    params: dict[str, Any] = {
        "manifests": [
            {"account": box.id, "snapshot": "s1"},
            {"account": "../etc", "snapshot": "x"},  # rejected, reported by omission
        ],
        "blobs": [old["blob"], old["meta_blob"], "f" * 64, "not-a-blob"],
    }
    api = FakeApi(
        [
            {
                "job_id": "p-1",
                "kind": "purge",
                "attempt": 1,
                "lease_seconds": 30,
                "params": params,
                "checkpoint": None,
            }
        ]
    )
    worker = Worker(
        AgentApi(_settings(tmp_path), http=api.client()),
        client,
        engine,
        None,
        repo,
        sleep=lambda _: True,
    )
    assert worker.run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/p-1/purge-complete"
    assert body["manifests_deleted"] == ["s1"]
    assert body["blobs_deleted"] == [old["blob"], old["meta_blob"], "f" * 64]
    assert body["freed_bytes"] > 0
    with pytest.raises(RepositoryError):
        repo.get_manifest(box.id, "s1")
    with pytest.raises(RepositoryError):
        repo.get(old["blob"])
    # The kept snapshot is intact and the repository verifies.
    for entry in repo.get_manifest(box.id, "s2")["items"].values():
        repo.get(entry["blob"])
    assert repo.verify().ok
    assert s2.item_count == 1

    # Without a repository, purge jobs are refused.
    api = FakeApi(
        [
            {
                "job_id": "p-2",
                "kind": "purge",
                "attempt": 1,
                "lease_seconds": 30,
                "params": {},
                "checkpoint": None,
            }
        ]
    )
    Worker(AgentApi(_settings(tmp_path), http=api.client()), client, engine).run_once()
    assert api.calls[-1][0] == "/jobs/p-2/fail"
