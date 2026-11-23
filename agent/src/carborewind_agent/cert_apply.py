"""Renew the Let's Encrypt certificate of the mail server (root, ``carborewind-cert.service``).

The agent writes ``/var/lib/carborewind/cert/request.json`` (see :mod:`.certcheck`); the
``carborewind-cert.path`` unit then runs ``carborewind-agent cert apply`` as root, which:

1. reads the request defensively and accepts only ``{"action": "renew", "name": <host>}``,
   the name matching a strict host name pattern; it is passed as one argument, no shell;
2. runs ``certbot renew --cert-name <name>`` (Carbonio's certbot as zextras when present,
   else the system certbot): certbot renews only a certificate due for renewal (less than
   30 days left) and keeps its own way of proving the domain (webroot, standalone…);
3. when certbot produced a newer certificate, deploys it into Carbonio (private key into
   ``commercial.key``, then ``zmcertmgr deploycrt comm`` with the chain and the ISRG root)
   and restarts the proxy and the MTA, so that HTTPS, IMAP and SMTP present it;
4. writes ``status.json`` (root:carborewind 0640): outcome, new end of validity, the end
   of the output.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509

from .certcheck import CERT_DIR, valid_name
from .storage import AGENT_USER, StorageError
from .storage_apply import read_request

ZEXTRAS = "zextras"
CERTBOTS = (  # (path, run as zextras)
    (Path("/opt/zextras/libexec/certbot"), True),
    (Path("/opt/zextras/common/bin/certbot"), True),
    (Path("/usr/bin/certbot"), False),
)
LIVE_DIRS = (
    Path("/opt/zextras/common/certbot/etc/letsencrypt/live"),
    Path("/etc/letsencrypt/live"),
)
COMMERCIAL = Path("/opt/zextras/ssl/carbonio/commercial")
STAGING = COMMERCIAL / "carborewind"
ISRG_ROOT = Path("/etc/ssl/certs/ISRG_Root_X1.pem")
# Fixed commands (no data from the request in them).
DEPLOY = [
    "/usr/bin/su",
    "-",
    ZEXTRAS,
    "-c",
    f"zmcertmgr deploycrt comm {STAGING}/cert.pem {STAGING}/chain.pem",
]
RESTART = [
    ["/usr/bin/su", "-", ZEXTRAS, "-c", "zmproxyctl restart"],
    ["/usr/bin/su", "-", ZEXTRAS, "-c", "zmmtactl restart"],
]
TIMEOUT = 10 * 60
OUTPUT_TAIL = 3000
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _write_status(path: Path, status: dict[str, Any], gid: int) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status), encoding="utf-8")
    os.chown(tmp, 0, gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def not_after(path: Path) -> datetime | None:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes()).not_valid_after_utc
    except (OSError, ValueError):
        return None


def _certbot(name: str) -> list[str]:
    for path, as_zextras in CERTBOTS:
        if path.is_file():
            cmd = [str(path), "renew", "--cert-name", name, "--non-interactive"]
            if as_zextras:
                return ["/usr/sbin/runuser", "-u", ZEXTRAS, "--", *cmd]
            return cmd
    raise FileNotFoundError("certbot not found (Carbonio's or the system's)")


def _live(name: str) -> Path | None:
    for base in LIVE_DIRS:
        if (base / name / "cert.pem").is_file():
            return base / name
    return None


def _stage(live: Path) -> None:
    """Key, certificate and chain where zmcertmgr reads them (owned by zextras)."""
    z = pwd.getpwnam(ZEXTRAS)
    STAGING.mkdir(parents=True, exist_ok=True)
    os.chown(STAGING, z.pw_uid, z.pw_gid)
    shutil.copyfile(live / "cert.pem", STAGING / "cert.pem")
    chain = (live / "chain.pem").read_bytes()
    if ISRG_ROOT.is_file():  # zmcertmgr wants the whole chain up to the root
        chain = chain.rstrip(b"\n") + b"\n" + ISRG_ROOT.read_bytes()
    (STAGING / "chain.pem").write_bytes(chain)
    key = COMMERCIAL / "commercial.key"
    shutil.copyfile(live / "privkey.pem", key)
    for path in (STAGING / "cert.pem", STAGING / "chain.pem", key):
        os.chown(path, z.pw_uid, z.pw_gid)
        os.chmod(path, 0o640)


def apply(cert_dir: Path = CERT_DIR, run: Any = subprocess.run) -> dict[str, Any]:
    user = pwd.getpwnam(AGENT_USER)
    status: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "ok": False, "renewed": False}
    try:
        request = read_request(cert_dir / "request.json", user.pw_uid)
    except StorageError as exc:
        status["error"] = str(exc)
        _write_status(cert_dir / "status.json", status, user.pw_gid)
        return status
    request_id = request.get("id")
    status["id"] = (
        request_id if isinstance(request_id, str) and REQUEST_ID.fullmatch(request_id) else None
    )
    name = str(request.get("name") or "").strip().lower()
    output: list[str] = []

    def step(cmd: list[str]) -> int:
        done = run(cmd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
        output.append(f"$ {' '.join(cmd[-3:])}\n{done.stdout or ''}{done.stderr or ''}")
        return int(done.returncode)

    try:
        if request.get("action") != "renew":
            raise ValueError("unknown action")
        if not valid_name(name):
            raise ValueError("invalid server name")
        current = not_after(COMMERCIAL / "commercial.crt")
        if step(_certbot(name)) != 0:
            raise RuntimeError("certbot renew failed")
        live = _live(name)
        if live is None:
            raise RuntimeError(f"no certbot certificate named {name}")
        fresh = not_after(live / "cert.pem")
        status["not_after"] = fresh.isoformat() if fresh else None
        if fresh is None or (current is not None and fresh <= current):
            # Not due yet (certbot renews in the last 30 days), or already deployed.
            status["ok"] = True
            status["error"] = "certbot did not produce a newer certificate (not due yet?)"
        else:
            _stage(live)
            if step(DEPLOY) != 0:
                raise RuntimeError("zmcertmgr deploycrt failed")
            for cmd in RESTART:
                step(cmd)
            status["ok"] = status["renewed"] = True
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError) as exc:
        status["error"] = str(exc)[:500]
    status["output"] = "\n".join(output)[-OUTPUT_TAIL:]
    _write_status(cert_dir / "status.json", status, user.pw_gid)
    return status


def apply_command() -> int:
    if os.geteuid() != 0:
        print("cert apply must run as root (carborewind-cert.service)")
        return 2
    status = apply()
    print(json.dumps({k: v for k, v in status.items() if k != "output"}))
    return 0 if status.get("ok") else 1
