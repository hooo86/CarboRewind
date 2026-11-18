import json
import os
import threading
from pathlib import Path

import pytest

from carborewind_agent.__main__ import main
from carborewind_agent.repository import (
    IntegrityError,
    Repository,
    RepositoryError,
    decode_key,
    encode_key,
    generate_key,
)
from carborewind_agent.repository.crypto import MAGIC
from carborewind_agent.repository.keyfile import KeyFileError, create_kek, read_kek

EML = b"From: alice@example.test\r\nSubject: Facture\r\n\r\n" + b"Bonjour " * 500


@pytest.fixture
def kek() -> bytes:
    return generate_key()


@pytest.fixture
def repo(tmp_path: Path, kek: bytes) -> Repository:
    return Repository.init(tmp_path / "repo", kek)


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def test_init_layout_and_permissions(repo: Repository) -> None:
    config = json.loads((repo.root / "config.json").read_text())
    assert config["format"] == 1
    assert config["repository_id"] == repo.repo_id
    key_file = repo.root / "keys" / "master.json"
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    assert oct((repo.root / "config.json").stat().st_mode & 0o777) == "0o600"
    assert oct((repo.root / "blobs").stat().st_mode & 0o777) == "0o700"


def test_init_refuses_non_empty_dir(tmp_path: Path, kek: bytes) -> None:
    (tmp_path / "x").write_text("data")
    with pytest.raises(RepositoryError, match="not empty"):
        Repository.init(tmp_path, kek)


def test_roundtrip_compression_and_encryption(repo: Repository) -> None:
    result = repo.put(EML)
    assert result.created
    assert result.size == len(EML)
    assert result.stored_size < len(EML)  # zstd compresses the repetitive body
    stored = (repo.root / "blobs" / result.blob_id[:2] / result.blob_id).read_bytes()
    assert stored.startswith(MAGIC)
    assert b"Facture" not in stored  # encrypted at rest
    assert repo.get(result.blob_id) == EML
    assert repo.has(result.blob_id)


def test_deduplication(repo: Repository) -> None:
    first = repo.put(EML)
    second = repo.put(EML)
    assert first.blob_id == second.blob_id
    assert not second.created
    assert len(list(repo.blob_ids())) == 1


def test_blob_id_is_keyed(tmp_path: Path) -> None:
    """Two repositories do not share ids: the id does not leak a plain content hash."""
    a = Repository.init(tmp_path / "a", generate_key())
    b = Repository.init(tmp_path / "b", generate_key())
    assert a.put(EML).blob_id != b.put(EML).blob_id


def test_reopen_with_kek(repo: Repository, kek: bytes) -> None:
    blob = repo.put(EML).blob_id
    reopened = Repository.open(repo.root, kek)
    assert reopened.repo_id == repo.repo_id
    assert reopened.get(blob) == EML


def test_wrong_kek_is_rejected(repo: Repository) -> None:
    with pytest.raises(IntegrityError, match="wrong KEK"):
        Repository.open(repo.root, generate_key())


def test_open_non_repository(tmp_path: Path, kek: bytes) -> None:
    with pytest.raises(RepositoryError, match="not a CarboRewind repository"):
        Repository.open(tmp_path, kek)


def test_unsupported_format(repo: Repository, kek: bytes) -> None:
    config_path = repo.root / "config.json"
    config = json.loads(config_path.read_text())
    config["format"] = 99
    config_path.write_text(json.dumps(config))
    with pytest.raises(RepositoryError, match="unsupported"):
        Repository.open(repo.root, kek)


def test_bit_flip_is_detected(repo: Repository) -> None:
    blob = repo.put(EML).blob_id
    path = repo.root / "blobs" / blob[:2] / blob
    data = bytearray(path.read_bytes())
    data[-5] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(IntegrityError):
        repo.get(blob)


def test_swapped_blob_is_detected(repo: Repository) -> None:
    """Copying a valid blob over another one fails: the AAD binds ciphertext to its id."""
    a = repo.put(b"message A").blob_id
    b = repo.put(b"message B").blob_id
    (repo.root / "blobs" / b[:2] / b).write_bytes((repo.root / "blobs" / a[:2] / a).read_bytes())
    with pytest.raises(IntegrityError):
        repo.get(b)


def test_truncated_object(repo: Repository) -> None:
    blob = repo.put(EML).blob_id
    (repo.root / "blobs" / blob[:2] / blob).write_bytes(MAGIC + b"short")
    with pytest.raises(IntegrityError, match="truncated"):
        repo.get(blob)


def test_missing_blob_and_invalid_id(repo: Repository) -> None:
    with pytest.raises(RepositoryError, match="not found"):
        repo.get("a" * 64)
    with pytest.raises(RepositoryError, match="invalid blob id"):
        repo.get("../../etc/passwd")


def test_delete_blob(repo: Repository) -> None:
    blob = repo.put(EML).blob_id
    repo.delete(blob)
    repo.delete(blob)  # idempotent
    assert not repo.has(blob)


def test_no_temporary_files_left(repo: Repository) -> None:
    for i in range(20):
        repo.put(f"message {i}".encode())
    assert not [p for p in _files(repo.root) if p.name.startswith(".tmp-")]


