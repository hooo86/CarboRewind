"""Restore engine: backup with the real engine, damage the mailbox, restore, compare."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio import CarbonioClient, SoapFaultError
from carborewind_agent.config import AgentSettings
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.restore import AccountRestore, RestoreError
from carborewind_agent.worker import Worker

from .fake_carbonio import FakeCarbonio, Folder, Mailbox
from .test_backup import eml
from .test_carbonio_client import _settings
from .test_worker import FakeApi

LABEL = "CarboRewind 2026-11-18 1200 #1"
VCARD = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bob Martin\r\nEND:VCARD\r\n"
ICS = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Point\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def alice(fake: FakeCarbonio) -> Mailbox:
    box = fake.add_mailbox("alice@example.test")
    box.folders[50] = Folder(50, "Projets", 2, "message")
    box.folders[51] = Folder(51, "2026", 50, "message")
    box.add("m", 2, eml("Facture 1"), flags="ua", tags="Important", date=1_700_000_000_000)
    box.add("m", 50, eml("Projet A"))
    box.add("m", 51, eml("Projet B"))
    box.add("m", 5, eml("Réponse"), flags="s")
    box.add("cn", 7, VCARD)
    box.add("appt", 10, ICS)
    box.add("doc", 2, b"%PDF-1.4")
    return box


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    from carborewind_agent.carbonio.factory import build_client

    return build_client(_settings(tmp_path), transport=fake.transport())


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository.init(tmp_path / "repo", generate_key())


@pytest.fixture
def snapshot(client: CarbonioClient, repo: Repository, alice: Mailbox) -> dict[int, bytes]:
    """Back up alice as snapshot s1; returns the original contents by item id."""
    AccountBackup(client, repo).run(alice.id, alice.name, "s1", 1)
    return {i: item.raw for i, item in alice.items.items()}


def _restore(client: CarbonioClient, repo: Repository, box: Mailbox, **kw: Any) -> Any:
    target: Mailbox = kw.pop("target", box)
    return AccountRestore(client, repo).run(box.id, "s1", target.id, target.name, label=LABEL, **kw)


def _path(fake: FakeCarbonio, box: Mailbox, folder_id: int) -> str:
    parts = []
    while folder_id != 1:
        f = box.folders[folder_id]
        parts.append(f.name)
        folder_id = f.parent or 1
    return "/" + "/".join(reversed(parts))


def _contents(fake: FakeCarbonio, box: Mailbox, ids: set[int]) -> dict[str, list[bytes]]:
    out: dict[str, list[bytes]] = {}
    for i, item in box.items.items():
        if i not in ids:
            out.setdefault(_path(fake, box, item.folder), []).append(item.raw)
    return out


def test_new_folder_restore_recreates_the_tree(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    before = set(alice.items)
    result = _restore(client, repo, alice)
    assert result.selected == 7
    assert result.restored == 6
    assert result.unsupported == 1  # the Files document
    assert result.by_kind == {"message": 4, "contact": 1, "appointment": 1}
    assert not result.failed
    assert result.root == f"/{LABEL}"
    new = _contents(fake, alice, before)
    assert new[f"/{LABEL}/Inbox"] == [snapshot[min(snapshot)]]
    assert new[f"/{LABEL}/Inbox/Projets/2026"] == [eml("Projet B")]
    assert new[f"/{LABEL}/Sent"] == [eml("Réponse")]
    assert new[f"/Contacts ({LABEL})"] == [VCARD]
    assert new[f"/Calendar ({LABEL})"] == [ICS]
    # Contacts and calendars get folders of their own view.
    views = {_path(fake, alice, f.id): f.view for f in alice.folders.values()}
    assert views[f"/Contacts ({LABEL})"] == "contact"
    assert views[f"/Calendar ({LABEL})"] == "appointment"
    assert views[f"/{LABEL}/Inbox/Projets"] == "message"
    # Flags kept (minus the derived attachment flag), tags, received date.
    restored = next(i for i in alice.items.values() if i.id not in before and i.tags)
    assert (restored.flags, restored.tags, restored.date) == ("u", "Important", 1_700_000_000_000)
    assert f"/{LABEL}/Inbox/Projets/2026" in result.folders_created
    assert len(alice.items) == len(before) + 6


def test_original_mode_restores_only_what_was_deleted(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    lost = next(i for i, it in alice.items.items() if it.folder == 51)
    alice.delete(lost)
    del alice.folders[51]  # the folder was deleted too
    result = _restore(client, repo, alice, mode="original")
    assert result.restored == 1
    assert result.present == 5
    assert result.root is None
    assert result.folders_created == ["/Inbox/Projets/2026"]
    paths = _contents(fake, alice, set())
    assert paths["/Inbox/Projets/2026"] == [eml("Projet B")]
    assert len(alice.items) == 7


def test_selection_by_folder_and_by_item(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    before = set(alice.items)
    result = _restore(client, repo, alice, folder_ids=[50])  # includes the 2026 subfolder
    assert result.selected == 2 and result.restored == 2
    one = min(snapshot)
    result = _restore(client, repo, alice, item_ids=[one, one])
    assert result.selected == 1 and result.restored == 1
    assert len(alice.items) == len(before) + 3
    with pytest.raises(RestoreError, match="item"):
        _restore(client, repo, alice, item_ids=[999_999])
    with pytest.raises(RestoreError, match="folder"):
        _restore(client, repo, alice, folder_ids=[999_999])


def test_dry_run_writes_nothing(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    folders, items = dict(alice.folders), dict(alice.items)
    fake.calls.clear()
    result = _restore(client, repo, alice, dry_run=True)
    assert result.dry_run and result.restored == 6
    assert f"/{LABEL}" in result.folders_created
    assert alice.folders == folders and alice.items == items
    assert not {"CreateFolderRequest", "AddMsgRequest"} & set(fake.calls)
    assert not any(c.startswith("import:") for c in fake.calls)


def test_restore_into_another_account(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    bob = fake.add_mailbox("bob@example.test")
    result = _restore(client, repo, alice, target=bob, item_ids=[min(snapshot)])
    assert result.restored == 1
    assert _contents(fake, bob, set())[f"/{LABEL}/Inbox"] == [snapshot[min(snapshot)]]
    with pytest.raises(RestoreError, match="source account only"):
        _restore(client, repo, alice, target=bob, mode="original")


def test_invalid_requests(client: CarbonioClient, repo: Repository, alice: Mailbox) -> None:
    engine = AccountRestore(client, repo)
    with pytest.raises(RestoreError, match="mode"):
        engine.run(alice.id, "s1", alice.id, alice.name, mode="overwrite")
    with pytest.raises(RestoreError, match="label"):
        engine.run(alice.id, "s1", alice.id, alice.name, label="a/b")


def test_resume_skips_done_items_and_checkpoints(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    states: list[dict[str, Any]] = []
    first, second = sorted(i for i, it in alice.items.items() if it.kind == "m")[:2]
    engine = AccountRestore(client, repo, checkpoint_every=1)
    before = len(alice.items)
    result = engine.run(
        alice.id,
        "s1",
        alice.id,
        alice.name,
        label=LABEL,
        resume={"done": [first], "present": [second]},
        checkpoint=states.append,
    )
    assert result.restored == 5 and result.present == 1
    assert len(alice.items) == before + 4  # first was already done, second already present
    assert states and first in states[-1]["done"]


def test_item_failures_tags_and_fatal_faults(
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = client.add_message
    seen: list[str] = []

    def flaky(account_id: str, folder_id: int, raw: bytes, **kw: Any) -> int:
        seen.append(kw.get("tags", ""))
        if kw.get("tags"):
            raise SoapFaultError("mail.INVALID_NAME", "no such tag")
        if b"Projet A" in raw:
            raise SoapFaultError("mail.MESSAGE_TOO_BIG", "too big")
        return original(account_id, folder_id, raw, **kw)

    monkeypatch.setattr(client, "add_message", flaky)
    result = _restore(client, repo, alice)
    assert "Important" in seen  # tried with the tag, then without
    assert list(result.failed.values()) == ["mail.MESSAGE_TOO_BIG"]
    assert result.restored == 5

    def fatal(*a: Any, **k: Any) -> int:
        raise SoapFaultError("mail.QUOTA_EXCEEDED", "quota")

    monkeypatch.setattr(client, "add_message", fatal)
    with pytest.raises(SoapFaultError):
        _restore(client, repo, alice)


def test_worker_runs_restore_jobs(
    tmp_path: Path,
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    settings: AgentSettings = _settings(tmp_path)
    job = {
        "job_id": "r-1",
        "kind": "restore",
        "attempt": 1,
        "lease_seconds": 30,
        "params": {
            "source_carbonio_id": alice.id,
            "snapshot_id": "s1",
            "mode": "new_folder",
            "label": LABEL,
            "dry_run": True,
        },
        "checkpoint": None,
        "carbonio_id": alice.id,
        "account_email": alice.name,
    }
    bad = {**job, "job_id": "r-2", "params": {**job["params"], "mode": "overwrite"}}
    api = FakeApi([job, bad])
    worker = Worker(
        AgentApi(settings, http=api.client()),
        client,
        AccountBackup(client, repo),
        AccountRestore(client, repo),
        sleep=lambda _: True,
    )
    assert worker.run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/r-1/restore-complete"
    assert body["dry_run"] is True and body["restored"] == 6 and body["unsupported"] == 1
    assert worker.run_once()
    assert api.calls[-1] == (
        "/jobs/r-2/fail",
        {"error": "unknown restore mode 'overwrite'", "retryable": False},
    )
    # Without a restore engine the job kind is refused.
    api = FakeApi([job])
    Worker(AgentApi(settings, http=api.client()), client, AccountBackup(client, repo)).run_once()
    assert api.calls[-1][0] == "/jobs/r-1/fail"


def test_restore_test_job_cleans_its_folder(
    tmp_path: Path,
    fake: FakeCarbonio,
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    snapshot: dict[int, bytes],
) -> None:
    probe = fake.add_mailbox("restore-test@example.test")
    messages = [i for i, item in alice.items.items() if item.kind == "m"][:2]
    job = {
        "job_id": "t-1",
        "kind": "restore",
        "attempt": 1,
        "lease_seconds": 30,
        "params": {
            "source_carbonio_id": alice.id,
            "snapshot_id": "s1",
            "mode": "new_folder",
            "label": "Test de restauration 2026-11-21 03.45",
            "item_ids": messages,
            "test": True,
            "cleanup": True,
        },
        "checkpoint": None,
        "carbonio_id": probe.id,
        "account_email": probe.name,
    }
    api = FakeApi([job])
    worker = Worker(
        AgentApi(_settings(tmp_path), http=api.client()),
        client,
        AccountBackup(client, repo),
        AccountRestore(client, repo),
        repo,
        sleep=lambda _: True,
    )
    assert worker.run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/t-1/restore-complete"
    assert body["restored"] == 2 and body["cleaned"] is True
    assert not any(f.name.startswith("Test de restauration") for f in probe.folders.values())
    assert not probe.items  # the restored copies went with the folder
