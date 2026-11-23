"""TLS certificate: expiry read on the ports, renewal asked and applied by root."""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from carborewind_agent import cert_apply
from carborewind_agent.certcheck import (
    RETRY_AFTER,
    CertWatch,
    check,
    describe,
    fetch_der,
    valid_name,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def make_cert(name: str, days: float, *, sans: list[str] | None = None) -> tuple[bytes, bytes]:
    """(certificate PEM, key PEM) valid ``days`` more days after NOW."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Let's Encrypt"),
            x509.NameAttribute(NameOID.COMMON_NAME, "R11"),
        ]
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=60))
        .not_valid_after(NOW + timedelta(days=days))
    )
    if sans is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False
        )
    cert = builder.sign(key, hashes.SHA256())
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def der(pem: bytes) -> bytes:
    return x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)


def test_valid_names() -> None:
    assert valid_name("mail.lnhc.dz") and valid_name("MAIL.LNHC.DZ ")
    for bad in ("", "localhost", "mail.lnhc.dz; rm -rf /", "-x.dz", "a..b.dz", "x" * 300):
        assert not valid_name(bad)


def test_describe_expiry_names_and_issuer() -> None:
    pem, _ = make_cert("mail.lnhc.dz", 5.5, sans=["mail.lnhc.dz", "webmail.lnhc.dz"])
    info = describe(der(pem), "mail.lnhc.dz", NOW)
    assert info["days_left"] == 5.5 and info["matches"] is True
    assert info["names"] == ["mail.lnhc.dz", "webmail.lnhc.dz"]
    assert info["issuer"] == "Let's Encrypt R11" and info["self_signed"] is False
    assert describe(der(pem), "autre.lnhc.dz", NOW)["matches"] is False
    wildcard, _ = make_cert("x", 30, sans=["*.lnhc.dz"])
    assert describe(der(wildcard), "mail.lnhc.dz", NOW)["matches"] is True
    no_san, _ = make_cert("mail.lnhc.dz", -2)
    expired = describe(der(no_san), "mail.lnhc.dz", NOW)
    assert expired["days_left"] == -2.0 and expired["matches"] is True


def test_check_reports_each_port() -> None:
    pem, _ = make_cert("mail.lnhc.dz", 20, sans=["mail.lnhc.dz"])

    def fetch(host: str, port: int, name: str, starttls: bool) -> bytes:
        if port == 587:
            raise ConnectionRefusedError("refused")
        return der(pem)

    out = check("mail.lnhc.dz", fetch=fetch, now=NOW)
    assert [c["port"] for c in out] == [443, 993, 25, 587]
    assert out[0]["days_left"] == 20.0 and out[2]["service"] == "smtp"
    assert out[3]["error"] == "refused"


def test_fetch_der_reads_a_real_tls_server(tmp_path: Path) -> None:
    pem, key = make_cert("mail.lnhc.dz", 3, sans=["mail.lnhc.dz"])
    (tmp_path / "c.pem").write_bytes(pem)
    (tmp_path / "k.pem").write_bytes(key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def serve() -> None:
        conn, _ = server.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert fetch_der("127.0.0.1", port, "mail.lnhc.dz", False) == der(pem)
    finally:
        server.close()
        thread.join(timeout=5)


# ------------------------------------------------------------------ renewal decision


def _watch(tmp_path: Path, days: float, **cfg: Any) -> CertWatch:
    pem, _ = make_cert("mail.lnhc.dz", days, sans=["mail.lnhc.dz"])
    watch = CertWatch(
        cert_dir=tmp_path / "cert",
        checker=lambda name: [{"port": 443, **describe(der(pem), name, NOW)}],
    )
    watch.configure({"cert_name": "mail.lnhc.dz", **cfg})
    return watch


def test_renewal_asked_once_when_due(tmp_path: Path) -> None:
    watch = _watch(tmp_path, 6, cert_renew_enabled=True, cert_renew_days=7)
    watch.evaluate(1000.0)
    request = json.loads((tmp_path / "cert" / "request.json").read_text())
    assert request["action"] == "renew" and request["name"] == "mail.lnhc.dz"
    assert request["reason"] == "auto" and len(request["id"]) == 32
    (tmp_path / "cert" / "request.json").unlink()
    watch.evaluate(1000.0 + 3600)  # within RETRY_AFTER: not again
    assert not (tmp_path / "cert" / "request.json").exists()
    # root's answer is read back
    (tmp_path / "cert" / "status.json").write_text(
        json.dumps({"id": request["id"], "ok": True, "renewed": True, "not_after": "x"})
    )
    watch.evaluate(1000.0 + 3601)
    report = watch.report()
    assert report["last_result"]["renewed"] is True and report["days_left"] == 6.0
    watch.evaluate(1000.0 + RETRY_AFTER + 1)
    assert (tmp_path / "cert" / "request.json").exists()


def test_no_renewal_when_off_far_or_unnamed(tmp_path: Path) -> None:
    off = _watch(tmp_path, 3, cert_renew_enabled=False)
    off.evaluate(1.0)
    far = _watch(tmp_path, 40, cert_renew_enabled=True, cert_renew_days=7)
    far.evaluate(1.0)
    assert not (tmp_path / "cert" / "request.json").exists()
    bad = CertWatch(cert_dir=tmp_path / "cert")
    bad.configure({"cert_name": "x; reboot"})
    assert bad.name == "" and bad.renew(1.0) is False
    assert "no valid server name" in (bad.note or "")
    bad.evaluate(1.0)
    assert bad.report()["certificates"] == []
    default = CertWatch()
    default.configure({}, default_name="Mail.Lnhc.Dz")
    assert default.name == "mail.lnhc.dz" and default.alert_days == 7


def test_manual_renewal_keeps_the_request_id(tmp_path: Path) -> None:
    watch = _watch(tmp_path, 50)
    assert watch.renew(5.0, "manual", "c" * 32)
    request = json.loads((tmp_path / "cert" / "request.json").read_text())
    assert request["id"] == "c" * 32 and request["reason"] == "manual"


# ------------------------------------------------------------------ root helper


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    import pwd

    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(cert_apply.pwd, "getpwnam", lambda _name: me)
    monkeypatch.setattr(cert_apply.os, "chown", lambda *_a: None)
    paths = {
        "cert": tmp_path / "cert",
        "live": tmp_path / "live",
        "commercial": tmp_path / "commercial",
        "certbot": tmp_path / "certbot",
        "root": tmp_path / "isrg.pem",
    }
    for key in ("cert", "live", "commercial"):
        paths[key].mkdir()
    paths["certbot"].write_text("#!/bin/sh\n")
    paths["root"].write_text("ROOT\n")
    monkeypatch.setattr(cert_apply, "CERTBOTS", ((paths["certbot"], True),))
    monkeypatch.setattr(cert_apply, "LIVE_DIRS", (paths["live"],))
    monkeypatch.setattr(cert_apply, "COMMERCIAL", paths["commercial"])
    monkeypatch.setattr(cert_apply, "STAGING", paths["commercial"] / "carborewind")
    monkeypatch.setattr(cert_apply, "ISRG_ROOT", paths["root"])
    return paths


def _ask(path: Path, request: dict[str, Any]) -> None:
    (path / "request.json").write_text(json.dumps(request))


def _live(paths: dict[str, Path], days: float) -> None:
    pem, key = make_cert("mail.lnhc.dz", days)
    live = paths["live"] / "mail.lnhc.dz"
    live.mkdir(exist_ok=True)
    (live / "cert.pem").write_bytes(pem)
    (live / "chain.pem").write_text("CHAIN\n")
    (live / "privkey.pem").write_bytes(key)


def test_cert_apply_renews_and_deploys(root: dict[str, Path]) -> None:
    old, _ = make_cert("mail.lnhc.dz", 5)
    (root["commercial"] / "commercial.crt").write_bytes(old)
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "renew" in cmd:
            _live(root, 90)  # certbot wrote a new certificate
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    _ask(root["cert"], {"id": "a" * 32, "action": "renew", "name": "Mail.Lnhc.Dz"})
    status = cert_apply.apply(root["cert"], run=run)
    assert status["ok"] is True and status["renewed"] is True, status
    assert calls[0] == [
        "/usr/sbin/runuser",
        "-u",
        "zextras",
        "--",
        str(root["certbot"]),
        "renew",
        "--cert-name",
        "mail.lnhc.dz",
        "--non-interactive",
    ]
    assert calls[1] == cert_apply.DEPLOY and calls[2:] == cert_apply.RESTART
    staged = root["commercial"] / "carborewind"
    assert (staged / "chain.pem").read_text() == "CHAIN\nROOT\n"
    assert (root["commercial"] / "commercial.key").read_bytes().startswith(b"-----BEGIN")
    saved = json.loads((root["cert"] / "status.json").read_text())
    assert saved["id"] == "a" * 32 and saved["not_after"].startswith("2027-02")


def test_cert_apply_not_due_and_refusals(root: dict[str, Path]) -> None:
    current, _ = make_cert("mail.lnhc.dz", 60)
    (root["commercial"] / "commercial.crt").write_bytes(current)
    _live(root, 60)

    def run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, "not due\n", "")

    _ask(root["cert"], {"id": "b" * 32, "action": "renew", "name": "mail.lnhc.dz"})
    status = cert_apply.apply(root["cert"], run=run)
    assert status["ok"] is True and status["renewed"] is False
    assert "not due" in status["error"]

    def never(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("must not run")

    _ask(root["cert"], {"id": "c" * 32, "action": "renew", "name": "x.dz; reboot"})
    assert cert_apply.apply(root["cert"], run=never)["error"] == "invalid server name"
    _ask(root["cert"], {"id": "../x", "action": "delete", "name": "mail.lnhc.dz"})
    refused = cert_apply.apply(root["cert"], run=never)
    assert refused["error"] == "unknown action" and refused["id"] is None

    def failing(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "Challenge failed")

    _ask(root["cert"], {"id": "d" * 32, "action": "renew", "name": "mail.lnhc.dz"})
    failed = cert_apply.apply(root["cert"], run=failing)
    assert failed["ok"] is False and "certbot renew failed" in failed["error"]
    assert "Challenge failed" in failed["output"]
    (root["cert"] / "request.json").symlink_to("/etc/passwd")
    assert "cannot open" in cert_apply.apply(root["cert"], run=never)["error"]


def test_monitor_reports_the_certificate_and_asks_renewals(tmp_path: Path) -> None:
    from carborewind_agent.servicewatch import ServiceMonitor

    class Api:
        def services(self, report: dict[str, Any]) -> dict[str, Any]:
            self.report = report
            return {"cert_renew": {"id": "e" * 32}}

    api = Api()
    monitor = ServiceMonitor(api, None, {})  # type: ignore[arg-type]
    monitor.cert = _watch(tmp_path, 30)
    monitor._extend = lambda report: report  # type: ignore[method-assign]
    report = ServiceMonitor._extend(monitor, {"services": []})
    assert report["certificate"]["days_left"] == 30.0
    monitor.cert_enabled = False
    assert ServiceMonitor._extend(monitor, {"services": []})["certificate"] is None
    import carborewind_agent.servicewatch as sw

    original = sw.collect
    sw.collect = lambda *_a, **_kw: {"services": []}  # type: ignore[assignment]
    try:
        monitor.tick()
    finally:
        sw.collect = original
    request = json.loads((tmp_path / "cert" / "request.json").read_text())
    assert request["id"] == "e" * 32 and request["reason"] == "manual"
