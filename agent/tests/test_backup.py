"""Backup engine against the Carbonio simulator and a real (temporary) repository."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent.backup import AccountBackup, BackupResult, extract_metadata
from carborewind_agent.carbonio import CarbonioClient, SoapFaultError
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.throttle import Throttle

from .fake_carbonio import FakeCarbonio, Mailbox
from .test_carbonio_client import _settings


def eml(subject: str, body: str = "Bonjour") -> bytes:
    return (
        f"From: Bob <bob@example.test>\r\nTo: alice@example.test\r\nSubject: {subject}\r\n"
        f"Date: Tue, 17 Nov 2026 10:00:00 +0100\r\n\r\n{body}\r\n"
    ).encode()


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def alice(fake: FakeCarbonio) -> Mailbox:
    box = fake.add_mailbox("alice@example.test")
    box.add("m", 2, eml("Facture 1"), flags="u", tags="Important")
    box.add("m", 2, eml("Facture 2"))
    box.add("m", 5, eml("Réponse", "Merci"), flags="s")
    box.add("cn", 7, b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bob Martin\r\nEND:VCARD\r\n")
    box.add(
        "appt",
        10,
        b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Point\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    return box


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    from carborewind_agent.carbonio.factory import build_client

    return build_client(_settings(tmp_path), transport=fake.transport())


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository.init(tmp_path / "repo", generate_key())


def _full(engine: AccountBackup, box: Mailbox, snap: str = "s1", seq: int = 1) -> BackupResult:
    return engine.run(box.id, box.name, snap, seq)


def test_full_backup_stores_everything(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    result = _full(AccountBackup(client, repo), alice)
    assert result.backup_type == "full"
    assert result.item_count == 5
    assert not result.partial
    assert set(result.added) == set(alice.items)
    assert result.removed == []
    manifest = repo.get_manifest(alice.id, "s1")
    assert manifest["token"] == result.token == str(alice.seq)
    assert {f["path"] for f in manifest["folders"]} >= {"/Inbox", "/Sent", "/Contacts"}
    for item_id, entry in manifest["items"].items():
        assert repo.get(entry["blob"]) == alice.items[int(item_id)].raw
        meta = json.loads(repo.get(entry["meta_blob"]))
        assert meta["folder"] == alice.items[int(item_id)].folder
    first = manifest["items"][str(min(alice.items))]
    assert first["subject"] == "Facture 1"
    assert first["sender"] == "Bob <bob@example.test>"
    assert first["date"] == "2026-11-17T09:00:00+00:00"
    assert json.loads(repo.get(first["meta_blob"]))["tags"] == "Important"
    contact = next(e for e in manifest["items"].values() if e["kind"] == "contact")
    assert contact["subject"] == "Bob Martin"
    assert result.new_bytes > 0


def test_incremental_patches_previous_manifest(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    engine = AccountBackup(client, repo)
    first = _full(engine, alice)
    ids = sorted(alice.items)
    alice.modify(ids[0], flags="uf")  # flagged -> new metadata version
    alice.delete(ids[1])
    new = alice.add("m", 2, eml("Nouveau"))
    second = engine.run(alice.id, alice.name, "s2", 2, previous=("s1", first.token))

    assert second.backup_type == "incremental"
    assert set(second.added) == {ids[0], new.id}
    assert second.removed == [ids[0], ids[1]]
    assert second.item_count == 5

    # Point in time: snapshot 1 is untouched, snapshot 2 is complete on its own.
    s1 = repo.get_manifest(alice.id, "s1")["items"]
    s2 = repo.get_manifest(alice.id, "s2")["items"]
    assert str(ids[1]) in s1 and str(ids[1]) not in s2
    assert str(new.id) in s2 and str(new.id) not in s1
    assert s1[str(ids[2])] == s2[str(ids[2])]  # unchanged item: same blobs
    assert repo.get_manifest(alice.id, "s2")["folders"]  # folder tree carried over

    # Deduplication: the flag change re-stored only the small metadata blob.
    assert s1[str(ids[0])]["blob"] == s2[str(ids[0])]["blob"]
    assert s1[str(ids[0])]["meta_blob"] != s2[str(ids[0])]["meta_blob"]


def test_incremental_without_changes(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    engine = AccountBackup(client, repo)
    first = _full(engine, alice)
    second = engine.run(alice.id, alice.name, "s2", 2, previous=("s1", first.token))
    assert second.added == {} and second.removed == []
    assert second.new_bytes == 0
    assert second.item_count == first.item_count


def test_expired_token_falls_back_to_full(
    client: CarbonioClient, repo: Repository, alice: Mailbox
) -> None:
    engine = AccountBackup(client, repo)
    first = _full(engine, alice)
    gone = sorted(alice.items)[0]
    alice.delete(gone)
    alice.purge_tombstones()
    second = engine.run(alice.id, alice.name, "s2", 2, previous=("s1", first.token))
    assert second.backup_type == "full"
    assert second.removed == [gone]  # diff still computed against the previous snapshot
    assert second.added == {}
    assert str(gone) not in repo.get_manifest(alice.id, "s2")["items"]


def test_resume_after_interruption(
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for i in range(20):
        alice.add("m", 2, eml(f"Message {i}"))
    checkpoints: list[dict[str, Any]] = []
    calls = {"n": 0}
    real_fetch = client.fetch_raw

    def crashing_fetch(name: str, account_id: str, item_id: int) -> bytes:
        calls["n"] += 1
        if calls["n"] == 13:
            raise RuntimeError("agent killed")
        return real_fetch(name, account_id, item_id)

    monkeypatch.setattr(client, "fetch_raw", crashing_fetch)
    engine = AccountBackup(client, repo, checkpoint_every=5)
    with pytest.raises(RuntimeError, match="killed"):
        engine.run(alice.id, alice.name, "s1", 1, checkpoint=checkpoints.append)
    assert len(checkpoints) == 2  # after 5 and 10 items
    progress = checkpoints[-1]["progress"]
    assert progress["items"] == 10 and progress["total"] >= 10 and progress["bytes"] > 0
    state = json.loads(json.dumps(checkpoints[-1]))  # survives a JSON round-trip (API storage)

    calls["n"] = 100  # no more crash
    resumed = engine.run(alice.id, alice.name, "s1", 1, resume=state)
    assert resumed.item_count == 25
    assert calls["n"] - 100 == 25 - 10  # the 10 checkpointed items were not re-read
    manifest = repo.get_manifest(alice.id, "s1")
    for item_id, entry in manifest["items"].items():
        assert repo.get(entry["blob"]) == alice.items[int(item_id)].raw


def test_item_errors_make_a_partial_snapshot_keeping_previous_version(
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AccountBackup(client, repo)
    first = _full(engine, alice)
    target = sorted(alice.items)[0]
    alice.modify(target, flags="f")
    real_fetch = client.fetch_raw

    def failing(name: str, account_id: str, item_id: int) -> bytes:
        if item_id == target:
            raise SoapFaultError("service.TEMPORARILY_UNAVAILABLE", "busy")
        return real_fetch(name, account_id, item_id)

    monkeypatch.setattr(client, "fetch_raw", failing)
    second = engine.run(alice.id, alice.name, "s2", 2, previous=("s1", first.token))
    assert second.partial
    assert second.errors == {target: "service.TEMPORARILY_UNAVAILABLE"}
    s1 = repo.get_manifest(alice.id, "s1")["items"][str(target)]
    s2 = repo.get_manifest(alice.id, "s2")["items"][str(target)]
    assert s1 == s2  # previous version kept, not lost
    assert repo.get_manifest(alice.id, "s2")["errors"] == {
        str(target): "service.TEMPORARILY_UNAVAILABLE"
    }


def test_item_deleted_between_sync_and_read(
    client: CarbonioClient,
    repo: Repository,
    alice: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = sorted(alice.items)[0]
    real_meta = client.get_item_meta

    def vanishing(account_id: str, item_id: int) -> dict[str, Any]:
        if item_id == target:
            raise SoapFaultError("mail.NO_SUCH_ITEM", "gone")
        return real_meta(account_id, item_id)

    real_batch = client.messages_meta

    def batch_without_target(account_id: str, ids: list[int]) -> dict[int, dict[str, Any]]:
        return {k: v for k, v in real_batch(account_id, ids).items() if k != target}

    monkeypatch.setattr(client, "get_item_meta", vanishing)
    monkeypatch.setattr(client, "messages_meta", batch_without_target)
    result = _full(AccountBackup(client, repo), alice)
    assert not result.partial
    assert result.item_count == 4
    assert str(target) not in repo.get_manifest(alice.id, "s1")["items"]


def test_other_errors_propagate(
    client: CarbonioClient, repo: Repository, fake: FakeCarbonio
) -> None:
    with pytest.raises(SoapFaultError, match="NO_SUCH_ACCOUNT"):
        AccountBackup(client, repo).run("nope", "nope@example.test", "s1", 1)


def test_throttle_is_applied(client: CarbonioClient, repo: Repository, alice: Mailbox) -> None:
    consumed: list[int] = []

    class Recorder(Throttle):
        def consume(self, nbytes: int) -> None:
            consumed.append(nbytes)

    _full(AccountBackup(client, repo, Recorder()), alice)
    assert sum(consumed) == sum(len(i.raw) for i in alice.items.values())


# ------------------------------------------------------------------ metadata & throttle units


def test_extract_metadata_edge_cases() -> None:
    assert extract_metadata("message", b"\xff\xfe garbage", {"d": 0})["subject"] is None
    assert (
        extract_metadata("message", b"Subject: x\r\nDate: not a date\r\n\r\n", {})["date"] is None
    )
    assert extract_metadata("task", b"", {"d": 1_700_000_000_000})["date"] == (
        "2023-11-14T22:13:20+00:00"
    )


def test_token_bucket_limits_rate() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = Throttle(1000, clock=lambda: now[0], sleep=sleep)
    throttle.consume(500)  # within the initial burst
    assert slept == []
    throttle.consume(1500)  # 1000 bytes over budget -> 1 s
    assert slept == [pytest.approx(1.0)]
    now[0] += 10  # idle time refills the bucket, capped at one second of traffic
    throttle.consume(1000)
    assert len(slept) == 1
    assert throttle.waited == pytest.approx(1.0)


def test_unlimited_and_from_mbps() -> None:
    Throttle().consume(10**9)  # returns immediately
    assert Throttle.from_mbps(8)._rate == 1_000_000


def test_load_backoff() -> None:
    loads = iter([3.0, 2.5, 0.5])
    slept: list[float] = []
    throttle = Throttle(0, max_load=1.0, sleep=slept.append, loadavg=lambda: next(loads))
    throttle.consume(1)
    assert slept == [5.0, 5.0]
    stuck = Throttle(0, max_load=1.0, sleep=lambda _: None, loadavg=lambda: 9.0)
    stuck.consume(1)  # gives up waiting after max_wait instead of blocking forever
    assert stuck.waited == 300.0


def test_full_backup_reads_message_metadata_in_batches(
    client: CarbonioClient, repo: Repository, fake: FakeCarbonio
) -> None:
    box = fake.add_mailbox("many@example.test")
    for n in range(450):
        box.add("m", 2, eml(f"Message {n}"), flags="u" if n % 2 else "")
    fake.calls.clear()
    result = AccountBackup(client, repo).run(box.id, box.name, "s1", 1)
    assert result.item_count == 450
    assert fake.calls.count("GetMsgMetadataRequest") == 3  # 200 + 200 + 50
    assert fake.calls.count("GetItemRequest") == 0
    manifest = repo.get_manifest(box.id, "s1")
    flags = {json.loads(repo.get(e["meta_blob"]))["flags"] for e in manifest["items"].values()}
    assert flags == {"", "u"}


def test_backup_falls_back_when_batched_metadata_is_refused(
    client: CarbonioClient, repo: Repository, fake: FakeCarbonio, monkeypatch: pytest.MonkeyPatch
) -> None:
    from carborewind_agent.carbonio import SoapFaultError

    box = fake.add_mailbox("old@example.test")
    for n in range(3):
        box.add("m", 2, eml(f"Message {n}"))

    def refuse(*_: object) -> None:
        raise SoapFaultError("service.UNKNOWN_DOCUMENT", "unknown request")

    monkeypatch.setattr(client, "messages_meta", refuse)
    fake.calls.clear()
    result = AccountBackup(client, repo).run(box.id, box.name, "s1", 1)
    assert result.item_count == 3 and fake.calls.count("GetItemRequest") == 3
