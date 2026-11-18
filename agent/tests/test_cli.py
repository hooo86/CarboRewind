from pathlib import Path

import pytest

from carborewind_agent import __version__
from carborewind_agent.__main__ import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_config_rejects_missing_api_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CARBOREWIND_AGENT_API_URL", raising=False)
    assert main(["check-config"]) == 2
    assert "api_url" in capsys.readouterr().err


def test_check_config_reports_missing_tls_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARBOREWIND_AGENT_API_URL", "https://api.example.test")
    monkeypatch.setenv("CARBOREWIND_AGENT_TLS_CERT", str(tmp_path / "nope.crt"))
    assert main(["check-config"]) == 1
    assert "nope.crt" in capsys.readouterr().err


def test_check_config_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARBOREWIND_AGENT_API_URL", "https://api.example.test")
    for name in ("cert", "key", "ca"):
        path = tmp_path / f"{name}.pem"
        path.write_text("dummy")
        monkeypatch.setenv(f"CARBOREWIND_AGENT_TLS_{name.upper()}", str(path))
    assert main(["check-config"]) == 0
    assert "configuration OK" in capsys.readouterr().out


def test_concurrency_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from carborewind_agent.config import AgentSettings

    monkeypatch.setenv("CARBOREWIND_AGENT_API_URL", "https://api.example.test")
    monkeypatch.setenv("CARBOREWIND_AGENT_MAX_CONCURRENT_ACCOUNTS", "0")
    with pytest.raises(ValidationError):
        AgentSettings()  # type: ignore[call-arg]
