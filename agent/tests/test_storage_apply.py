"""Storage settings from the interface: root helper, agent hand-off, copy 02 mirror."""

from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
from typing import Any

import httpx
import pytest

from carborewind_agent import storage, storage_apply
from carborewind_agent.api_client import AgentApi
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.replicate import ReplicaError, replicate
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.repository.keyfile import create_kek
from carborewind_agent.storage import StorageError
from carborewind_agent.storage_apply import apply, apply_command, read_request
from carborewind_agent.worker import Worker

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings

ME = os.getuid()
SHARE = {"share": "//nas1.lnhc.dz/backups", "username": "svc", "password": "p@ss", "seal": True}
COPY = {"share": "//nas2.lnhc.dz/copie02", "username": "svc2", "password": "s3cret"}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(storage.shutil, "which", lambda name: f"/sbin/{name}")
    user = pwd.struct_passwd(("carborewind", "x", ME, os.getgid(), "", "/", "/bin/false"))
    monkeypatch.setattr(storage.pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(storage_apply.pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(storage, "MOUNT_ROOT", str(tmp_path) + "/")
    monkeypatch.setattr(storage_apply, "ROOT_UID", ME)
    key = tmp_path / "repository.key"
    create_kek(key)
    local = tmp_path / "repository"
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        f"CARBOREWIND_AGENT_REPOSITORY_PATH={local}\nCARBOREWIND_AGENT_REPOSITORY_KEY_FILE={key}\n"
    )
    os.chmod(env_file, 0o640)
    calls: list[tuple[str, ...]] = []
    return {
        "tmp": tmp_path,
        "env_file": env_file,
        "local": local,
        "key": key,
        "calls": calls,
        "kw": {
            "etc": tmp_path / "etc",
            "systemd": tmp_path / "systemd",
            "run": lambda *cmd: calls.append(cmd),
            "primary_mount": tmp_path / "nas",
            "copy_mount": tmp_path / "copy02",
            "local_repository": local,
        },
    }


def read_env(env_file: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)


def test_primary_share_with_migration_then_copy_then_back(env: dict[str, Any]) -> None:
    from carborewind_agent.repository.keyfile import read_kek

    repo = Repository.init(env["local"], read_kek(env["key"]))
    blob = repo.put(b"hello").blob_id
    request = {
        "revision": 3,
        "primary_revision": 2,
        "copy_revision": 0,
        "primary": {"kind": "smb", "migrate": True, **SHARE},
        "copy": None,
    }
    status = apply(request, env["env_file"], **env["kw"])
    nas_repo = env["tmp"] / "nas" / "repository"
    assert status["changed"] == ["primary"]
    assert read_env(env["env_file"])["CARBOREWIND_AGENT_REPOSITORY_PATH"] == str(nas_repo)
    assert Repository.open(nas_repo, read_kek(env["key"])).get(blob) == b"hello"
    creds = env["tmp"] / "etc" / "smb.credentials"
    assert "password=p@ss" in creds.read_text() and oct(creds.stat().st_mode)[-3:] == "600"
    assert ("systemctl", "restart", "--no-block", "carborewind-agent.service") in env["calls"]
    # Same revisions again: nothing is redone.
    env["calls"].clear()
    assert apply(request, env["env_file"], **env["kw"])["changed"] == []
    assert env["calls"] == []
    # New password on the same share: remount.
    request["primary_revision"] = 4
    request["primary"]["password"] = "new"
    apply(request, env["env_file"], **env["kw"])
    assert any(c[:2] == ("systemctl", "restart") and c[2].endswith(".mount") for c in env["calls"])
    # Copy 02 on a second share.
    request.update(copy_revision=5, copy=COPY)
    status = apply(request, env["env_file"], **env["kw"])
    replica = env["tmp"] / "copy02" / "repository"
    assert status["changed"] == ["copy"] and status["replica"]["path"] == str(replica)
    assert read_env(env["env_file"])["CARBOREWIND_AGENT_REPLICA_PATH"] == str(replica)
    assert "password=s3cret" in (env["tmp"] / "etc" / "smb-copy02.credentials").read_text()
    assert not (env["tmp"] / "systemd" / "carborewind-agent.service.d" / "copy02.conf").exists()
    # Copy 02 removed, repository back on the local disk (copied back).
    request.update(copy_revision=6, copy=None, primary_revision=7, primary={"kind": "local"})
    status = apply(request, env["env_file"], **env["kw"])
    assert sorted(status["changed"]) == ["copy", "primary"]
    values = read_env(env["env_file"])
    assert "CARBOREWIND_AGENT_REPLICA_PATH" not in values
    assert values["CARBOREWIND_AGENT_REPOSITORY_PATH"] == str(env["local"])
    assert not (env["tmp"] / "etc" / "smb-copy02.credentials").exists()
    assert Repository.open(env["local"], read_kek(env["key"])).get(blob) == b"hello"


def test_new_share_without_repository_is_initialised(env: dict[str, Any]) -> None:
    request = {
        "revision": 1,
        "primary_revision": 1,
        "primary": {"kind": "smb", "migrate": False, **SHARE},
    }
    apply(request, env["env_file"], **env["kw"])
    assert (env["tmp"] / "nas" / "repository" / "config.json").exists()


@pytest.mark.parametrize(
    "primary",
    [
        {"kind": "ftp"},
        {"kind": "smb", **{**SHARE, "share": "//nas/../etc"}},
        {"kind": "smb", **{**SHARE, "share": "nas/backups"}},
        {"kind": "smb", **{**SHARE, "password": ""}},
        {"kind": "smb", **{**SHARE, "username": 42}},
        "smb",
    ],
)
def test_invalid_requests(env: dict[str, Any], primary: Any) -> None:
    with pytest.raises(StorageError):
        apply({"primary_revision": 1, "primary": primary}, env["env_file"], **env["kw"])
    with pytest.raises(StorageError):
        apply({"copy_revision": 1, "copy": "x"}, env["env_file"], **env["kw"])


def test_read_request_is_defensive(
    env: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "req.json"
    good.write_text(json.dumps({"revision": 1}))
    assert read_request(good, ME) == {"revision": 1}
    assert not good.exists()  # deleted at once
    secret = tmp_path / "secret"
    secret.write_text("root:x:0:0")
    link = tmp_path / "link.json"
    link.symlink_to(secret)
    with pytest.raises(StorageError):
        read_request(link, ME)
    assert secret.exists()
    for content in ("x" * 70_000, "not json", "[1, 2]"):
        bad = tmp_path / "bad.json"
        bad.write_text(content)
        with pytest.raises(StorageError):
            read_request(bad, ME)
    other = tmp_path / "other.json"
    other.write_text("{}")
    monkeypatch.setattr(storage_apply, "ROOT_UID", -1)
    with pytest.raises(StorageError):
        read_request(other, ME + 12345)
    directory = tmp_path / "dir.json"
    directory.mkdir()
    with pytest.raises(StorageError):
        read_request(directory, ME)
    with pytest.raises(StorageError):
        read_request(tmp_path / "missing.json", ME)


def test_apply_command_writes_a_status(env: dict[str, Any]) -> None:
    sdir = env["tmp"] / "storage"
    sdir.mkdir()
    assert apply_command(sdir, env["env_file"], **env["kw"]) == 0  # nothing to do
    assert not (sdir / "status.json").exists()
    (sdir / "request.json").write_text(
        json.dumps({"revision": 9, "copy_revision": 9, "copy": COPY})
    )
    assert apply_command(sdir, env["env_file"], **env["kw"]) == 0
    status = json.loads((sdir / "status.json").read_text())
    assert status["ok"] and status["revision"] == 9 and "s3cret" not in json.dumps(status)
    assert not (sdir / "request.json").exists()
    (sdir / "request.json").write_text(json.dumps({"revision": 10, "copy_revision": 10, "copy": 1}))
    assert apply_command(sdir, env["env_file"], **env["kw"]) == 1
    status = json.loads((sdir / "status.json").read_text())
    assert status["ok"] is False and "invalid copy 02" in status["error"]
    # A substituted (symlinked) env file is refused.
    real = env["tmp"] / "real.env"
    env["env_file"].rename(real)
    env["env_file"].symlink_to(real)
    with pytest.raises(StorageError):
        apply_command(sdir, env["env_file"], **env["kw"])


def test_replicate(tmp_path: Path) -> None:
    kek = generate_key()
    repo = Repository.init(tmp_path / "repo", kek)
    ids = [repo.put(f"blob {i}".encode()).blob_id for i in range(3)]
    repo.put_manifest("acc", "snap-1", {"items": {}})
    target = tmp_path / "copy"
    with pytest.raises(ReplicaError):
        replicate(repo.root, target)  # not a network mount
    with pytest.raises(ReplicaError):
        replicate(tmp_path / "nothing", target, require_mount=False)
    first = replicate(repo.root, target, require_mount=False)
    assert first.copied == first.files and not first.errors and first.copied_bytes > 0
    copy = Repository.open(target, kek)
    assert copy.get(ids[0]) == b"blob 0"
    second = replicate(repo.root, target, require_mount=False)
    assert second.copied == 0 and second.deleted == 0
    repo.delete(ids[1])
    repo.delete_manifest("acc", "snap-1")
    (target / "blobs" / ids[2][:2] / ids[2]).write_bytes(b"truncated")
    third = replicate(repo.root, target, require_mount=False)
    assert third.deleted == 2 and third.copied == 1
    assert not (target / "blobs" / ids[1][:2] / ids[1]).exists()
    assert third.to_payload()["files"] == third.files


def test_worker_hands_settings_over_and_replicates(tmp_path: Path) -> None:
    desired = {"revision": 2, "primary_revision": 2, "primary": {"kind": "local"}}
    calls: list[tuple[str, str, Any]] = []
    jobs = [{"job_id": "r1", "kind": "replicate", "attempt": 1, "lease_seconds": 300}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/agent")
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if path == "/storage":
            return httpx.Response(200, json=desired)
        if path == "/jobs/claim":
            return httpx.Response(200, json=jobs.pop(0)) if jobs else httpx.Response(204)
        return httpx.Response(204)

    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    fake = FakeCarbonio()
    client = build_client(_settings(tmp_path), transport=fake.transport())
    repo = Repository.init(tmp_path / "repo", generate_key())
    repo.put(b"x")
    sdir = tmp_path / "storage"
    replica = tmp_path / "copy02" / "repository"
    worker = Worker(
        AgentApi(_settings(tmp_path), http=http),
        client,
        AccountBackup(client, repo),
        repo=repo,
        storage_dir=sdir,
        replica=replica,
        mail_store=tmp_path,
    )
    worker.sync_storage()
    request = json.loads((sdir / "request.json").read_text())
    assert request == desired and oct((sdir / "request.json").stat().st_mode)[-3:] == "600"
    (sdir / "request.json").unlink()
    worker.sync_storage()  # already handed over: not written twice
    assert not (sdir / "request.json").exists()
    (sdir / "status.json").write_text(json.dumps({"revision": 2, "ok": True, "at": "now"}))
    worker.sync_storage()
    reported = [b for m, p, b in calls if p == "/storage-status"]
    assert reported and reported[0]["apply"]["ok"] is True and "replica" in reported[0]
    assert reported[0]["mail_store"]["path"] == str(tmp_path)
    assert reported[0]["mail_store"]["total_bytes"] > 0
    worker.sync_storage()  # unchanged status: not reported again
    assert len([p for _, p, _ in calls if p == "/storage-status"]) == 1
    (sdir / "status.json").write_text("garbage")
    report = worker.storage_report()
    assert report is not None and report["apply"]["ok"] is False
    # copy02 is not a network mount here: the job fails without writing anything.
    assert worker.run_once()
    fail = [b for m, p, b in calls if p == "/jobs/r1/fail"]
    assert fail and fail[0]["retryable"] is False and "not mounted" in fail[0]["error"]
    assert not replica.exists()
