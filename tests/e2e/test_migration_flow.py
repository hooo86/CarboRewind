"""End-to-end migration: remote IMAP server (simulated, real sockets) → agent → Carbonio
(simulated), driven through the real API and PostgreSQL."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_imap import FakeImapServer  # noqa: E402
from test_backup_flow import Stack, engine  # noqa: E402, F401

DB_URL = os.environ.get("CARBOREWIND_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="CARBOREWIND_TEST_DATABASE_URL not set")


def eml(subject: str, mid: str) -> bytes:
    return (
        f"From: bob@old.test\r\nSubject: {subject}\r\nMessage-ID: <{mid}@old.test>\r\n"
        f"\r\nBody of {subject}\r\n"
    ).encode()


def test_copy_from_imap_then_delta(tmp_path: Path, engine: Engine) -> None:  # noqa: F811
    stack = Stack(tmp_path, engine)
    target = stack.carbonio.add_mailbox("ana@new.test")
    stack.worker.sync_inventory()
    with FakeImapServer() as imap:
        account = imap.account("ana@old.test", "pw-ana")
        account.box("INBOX").add(eml("Bonjour", "1"), ["\\Seen"])
        account.box("Sent", ["\\Sent"]).add(eml("Réponse", "2"), ["\\Seen"])
        account.box("Clients/Alpha").add(eml("Devis", "3"))

        source = stack.api.post(
            "/api/migrations/sources",
            json={"name": "ancien", "host": "127.0.0.1", "port": imap.port, "security": "none"},
            headers=stack.csrf,
        ).json()
        batch = stack.api.post(
            "/api/migrations/batches",
            json={"name": "lot 1", "source_id": source["id"]},
            headers=stack.csrf,
        ).json()["id"]
        report = stack.api.post(
            f"/api/migrations/batches/{batch}/import?target_domain=new.test",
            content=b"source_email;password\nana@old.test;pw-ana\n",
            headers=stack.csrf,
        ).json()
        assert report["matched"] == 1 and not report["errors"]

        # Connection test, then the copy.
        for dry_run in (True, False):
            r = stack.api.post(
                f"/api/migrations/batches/{batch}/start",
                json={"dry_run": dry_run},
                headers=stack.csrf,
            )
            assert r.json()["queued"] == 1
            assert stack.worker.run_once()
        (box,) = stack.api.get(f"/api/migrations/batches/{batch}").json()["mailboxes"]
        assert box["status"] == "succeeded" and box["stats"]["plan"]["messages"] == 3
        assert box["stats"]["copied"] == 3
        folders = {f.name for f in target.folders.values()}
        assert {"Inbox", "Sent", "Clients", "Alpha"} <= folders
        assert len([i for i in target.items.values() if i.kind == "m"]) == 3

        # Final delta before the MX switch: only the new message.
        account.boxes["INBOX"].add(eml("Dernier", "4"))
        stack.api.post(f"/api/migrations/batches/{batch}/start", json={}, headers=stack.csrf)
        assert stack.worker.run_once()
        (box,) = stack.api.get(f"/api/migrations/batches/{batch}").json()["mailboxes"]
        assert box["stats"]["copied"] == 1 and box["stats"]["total_copied"] == 4
        assert len([i for i in target.items.values() if i.kind == "m"]) == 4
        assert not [c for c in imap.commands if c.split(" ")[0] in {"STORE", "APPEND"}]
