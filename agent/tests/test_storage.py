"""Repository storage: SMB share preparation, status, local fallback."""

from __future__ import annotations

import os
import pwd
from pathlib import Path
from typing import Any

import pytest

from carborewind_agent import storage
from carborewind_agent.__main__ import main
from carborewind_agent.repository import Repository, generate_key
from carborewind_agent.storage import (
    SmbRequest,
    StorageError,
    check_writable,
    configure_local,
    configure_smb,
    copy_repository,
    describe,
    mount_info,
    set_env,
    systemd_escape_path,
)

ME = os.getuid()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake root environment: /etc, systemd dir, mount point and agent user under tmp."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(storage.shutil, "which", lambda name: f"/sbin/{name}")
    user = pwd.struct_passwd(("carborewind", "x", ME, os.getgid(), "", "/", "/bin/false"))
    monkeypatch.setattr(storage.pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(storage, "MOUNT_ROOT", str(tmp_path) + "/")
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "# comment\nCARBOREWIND_AGENT_API_URL=https://api\n"
        "CARBOREWIND_AGENT_REPOSITORY_PATH=/var/lib/carborewind/repository\n"
    )
    os.chmod(env_file, 0o600)
    calls: list[tuple[str, ...]] = []
    return {
        "tmp": tmp_path,
        "etc": tmp_path / "etc",
        "systemd": tmp_path / "systemd",
        "mount": tmp_path / "nas",
        "env_file": env_file,
        "calls": calls,
        "run": lambda *cmd: calls.append(cmd),
    }


def smb(env: dict[str, Any], current: Path, **kw: Any) -> dict[str, object]:
    fields: dict[str, Any] = {
        "share": "//nas.lnhc.dz/backups/carborewind",
        "username": "svc-backup",
        "password": "p@ss word",
        "domain": "LNHC",
        "mount_point": env["mount"],
        **kw,
    }
    return configure_smb(
        SmbRequest(**fields),
        env["env_file"],
        current,
        etc=env["etc"],
        systemd=env["systemd"],
        run=env["run"],
    )


def test_escape_like_systemd() -> None:
    assert systemd_escape_path(Path("/var/lib/carborewind/nas")) == "var-lib-carborewind-nas"
    assert systemd_escape_path(Path("/mnt/my-nas.1")) == "mnt-my\\x2dnas.1"
    assert systemd_escape_path(Path("/")) == "-"
    assert systemd_escape_path(Path("/.hidden")) == "\\x2ehidden"


def test_mount_info_and_describe(tmp_path: Path) -> None:
    table = tmp_path / "mountinfo"
    table.write_text(
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "40 22 0:50 / /var/lib/carborewind/nas rw - cifs //nas/backups rw,vers=3.0\n"
        "garbage line\n"
        "41 22 0:51 / /short rw - nfs\n"
    )
    info = mount_info(Path("/var/lib/carborewind/nas/repository"), table)
    assert info is not None and (info.fstype, info.source) == ("cifs", "//nas/backups")
    root = mount_info(Path("/etc"), table)
    assert root is not None and root.fstype == "ext4"
    assert mount_info(Path("/"), tmp_path / "missing") is None
    status = describe(tmp_path)
    assert status["path"] == str(tmp_path)
    assert isinstance(status["free_bytes"], int) and status["free_bytes"] > 0
    assert describe(tmp_path / "absent")["error"] == "unreachable"


def test_set_env_keeps_other_lines(tmp_path: Path) -> None:
    f = tmp_path / "agent.env"
    f.write_text("A=1\n#CARBOREWIND_AGENT_REPOSITORY_PATH=/old\nB=2\n")
    os.chmod(f, 0o600)
    set_env(f, "CARBOREWIND_AGENT_REPOSITORY_PATH", "/new")
    set_env(f, "C", "3")
    assert f.read_text() == "A=1\nCARBOREWIND_AGENT_REPOSITORY_PATH=/new\nB=2\nC=3\n"
    assert f.stat().st_mode & 0o777 == 0o600
    fresh = tmp_path / "new.env"
    set_env(fresh, "X", "1")
    assert fresh.read_text() == "X=1\n"


