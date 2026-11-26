"""Standby server: clone a Carbonio, change the main one, replay its backups, compare."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.backup import AccountBackup
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.restore import KEPT_FLAGS
from carborewind_agent.standby import StandbyError, StandbySync

from .fake_carbonio import FakeCarbonio, Folder, Mailbox
from .test_backup import eml
from .test_carbonio_client import _settings

VCARD = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bob Martin\r\nEND:VCARD\r\n"
VCARD2 = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Chloe Durand\r\nEND:VCARD\r\n"
ICS = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Point\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"


def _client(tmp_path: Path, fake: FakeCarbonio, name: str) -> CarbonioClient:
    from carborewind_agent.carbonio.factory import build_client

    (tmp_path / name).mkdir()
    return build_client(_settings(tmp_path / name), transport=fake.transport())


@pytest.fixture
def main() -> FakeCarbonio:
    fake = FakeCarbonio()
    box = fake.add_mailbox("alice@example.test")
    box.folders[260] = Folder(260, "Projets", 2, "message")
    box.folders[261] = Folder(261, "2026", 260, "message")
    box.add("m", 2, eml("Facture 1"), flags="ua", tags="Important", date=1_700_000_000_000)
    box.add("m", 2, eml("Facture 2"))
    box.add("m", 260, eml("Projet A"))
    box.add("m", 261, eml("Projet B"))
    box.add("m", 5, eml("Réponse"), flags="s")
    box.add("cn", 7, VCARD)
    box.add("appt", 10, ICS)
    return fake


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository.init(tmp_path / "repo", generate_key())


class Env:
    def __init__(self, tmp_path: Path, main: FakeCarbonio, repo: Repository) -> None:
        self.main, self.repo = main, repo
        self.main_client = _client(tmp_path, main, "main")
        self.engine = AccountBackup(self.main_client, repo)
        self.box = next(iter(main.mailboxes.values()))
        self.seq = 0
        self.token: str | None = None
        self.snap = ""
        self.backup()
        # The standby: a clone of the VM, taken right after the first backup.
        self.standby = FakeCarbonio()
        self.standby.mailboxes = copy.deepcopy(main.mailboxes)
        self.standby_client = _client(tmp_path, self.standby, "standby")
        self.sync = StandbySync(self.standby_client, repo, state_dir=tmp_path / "state")

    def backup(self, box: Mailbox | None = None) -> str:
        box = box or self.box
        self.seq += 1
        snap = f"s{self.seq}"
        previous = (self.snap, self.token) if self.token and box is self.box else None
        result = self.engine.run(box.id, box.name, snap, self.seq, previous)
        if box is self.box:
            self.snap, self.token = snap, result.token
        return snap

    def replicate(self, box: Mailbox | None = None, snap: str | None = None) -> Any:
        box = box or self.box
        return self.sync.run(box.id, box.name, snap or self.snap, attrs=dict(box.attrs))


def _path(box: Mailbox, folder_id: int) -> str:
    parts = []
    while folder_id != 1:
        f = box.folders[folder_id]
        parts.append(f.name)
        folder_id = f.parent or 1
    return "/" + "/".join(reversed(parts))


def content(box: Mailbox) -> set[tuple[str, str, bytes, str]]:
    kinds = {"m": "m", "message": "m", "cn": "cn", "contact": "cn", "appt": "appt"}
    out = set()
    for item in box.items.values():
        kind = kinds.get(item.kind, item.kind)
        flags = "".join(sorted(c for c in item.flags if c in KEPT_FLAGS)) if kind == "m" else ""
        out.add((kind, _path(box, item.folder), item.raw, flags))
    return out


def folders(box: Mailbox) -> set[str]:
    return {_path(box, f.id) for f in box.folders.values() if f.id != 1}


@pytest.fixture
def env(tmp_path: Path, main: FakeCarbonio, repo: Repository) -> Env:
    return Env(tmp_path, main, repo)


def test_clone_follows_the_main_server(env: Env) -> None:
    box = env.box
    ids = sorted(i for i, it in box.items.items() if it.kind == "m")
    # Changes on the main server after the clone.
    box.add("m", 2, eml("Nouveau"), flags="f")
    box.delete(ids[1])  # Facture 2
    box.modify(ids[2], folder=2)  # Projet A back to the Inbox
    box.modify(ids[0], flags="")  # Facture 1 read
    box.folders[270] = Folder(270, "Clients", 2, "message")
    box.add("m", 270, eml("Client X"))
    box.delete(ids[3])
    del box.folders[261]  # folder 2026 deleted with its message
    box.add("cn", 7, VCARD2)
    env.backup()
    standby = env.standby.mailboxes[box.id]
    result = env.replicate()
    assert content(standby) == content(box)
    assert folders(standby) == folders(box)
    assert result.account_created is False
    assert result.added == 2 and result.moved == 1 and result.folders_deleted == 1
    assert result.deleted == 2  # Facture 2 (adopted then gone) and Projet B
    assert result.reimported_folders == 2  # first sync: contacts and calendar rebuilt
    assert not result.failed

    # Nothing changed: nothing written.
    calls = len(env.standby.calls)
    again = env.replicate()
    assert (again.added, again.deleted, again.moved, again.updated) == (0, 0, 0, 0)
    assert again.reimported_folders == 0
    assert not [c for c in env.standby.calls[calls:] if c.startswith(("item-action", "empty"))]

    # Later changes come from the state file (no adoption).
    new = box.add("m", 270, eml("Client Y"), flags="u")
    box.modify(new.id, flags="uf")
    box.delete(ids[0])
    env.backup()
    later = env.replicate()
    assert content(standby) == content(box)
    assert (later.added, later.deleted) == (1, 1)


def test_new_and_recreated_accounts(env: Env) -> None:
    bob = env.main.add_mailbox("bob@example.test", displayName="Bob")
    bob.add("m", 2, eml("Bonjour Bob"), flags="u")
    bob.add("cn", 7, VCARD)
    snap = env.backup(bob)
    result = env.replicate(bob, snap)
    assert result.account_created is True
    copy_ = env.standby.mailboxes[bob.id]  # same id kept
    assert copy_.name == "bob@example.test" and copy_.attrs["displayName"] == "Bob"
    assert content(copy_) == content(bob)

    # Recreated on the main server since the clone: the standby's copy is replaced.
    stale = env.standby.add_mailbox("carol@example.test")
    stale.add("m", 2, eml("Ancien"))
    carol = env.main.add_mailbox("carol@example.test")
    carol.add("m", 2, eml("Nouveau"))
    snap = env.backup(carol)
    result = env.replicate(carol, snap)
    assert result.account_created is False and result.deleted == 1
    assert content(stale) == content(carol)


def test_invalid_account_id(env: Env) -> None:
    with pytest.raises(StandbyError):
        env.sync.run("../x", "x@example.test", "s1")


def test_worker_runs_standby_jobs(tmp_path: Path, env: Env) -> None:
    from carborewind_agent.api_client import AgentApi
    from carborewind_agent.worker import Worker

    from .test_worker import FakeApi

    env.box.add("m", 2, eml("Après le clone"))
    env.backup()
    job = {
        "job_id": "sb-1",
        "kind": "standby_sync",
        "attempt": 1,
        "lease_seconds": 30,
        "params": {"snapshot_id": env.snap, "attrs": {}},
        "carbonio_id": env.box.id,
        "account_email": env.box.name,
    }
    bad = {**job, "job_id": "sb-2", "carbonio_id": "../x"}
    api = FakeApi([job, bad])
    worker = Worker(
        AgentApi(_settings(tmp_path / "standby"), http=api.client()),
        env.standby_client,
        AccountBackup(env.standby_client, env.repo),
        standby=env.sync,
        sleep=lambda _: True,
    )
    assert worker.run_once()
    path, body = api.calls[-1]
    assert path == "/jobs/sb-1/standby-complete"
    assert body["added"] == 1 and body["snapshot_id"] == env.snap and body["failed_count"] == 0
    assert content(env.standby.mailboxes[env.box.id]) == content(env.box)
    assert worker.run_once()
    assert api.calls[-1][0] == "/jobs/sb-2/fail"
    assert api.calls[-1][1]["retryable"] is False