def test_concurrent_puts_of_same_content(repo: Repository) -> None:
    errors: list[Exception] = []

    def worker() -> None:
        try:
            for _ in range(20):
                repo.put(EML)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    (blob,) = repo.blob_ids()
    assert repo.get(blob) == EML


def test_manifest_roundtrip_and_binding(repo: Repository) -> None:
    manifest = {"seq": 3, "items": [{"id": 257, "blob": "ab" * 32}]}
    ref = repo.put_manifest("acc-1", "snap-3", manifest)
    assert ref == "manifests/acc-1/snap-3.json"
    assert repo.get_manifest("acc-1", "snap-3") == manifest
    # A manifest moved to another snapshot name fails authentication.
    repo.put_manifest("acc-1", "snap-4", {"seq": 4})
    src = repo.root / "manifests" / "acc-1" / "snap-3.json"
    dst = repo.root / "manifests" / "acc-1" / "snap-4.json"
    dst.write_bytes(src.read_bytes())
    with pytest.raises(IntegrityError):
        repo.get_manifest("acc-1", "snap-4")
    repo.delete_manifest("acc-1", "snap-3")
    with pytest.raises(RepositoryError, match="not found"):
        repo.get_manifest("acc-1", "snap-3")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 200])
def test_manifest_path_traversal_rejected(repo: Repository, bad: str) -> None:
    with pytest.raises(RepositoryError):
        repo.put_manifest(bad, "snap", {})
    with pytest.raises(RepositoryError):
        repo.put_manifest("acc", bad, {})


def test_verify_full_and_sampled(repo: Repository) -> None:
    ids = [repo.put(f"m{i}".encode()).blob_id for i in range(50)]
    assert repo.verify().ok
    assert repo.verify().checked == 50
    bad = ids[7]
    (repo.root / "blobs" / bad[:2] / bad).write_bytes(b"garbage")
    report = repo.verify()
    assert report.corrupted == [bad]
    sampled = repo.verify(sample=0.2, seed=1)
    assert 0 < sampled.checked < 50
    with pytest.raises(RepositoryError):
        repo.verify(sample=0)


def test_kek_rotation(repo: Repository, kek: bytes) -> None:
    blob = repo.put(EML).blob_id
    new = generate_key()
    Repository.rotate_kek(repo.root, kek, new)
    reopened = Repository.open(repo.root, new)
    assert reopened.get(blob) == EML
    with pytest.raises(IntegrityError):
        Repository.open(repo.root, kek)


def test_locks(repo: Repository) -> None:
    with repo.lock(), repo.lock():  # shared locks coexist
        pass
    with repo.lock(exclusive=True):
        pass


def test_key_encoding() -> None:
    key = generate_key()
    assert decode_key(encode_key(key)) == key
    with pytest.raises(ValueError, match="32 bytes"):
        decode_key(encode_key(b"short"))


# ------------------------------------------------------------------ key file & CLI


def test_keyfile_permissions(tmp_path: Path) -> None:
    path = tmp_path / "repo.key"
    create_kek(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(read_kek(path)) == 32
    with pytest.raises(FileExistsError):
        create_kek(path)
    os.chmod(path, 0o640)  # group-readable is already refused
    with pytest.raises(KeyFileError, match="group/others"):
        read_kek(path)
    with pytest.raises(KeyFileError, match="not found"):
        read_kek(tmp_path / "absent")
    bad = tmp_path / "bad.key"
    bad.write_text("not base64!!")
    os.chmod(bad, 0o600)
    with pytest.raises(KeyFileError, match="invalid"):
        read_kek(bad)


def test_cli_repo_lifecycle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key, new_key, repo_dir = tmp_path / "k", tmp_path / "k2", tmp_path / "repo"
    base = ["repo", "--path", str(repo_dir), "--key-file", str(key)]
    assert main([*base, "keygen"]) == 0
    assert main([*base, "keygen"]) == 1  # never overwrites
    assert main([*base, "init"]) == 0
    repo = Repository.open(repo_dir, read_kek(key))
    blob = repo.put(EML).blob_id
    assert main([*base, "verify"]) == 0
    assert "checked 1 blob(s), 0 corrupted" in capsys.readouterr().out

    (repo_dir / "blobs" / blob[:2] / blob).write_bytes(b"garbage")
    assert main([*base, "verify", "--sample", "1"]) == 3

    create_kek(new_key)
    assert main([*base, "rotate-key", str(new_key)]) == 0
    assert main(["repo", "--path", str(repo_dir), "--key-file", str(new_key), "verify"]) == 3
    assert main([*base, "verify"]) == 1  # old key no longer opens it


def test_cli_repo_uses_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARBOREWIND_AGENT_REPOSITORY_PATH", str(tmp_path / "r"))
    monkeypatch.setenv("CARBOREWIND_AGENT_REPOSITORY_KEY_FILE", str(tmp_path / "k"))
    assert main(["repo", "keygen"]) == 0
    assert main(["repo", "init"]) == 0
    assert main(["repo", "verify"]) == 0
    assert "checked 0 blob(s)" in capsys.readouterr().out