def test_smb_configuration(env: dict[str, Any]) -> None:
    done = smb(env, env["tmp"] / "no-repository-yet", seal=True)
    cred = env["etc"] / "smb.credentials"
    assert cred.read_text() == "username=svc-backup\npassword=p@ss word\ndomain=LNHC\n"
    assert cred.stat().st_mode & 0o777 == 0o600
    unit_name = str(done["unit"])
    assert unit_name.endswith("-nas.mount")
    unit = (env["systemd"] / unit_name).read_text()
    assert "What=//nas.lnhc.dz/backups/carborewind" in unit
    assert f"Where={env['mount']}" in unit
    assert f"credentials={cred}" in unit and f"uid={ME}" in unit
    assert "nosuid,nodev,noexec" in unit and "vers=3.0" in unit and ",seal" in unit
    assert "p@ss" not in unit  # the password only lives in the 0600 file
    dropin = env["systemd"] / "carborewind-agent.service.d" / "storage.conf"
    assert f"RequiresMountsFor={env['mount']}" in dropin.read_text()
    assert env["calls"] == [
        ("systemctl", "daemon-reload"),
        ("systemctl", "enable", "--now", unit_name),
        ("systemctl", "daemon-reload"),
    ]
    text = env["env_file"].read_text()
    assert f"CARBOREWIND_AGENT_REPOSITORY_PATH={env['mount']}/repository" in text
    assert "CARBOREWIND_AGENT_API_URL=https://api" in text
    assert done == {
        "unit": unit_name,
        "repository": f"{env['mount']}/repository",
        "copied_files": 0,
        "initialised": False,
    }


def test_existing_repository_is_migrated_or_left_explicitly(env: dict[str, Any]) -> None:
    key = generate_key()
    old = env["tmp"] / "old-repo"
    repo = Repository.init(old, key)
    blob = repo.put(b"precious mail").blob_id
    repo.put_manifest("acc", "s1", {"items": {}})
    with pytest.raises(StorageError, match="--migrate"):
        smb(env, old)
    done = smb(env, old, migrate=True)
    assert int(str(done["copied_files"])) >= 4 and done["initialised"] is True
    moved = Repository.open(env["mount"] / "repository", key)
    assert moved.get(blob) == b"precious mail"
    assert moved.get_manifest("acc", "s1") == {"items": {}}
    # Re-running copies nothing new; --start-empty is accepted too.
    assert copy_repository(old, env["mount"] / "repository") == 0
    assert smb(env, old, start_empty=True)["copied_files"] == 0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"share": "nas/backups"}, "//server/share"),
        ({"share": "//nas/back,ups"}, "//server/share"),
        ({"username": "a,b"}, "invalid username"),
        ({"domain": "x\ny"}, "invalid domain"),
        ({"password": ""}, "required"),
        ({"password": "a\nb"}, "required"),
        ({"mount_point": Path("/mnt/nas")}, "must be under"),
    ],
)
def test_smb_rejects_bad_input(env: dict[str, Any], change: dict[str, Any], message: str) -> None:
    with pytest.raises(StorageError, match=message):
        smb(env, env["tmp"] / "none", **change)
    assert not (env["etc"] / "smb.credentials").exists()


def test_smb_requires_root_cifs_and_user(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage.shutil, "which", lambda name: None)
    with pytest.raises(StorageError, match="cifs-utils"):
        smb(env, env["tmp"] / "none")
    monkeypatch.setattr(storage.shutil, "which", lambda name: "/sbin/x")

    def no_user(name: str) -> Any:
        raise KeyError(name)

    monkeypatch.setattr(storage.pwd, "getpwnam", no_user)
    with pytest.raises(StorageError, match="does not exist"):
        smb(env, env["tmp"] / "none")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(StorageError, match="root"):
        smb(env, env["tmp"] / "none")
    with pytest.raises(StorageError, match="root"):
        configure_local(Path("/x"), env["env_file"], systemd=env["systemd"], run=env["run"])


def test_write_check(tmp_path: Path) -> None:
    check_writable(tmp_path, ME, os.getgid())
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(StorageError, match="belong to uid"):
        check_writable(tmp_path, ME + 1, os.getgid())
    with pytest.raises(StorageError, match="not writable"):
        check_writable(tmp_path / "missing", ME, os.getgid())


