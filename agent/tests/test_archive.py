"""Archiving engine: selection, storage read back, deletion, restore from the archive."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.archive import ArchiveError, MailboxArchiver
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.config import AgentSettings
from carborewind_agent.purge import run_purge
from carborewind_agent.replicate import ReplicaError
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.restore import AccountRestore
from carborewind_agent.worker import Worker, archive_manifest

from .fake_carbonio import FakeCarbonio, Folder, Mailbox
from .test_backup import eml
from .test_carbonio_client import _settings
from .test_worker import FakeApi


def ms(year: int, month: int = 1, day: int = 1) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


BEFORE = "2026-01-01T00:00:00+00:00"
OLD, YOUNG = ms(2025, 3, 1), ms(2026, 6, 1)


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


@pytest.fixture
def alice(fake: FakeCarbonio) -> Mailbox:
    box = fake.add_mailbox("alice@example.test")
    for fid, name, parent in (
        (3, "Trash", 1),
        (4, "Junk", 1),
        (6, "Drafts", 1),
        (60, "Old", 3),  # below the Trash
        (50, "Projets", 2),
        (51, "Privé", 2),
    ):
        box.folders[fid] = Folder(fid, name, parent, "message")
    box.add("m", 2, eml("Facture 2025"), date=OLD, flags="a")
    box.add("m", 50, eml("Projet A"), date=OLD + 1000)
    box.add("m", 5, eml("Réponse"), date=OLD + 2000, flags="s")
    box.add("m", 2, eml("Important"), date=OLD + 3000, flags="f")
    box.add("m", 2, eml("Pas lu"), date=OLD + 4000, flags="u")
    box.add("m", 51, eml("Privé"), date=OLD + 5000)
    box.add("m", 3, eml("Corbeille"), date=OLD)
    box.add("m", 60, eml("Corbeille ancienne"), date=OLD)
    box.add("m", 4, eml("Spam"), date=OLD)
    box.add("m", 6, eml("Brouillon"), date=OLD)
    box.add("m", 2, eml("Récent"), date=YOUNG)
    return box


def _params(**kw: Any) -> dict[str, Any]:
    return {"mode": "archive", "before": BEFORE, "exclude_folders": ["/Inbox/Privé"], **kw}


def _subjects(box: Mailbox) -> set[str]:
    out = set()
    for item in box.items.values():
        line = next(x for x in item.raw.decode().splitlines() if x.startswith("Subject:"))
        out.add(line.removeprefix("Subject: "))
    return out


def test_simulation_counts_without_touching_anything(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    before = dict(alice.items)
    result = MailboxArchiver(client, repo).run(
        alice.id, alice.name, "a1", _params(mode="simulate", keep_unread=True)
    )
    # Facture, Projet A, Réponse; not: flagged, unread, Privé (excluded), Trash/Junk/Drafts
    # (and below), the recent one.
    assert result.candidates == 3
    assert set(result.by_folder) == {"/Inbox", "/Inbox/Projets", "/Sent"}
    assert result.by_folder["/Inbox"][0] == 1
    assert result.candidate_bytes == sum(r[1] for r in result.by_folder.values())
    assert result.archived == 0 and result.removed == []
    assert alice.items == before
    assert list(repo.blob_ids()) == []


def test_archive_stores_reads_back_reports_then_deletes(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    originals = {i.id: i.raw for i in alice.items.values()}
    reported: list[dict[str, Any]] = []
    checked: list[str] = []
    order: list[str] = []

    def report(entries: list[dict[str, Any]]) -> None:
        order.append("report")
        reported.extend(entries)

    def replica(blobs: list[str]) -> None:
        order.append("replica")
        checked.extend(blobs)

    result = MailboxArchiver(client, repo).run(
        alice.id, alice.name, "a1", _params(), report=report, replica_check=replica
    )
    assert result.archived == 4  # the unread one is archived too (keep_unread off)
    assert len(result.removed) == 4 and result.removed_bytes == result.archived_bytes
    assert result.replica_checked and order == ["report", "replica"]
    assert _subjects(alice) == {
        "Important",
        "Privé",
        "Corbeille",
        "Corbeille ancienne",
        "Spam",
        "Brouillon",
        "Récent",
    }
    by_id = {e["item_id"]: e for e in reported}
    assert set(by_id) == set(result.removed)
    for item_id, entry in by_id.items():
        assert repo.get(entry["blob"]) == originals[item_id]
        assert entry["blob"] in checked and entry["meta_blob"] in checked
    facture = next(e for e in reported if e["subject"] == "Facture 2025")
    assert facture["folder_path"] == "/Inbox" and facture["has_attachments"] is True
    assert facture["sender"] == "Bob <bob@example.test>"
    assert facture["recipients"] == "alice@example.test"
    manifest = repo.get_archive(alice.id, "a1")
    assert set(manifest["items"]) == {str(i) for i in result.removed}
    assert result.manifest_ref == f"archives/{alice.id}/a1.json"
    assert repo.verify().manifests_checked == 1


def test_copy_mode_keeps_the_mailbox_and_skips_known_messages(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    first = MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(mode="copy"))
    assert first.archived == 4 and first.removed == []
    assert len(alice.items) == 11
    known = [int(i) for i in repo.get_archive(alice.id, "a1")["items"]]
    again = MailboxArchiver(client, repo).run(
        alice.id, alice.name, "a2", _params(mode="copy", skip_ids=known)
    )
    assert again.candidates == 0 and again.manifest_ref is None


def test_limit_per_run(client: CarbonioClient, repo: Repository, alice: Mailbox) -> None:
    result = MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(max_messages=2))
    assert result.archived == 2 and len(result.removed) == 2 and result.truncated
    assert "Facture 2025" not in _subjects(alice)  # oldest first


def test_copy_2_failure_deletes_nothing(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    def broken(_: list[str]) -> None:
        raise ReplicaError("share unreachable")

    with pytest.raises(ReplicaError):
        MailboxArchiver(client, repo).run(
            alice.id, alice.name, "a1", _params(), replica_check=broken
        )
    assert len(alice.items) == 11


def test_protected_account_and_bad_parameters(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    archiver = MailboxArchiver(client, repo, protected={"ALICE@example.test"})
    with pytest.raises(ArchiveError, match="protected"):
        archiver.run(alice.id, alice.name, "a1", _params())
    with pytest.raises(ArchiveError, match="mode"):
        MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(mode="move"))
    with pytest.raises(ArchiveError, match="cut-off"):
        MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", {"mode": "copy"})


def test_resume_does_not_store_or_delete_twice(
    client: CarbonioClient, repo: Repository, alice: Mailbox, fake: FakeCarbonio
) -> None:
    states: list[dict[str, Any]] = []
    first = MailboxArchiver(client, repo, checkpoint_every=1).run(
        alice.id, alice.name, "a1", _params(), checkpoint=states.append
    )
    # Pretend the answer was lost after the deletions: the retry resumes from the checkpoint.
    fake.calls.clear()
    again = MailboxArchiver(client, repo).run(
        alice.id, alice.name, "a1", _params(), resume=states[-1]
    )
    assert again.removed == first.removed
    assert "rest:get" not in fake.calls and "MsgActionRequest" not in fake.calls
    # Half-way: half the messages already gone, the others not.
    half = {**states[-1], "removed": first.removed[:2]}
    gone_twice = MailboxArchiver(client, repo).run(
        alice.id, alice.name, "a1", _params(), resume=half
    )
    assert gone_twice.removed == first.removed and gone_twice.errors == {}


def test_restore_from_the_archive(client: CarbonioClient, repo: Repository, alice: Mailbox) -> None:
    reported: list[dict[str, Any]] = []
    MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(), report=reported.extend)
    items = [
        {
            "id": e["item_id"],
            "blob": e["blob"],
            "meta_blob": e["meta_blob"],
            "folder_path": e["folder_path"],
        }
        for e in reported
    ]
    result = AccountRestore(client, repo).run(
        alice.id,
        "archive",
        alice.id,
        alice.name,
        mode="original",
        manifest=archive_manifest(items),
    )
    assert result.restored == 4
    projet = next(i for i in alice.items.values() if b"Projet A" in i.raw)
    assert projet.folder == 50 and projet.date == OLD + 1000


def test_purge_removes_archive_manifests(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(mode="copy"))
    result = run_purge(repo, {"archives": [{"account": alice.id, "archive": "a1"}]})
    assert result.archives_deleted == ["a1"]
    assert result.to_payload()["archives_deleted"] == ["a1"]
    assert not (repo.root / "archives" / alice.id / "a1.json").exists()


# ------------------------------------------------------------------ worker


def _worker(
    tmp_path: Path, api: FakeApi, client: CarbonioClient, repo: Repository, **kw: Any
) -> Worker:
    settings: AgentSettings = _settings(tmp_path)
    return Worker(
        AgentApi(settings, http=api.client()),
        client,
        AccountBackup(client, repo),
        AccountRestore(client, repo),
        repo,
        archiver=MailboxArchiver(client, repo),
        sleep=lambda _: True,
        **kw,
    )


def _job(box: Mailbox, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": "j-1",
        "kind": kind,
        "attempt": 1,
        "lease_seconds": 30,
        "params": params,
        "checkpoint": None,
        "carbonio_id": box.id,
        "account_email": box.name,
    }


def test_worker_runs_archive_and_fetch_jobs(
    tmp_path: Path, client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    api = FakeApi([_job(alice, "archive", _params())])
    assert _worker(tmp_path, api, client, repo).run_once()
    assert api.paths() == ["/jobs/claim", "/jobs/j-1/archive-stored", "/jobs/j-1/archive-complete"]
    stored = api.calls[1][1]["entries"]
    done = api.calls[2][1]
    assert done["mode"] == "archive" and len(done["removed"]) == 4 == len(stored)
    assert done["replica_checked"] is False  # no copy 2 on this agent

    blob = stored[0]["blob"]
    api = FakeApi([_job(alice, "fetch", {"blob": blob})])
    assert _worker(tmp_path, api, client, repo).run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/j-1/fetch-complete"
    assert base64.b64decode(body["data"]) == repo.get(blob)

    api = FakeApi([_job(alice, "fetch", {"blob": blob, "max_bytes": 10})])
    assert _worker(tmp_path, api, client, repo).run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/j-1/fail" and "too large" in body["error"] and not body["retryable"]


def test_worker_checks_copy_2_before_deleting(
    tmp_path: Path,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from carborewind_agent import worker as worker_module
    from carborewind_agent.replicate import ReplicaResult

    replica = tmp_path / "replica"
    replica.mkdir()
    kek = generate_key()
    seen: list[Path] = []

    def fake_replicate(src: Path, dst: Path, min_free_bytes: int = 0) -> ReplicaResult:
        seen.append(dst)
        return ReplicaResult(errors=["blobs/xx: I/O error"])

    monkeypatch.setattr(worker_module, "replicate", fake_replicate)
    api = FakeApi([_job(alice, "archive", _params())])
    w = _worker(tmp_path, api, client, repo, replica=replica, kek=kek)
    assert w.run_once()
    assert seen == [replica]
    assert api.paths()[-1] == "/jobs/j-1/fail" and api.calls[-1][1]["retryable"] is False
    assert len(alice.items) == 11  # nothing deleted


def test_restore_job_with_archive_items(
    tmp_path: Path, client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    reported: list[dict[str, Any]] = []
    MailboxArchiver(client, repo).run(alice.id, alice.name, "a1", _params(), report=reported.extend)
    items = [
        {
            "id": e["item_id"],
            "blob": e["blob"],
            "meta_blob": e["meta_blob"],
            "folder_path": e["folder_path"],
        }
        for e in reported[:2]
    ]
    job = _job(
        alice,
        "restore",
        {
            "source_carbonio_id": alice.id,
            "mode": "new_folder",
            "label": "Archives restaurées",
            "archive_items": items,
        },
    )
    api = FakeApi([job])
    assert _worker(tmp_path, api, client, repo).run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/j-1/restore-complete" and body["restored"] == 2
    assert any(f.name == "Archives restaurées" for f in alice.folders.values())
    assert json.dumps(body)  # serialisable


def test_legal_hold_copies_every_folder(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    before = len(alice.items)
    result = MailboxArchiver(client, repo).run(
        alice.id,
        alice.name,
        "h1",
        {
            "mode": "copy",
            "before": "2100-01-01T00:00:00+00:00",
            "all_folders": True,
            "keep_flagged": False,
            "keep_unread": False,
            "max_messages": 200_000,
        },
    )
    assert result.archived == before and result.removed == []
    assert len(alice.items) == before  # nothing leaves the mailbox
    folders = {e["folder_path"] for e in repo.get_archive(alice.id, "h1")["items"].values()}
    assert {"/Trash", "/Trash/Old", "/Junk", "/Drafts"} <= folders