def test_failing_systemctl_is_reported(env: dict[str, Any]) -> None:
    def fail(*cmd: str) -> None:
        raise StorageError("mount error(13): Permission denied")

    env["run"] = fail
    with pytest.raises(StorageError, match="Permission denied"):
        smb(env, env["tmp"] / "none")


def test_failed_mount_leaves_the_agent_startable(env: dict[str, Any]) -> None:
    """A share that cannot be mounted must not leave RequiresMountsFor behind."""
    calls: list[tuple[str, ...]] = []

    def mount_fails(*cmd: str) -> None:
        calls.append(cmd)
        if cmd[1] == "enable":
            raise StorageError("mount error(2): No such file or directory")

    env["run"] = mount_fails
    with pytest.raises(StorageError, match="mount error"):
        smb(env, env["tmp"] / "none")
    assert not (env["systemd"] / "carborewind-agent.service.d" / "storage.conf").exists()
    assert calls[-1][:3] == ("systemctl", "disable", "--now")
    assert "/nas" not in env["env_file"].read_text()  # repository not switched


def test_run_helper() -> None:
    storage._run("true")
    with pytest.raises(StorageError, match="false"):
        storage._run("false")


def test_back_to_local(env: dict[str, Any]) -> None:
    smb(env, env["tmp"] / "none")
    env["calls"].clear()
    configure_local(
        Path("/var/lib/carborewind/repository"),
        env["env_file"],
        systemd=env["systemd"],
        run=env["run"],
    )
    assert not (env["systemd"] / "carborewind-agent.service.d" / "storage.conf").exists()
    assert env["calls"] == [("systemctl", "daemon-reload")]
    assert "REPOSITORY_PATH=/var/lib/carborewind/repository" in env["env_file"].read_text()
    configure_local(Path("/data"), env["env_file"], systemd=env["systemd"], run=env["run"])
    assert len(env["calls"]) == 1  # no drop-in left: no reload


def test_cli(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pw = env["tmp"] / "pw"
    pw.write_text("secret\n")
    set_env(env["env_file"], "CARBOREWIND_AGENT_REPOSITORY_PATH", str(env["tmp"] / "none"))
    monkeypatch.setattr(storage, "_run", env["run"])
    monkeypatch.setattr(storage, "ETC", env["etc"])
    monkeypatch.setattr(storage, "SYSTEMD", env["systemd"])

    def patched(req: SmbRequest, env_file: Path, current: Path, **kw: Any) -> dict[str, object]:
        assert req.password == "secret" and req.username == "svc"
        return configure_smb(
            req, env_file, current, etc=env["etc"], systemd=env["systemd"], run=env["run"]
        )

    monkeypatch.setattr(storage, "configure_smb", patched)
    args = ["storage", "--env-file", str(env["env_file"])]
    code = main(
        [
            *args,
            "smb",
            "//nas/share",
            "--username",
            "svc",
            "--password-file",
            str(pw),
            "--mount-point",
            str(env["mount"]),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "repo init" in out
    assert main([*args, "smb", "bad", "--username", "svc", "--password-file", str(pw)]) == 1
    set_env(env["env_file"], "CARBOREWIND_AGENT_REPOSITORY_PATH", str(env["tmp"] / "none"))
    assert main([*args, "status"]) == 1  # the configured path does not exist
    set_env(env["env_file"], "CARBOREWIND_AGENT_REPOSITORY_PATH", f'"{env["tmp"]}"')
    assert main([*args, "status"]) == 0
    assert "free_bytes" in capsys.readouterr().out
    assert main([*args, "local", "/var/lib/carborewind/repository"]) == 0
    assert storage.read_env(env["tmp"] / "missing.env", "X", "dflt") == "dflt"
    assert storage.read_env(env["env_file"], "NOPE", "dflt") == "dflt"


def test_password_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["one", "one", "a", "b"])
    monkeypatch.setattr(storage.getpass, "getpass", lambda prompt: next(answers))
    assert storage.read_password(None) == "one"
    with pytest.raises(StorageError, match="match"):
        storage.read_password(None)
