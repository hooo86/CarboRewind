"""IMAP migration: parsing helpers, then real imaplib sessions against a fake IMAP server
writing into the fake Carbonio."""

from __future__ import annotations

import datetime as dt
import ssl
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from carborewind_agent.carbonio import CarbonioClient, SoapFaultError
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.imap import (
    ImapError,
    ImapSettings,
    ImapSource,
    RemoteFolder,
    decode_mutf7,
    parse_fetch,
    parse_list,
    parse_namespace,
)
from carborewind_agent.migrate import (
    MailboxMigration,
    MigrationError,
    excluded,
    message_id,
    target_path,
    zimbra_flags,
)

from .fake_carbonio import FakeCarbonio, Mailbox
from .fake_imap import FakeImapServer
from .test_carbonio_client import _settings

PASSWORD = "imap-secret"


def eml(subject: str, mid: str | None = None) -> bytes:
    head = f"From: Bob <bob@old.test>\r\nTo: ana@old.test\r\nSubject: {subject}\r\n"
    if mid:
        head += f"Message-ID: <{mid}@old.test>\r\n"
    return (head + "\r\nBonjour\r\n").encode()


def folder(name: str, delim: str | None = "/", *flags: str) -> RemoteFolder:
    return RemoteFolder(name, name, delim, frozenset(f.lower() for f in flags))


# ------------------------------------------------------------------ helpers


def test_modified_utf7() -> None:
    assert decode_mutf7("&AMk-l&AOk-ments envoy&AOk-s") == "Éléments envoyés"
    assert decode_mutf7("Tom &- Jerry") == "Tom & Jerry"
    assert decode_mutf7("plain") == "plain"
    assert decode_mutf7("broken&AMk") == "broken&AMk"


def test_parse_list_and_namespace() -> None:
    data: list[Any] = [
        b'(\\HasNoChildren \\Sent) "/" "Sent Items"',
        b'(\\Noselect \\HasChildren) "/" "[Gmail]"',
        (b'(\\HasNoChildren) "." {14}', b'INBOX.Projets"'),
        b"() NIL INBOX",
        b"garbage",
        None,
    ]
    folders = parse_list(data)
    assert [f.name for f in folders] == ["Sent Items", "[Gmail]", 'INBOX.Projets"', "INBOX"]
    assert folders[0].flags == {"\\hasnochildren", "\\sent"}
    assert not folders[1].selectable and folders[2].delimiter == "."
    assert folders[3].delimiter is None
    assert parse_namespace([b'(("INBOX." ".")) NIL NIL']) == "INBOX."
    assert parse_namespace([b'(("" "/")) NIL NIL']) == ""
    assert parse_namespace([b"NIL NIL NIL"]) == "" and parse_namespace([]) == ""


def test_parse_fetch_flags_before_or_after_the_body() -> None:
    raw = b"Subject: x\r\n\r\nbody"
    head = (
        b'1 (UID 7 FLAGS (\\Seen $Forwarded) INTERNALDATE "15-Jan-2026 09:30:00 +0000" BODY[] {18}'
    )
    before = [(head, raw), b")"]
    after = [(b"2 (UID 8 BODY[] {18}", raw), b" FLAGS (\\Flagged))"]
    (m1,) = parse_fetch(before)
    (m2,) = parse_fetch(after)
    assert m1.uid == 7 and m1.flags == {"\\seen", "$forwarded"} and m1.raw == raw
    assert m1.internal_ms == int(dt.datetime(2026, 1, 15, 9, 30, tzinfo=dt.UTC).timestamp() * 1000)
    assert m2.uid == 8 and m2.flags == {"\\flagged"} and m2.internal_ms is None
    assert parse_fetch([(b"3 (BODY[] {1}", b"x"), b")"]) == []


def test_target_path() -> None:
    assert target_path(folder("INBOX")) == "/Inbox"
    assert target_path(folder("INBOX/Clients")) == "/Inbox/Clients"
    assert target_path(folder("Sent Items")) == "/Sent"
    assert target_path(folder("[Gmail]/Envoyés", "/", "\\Sent")) == "/Sent"
    assert target_path(folder("Éléments envoyés")) == "/Sent"
    assert target_path(folder("Corbeille/Vieux")) == "/Trash/Vieux"
    assert target_path(folder("Spam")) == "/Junk"
    assert target_path(folder("INBOX.Projets.2026", "."), "INBOX.") == "/Projets/2026"
    assert target_path(folder("INBOX.Drafts", "."), "INBOX.") == "/Drafts"
    assert target_path(folder("INBOX", "."), "INBOX.") == "/Inbox"
    assert target_path(folder("a/b", ".")) == "/a_b"
    assert target_path(folder("Projets"), root="Ancien serveur") == "/Ancien serveur/Projets"
    assert target_path(folder("")) == "/Inbox"


def test_flags_message_id_and_exclusions() -> None:
    assert zimbra_flags(frozenset(), False) == "u"
    seen = frozenset({"\\seen", "\\flagged", "\\answered", "$forwarded"})
    assert zimbra_flags(seen, True) == "frws"
    assert zimbra_flags(frozenset({"\\seen", "\\draft"}), False) == "d"
    assert message_id(eml("x", "abc")) == "<abc@old.test>"
    assert message_id(b"Subject: none\n\nbody") is None
    assert message_id(b"Message-ID:\r\n <folded@x>\r\n\r\n") == "<folded@x>"
    trash = folder("Corbeille")
    assert excluded(trash, "/Trash", ["Trash"])
    assert excluded(trash, "/Trash", ["corbeille"])
    assert excluded(folder("Archives/2020"), "/Archives/2020", ["Archives"])
    assert not excluded(folder("Archives2"), "/Archives2", ["Archives", " "])


# ------------------------------------------------------------------ against servers


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def ana(fake: FakeCarbonio) -> Mailbox:
    return fake.add_mailbox("ana@new.test")


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    return build_client(_settings(tmp_path), transport=fake.transport())


@pytest.fixture
def server() -> Iterator[FakeImapServer]:
    with FakeImapServer(master=("admin", "master-secret")) as srv:
        acc = srv.account("ana@old.test", PASSWORD)
        inbox = acc.box("INBOX")
        inbox.add(eml("Bienvenue", "m1"), ["\\Seen"])
        inbox.add(eml("Facture", "m2"), ["\\Flagged"])
        acc.box("Sent Items", ["\\Sent"]).add(eml("Réponse", "m3"), ["\\Seen"])
        acc.box("Projets").add(eml("Projet A", "m4"), ["\\Seen"])
        acc.box("Projets/2026").add(eml("Projet B", "m5"), ["\\Seen", "\\Answered"])
        acc.box("&AMk-l&AOk-ments supprim&AOk-s").add(eml("Vieux", "m6"), ["\\Seen"])
        acc.box("[Gmail]", ["\\Noselect"])
        acc.box("[Gmail]/All Mail", ["\\All"]).add(eml("Doublon", "m1"))
        yield srv


def settings_for(srv: FakeImapServer, **kw: Any) -> ImapSettings:
    base: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": srv.port,
        "security": "none",
        "verify_tls": True,
        "username": "ana@old.test",
        "password": PASSWORD,
        "timeout": 10,
    }
    return ImapSettings.from_job({**base, **kw})


def messages(fake: FakeCarbonio, box: Mailbox, path: str) -> list[tuple[bytes, str]]:
    f = fake.folder_by_path(box, path)
    assert f is not None, path
    return sorted(
        (item.raw.split(b"Subject: ")[1].split(b"\r\n")[0], item.flags)
        for item in box.items.values()
        if item.folder == f.id
    )


def test_full_copy_then_delta(
    server: FakeImapServer, client: CarbonioClient, fake: FakeCarbonio, ana: Mailbox
) -> None:
    engine = MailboxMigration(client, checkpoint_every=1)
    saved: list[dict[str, Any]] = []
    imap = settings_for(server)
    assert "secret" not in repr(imap)
    result = engine.run(lambda: ImapSource(imap), ana.id, checkpoint=saved.append)
    assert result.copied == 6 and not result.failed and not result.cancelled
    assert messages(fake, ana, "/Inbox") == [(b"Bienvenue", ""), (b"Facture", "uf")]
    assert messages(fake, ana, "/Sent") == [("Réponse".encode(), "s")]
    assert messages(fake, ana, "/Projets/2026") == [(b"Projet B", "r")]
    assert messages(fake, ana, "/Trash") == [(b"Vieux", "")]
    assert fake.folder_by_path(ana, "/[Gmail]") is None  # virtual "All Mail" skipped
    assert "/Projets/2026" in result.folders_created
    assert saved and saved[-1]["progress"]["copied"] == 6
    state = result.state
    assert state["folders"]["INBOX"] == {"uidvalidity": 1000, "last_uid": 2, "failed": []}
    payload = result.to_payload()
    assert payload["copied"] == 6 and payload["folders"][0]["source"] == "INBOX"
    # Nothing new: nothing copied, the source is never written to.
    again = engine.run(lambda: ImapSource(imap), ana.id, state=state)
    assert again.copied == 0 and again.duplicates == 0
    assert not [c for c in server.commands if c.split(" ")[0] in {"STORE", "APPEND", "EXPUNGE"}]
    assert any(c.startswith('EXAMINE "INBOX"') for c in server.commands)
    # One new message: only it is fetched.
    server.accounts["ana@old.test"].boxes["INBOX"].add(eml("Nouveau", "m7"))
    delta = engine.run(lambda: ImapSource(imap), ana.id, state=again.state)
    assert delta.copied == 1
    assert (b"Nouveau", "u") in messages(fake, ana, "/Inbox")


def test_uidvalidity_change_deduplicates_by_message_id(
    server: FakeImapServer, client: CarbonioClient, fake: FakeCarbonio, ana: Mailbox
) -> None:
    engine = MailboxMigration(client)
    imap = settings_for(server)
    first = engine.run(lambda: ImapSource(imap), ana.id)
    inbox = server.accounts["ana@old.test"].boxes["INBOX"]
    inbox.uidvalidity = 2000  # the server renumbered the folder
    inbox.add(eml("Sans identifiant"))
    second = engine.run(lambda: ImapSource(imap), ana.id, state=first.state)
    assert second.duplicates == 2 and second.copied == 1
    assert "SearchRequest" in fake.calls
    assert len(messages(fake, ana, "/Inbox")) == 3


def test_master_user_options_and_dry_run(
    server: FakeImapServer, client: CarbonioClient, fake: FakeCarbonio, ana: Mailbox
) -> None:
    engine = MailboxMigration(client)
    master = settings_for(
        server, username="admin", password="master-secret", auth="plain", authzid="ana@old.test"
    )
    plan = engine.run(lambda: ImapSource(master), ana.id, dry_run=True)
    assert plan.dry_run and plan.copied == 0 and not ana.items
    assert {f.source: f.new for f in plan.folders}["INBOX"] == 2
    assert server.logins[-1] == ("admin", "ana@old.test")
    options = {
        "root": "Ancien serveur",
        "exclude_folders": ["Trash", "Projets"],
        "since": "2026-01-01",
        "before": "2027-01-01",
    }
    server.accounts["ana@old.test"].boxes["INBOX"].add(
        eml("Trop vieux"), date=dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    )
    result = engine.run(lambda: ImapSource(master), ana.id, options=options)
    assert result.copied == 3
    assert messages(fake, ana, "/Ancien serveur/Inbox") == [(b"Bienvenue", ""), (b"Facture", "uf")]
    assert fake.folder_by_path(ana, "/Ancien serveur/Projets") is None
    for bad in ({"root": "a/b"}, {"root": "x" * 200}):
        with pytest.raises(MigrationError):
            engine.run(lambda: ImapSource(master), ana.id, options=bad)


def test_bad_credentials_and_unreachable_server(
    server: FakeImapServer, client: CarbonioClient, ana: Mailbox
) -> None:
    engine = MailboxMigration(client)
    with pytest.raises(ImapError) as err:
        engine.run(lambda: ImapSource(settings_for(server, password="wrong")), ana.id)
    assert not err.value.retryable and "authentication failed" in str(err.value)
    assert "wrong" not in str(err.value)
    with pytest.raises(ImapError) as err:
        engine.run(lambda: ImapSource(settings_for(server, auth="plain", password="wrong")), ana.id)
    assert not err.value.retryable
    closed = FakeImapServer()
    port = closed.port
    closed._server.server_close()
    with pytest.raises(ImapError) as err:
        engine.run(lambda: ImapSource(settings_for(server, port=port)), ana.id)
    assert err.value.retryable


def test_failed_messages_are_retried_and_fatal_faults_stop(
    server: FakeImapServer,
    client: CarbonioClient,
    fake: FakeCarbonio,
    ana: Mailbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = client.add_message
    broken = {"code": "mail.MESSAGE_TOO_BIG"}

    def flaky(account_id: str, folder_id: int, raw: bytes, **kw: Any) -> int:
        if b"Facture" in raw and broken["code"]:
            raise SoapFaultError(broken["code"], "nope")
        return real(account_id, folder_id, raw, **kw)

    monkeypatch.setattr(client, "add_message", flaky)
    engine = MailboxMigration(client)
    imap = settings_for(server)
    first = engine.run(lambda: ImapSource(imap), ana.id)
    assert first.copied == 5 and first.failed == {"INBOX:2": "mail.MESSAGE_TOO_BIG"}
    assert first.state["folders"]["INBOX"]["failed"] == [2]
    broken["code"] = ""
    second = engine.run(lambda: ImapSource(imap), ana.id, state=first.state)
    assert second.copied == 1 and second.state["folders"]["INBOX"]["failed"] == []
    broken["code"] = "mail.QUOTA_EXCEEDED"
    server.accounts["ana@old.test"].boxes["INBOX"].add(eml("Facture 2"))
    with pytest.raises(SoapFaultError):
        engine.run(lambda: ImapSource(imap), ana.id, state=second.state)


def test_resume_and_cancel(
    server: FakeImapServer, client: CarbonioClient, fake: FakeCarbonio, ana: Mailbox
) -> None:
    engine = MailboxMigration(client, checkpoint_every=1)
    imap = settings_for(server)
    saved: list[dict[str, Any]] = []
    calls = {"n": 0}

    def stop_after_two() -> bool:
        calls["n"] += 1
        return len(saved) >= 2

    cancelled = engine.run(
        lambda: ImapSource(imap), ana.id, checkpoint=saved.append, should_stop=stop_after_two
    )
    assert cancelled.cancelled and cancelled.copied == 2
    resumed = engine.run(lambda: ImapSource(imap), ana.id, resume=saved[-1])
    assert resumed.copied == 6 and not resumed.cancelled
    assert len(ana.items) == 6  # nothing copied twice


def test_courier_layout_and_literals(
    client: CarbonioClient, fake: FakeCarbonio, ana: Mailbox
) -> None:
    courier = {"delimiter": ".", "prefix": "INBOX.", "flags_after_body": True}
    with FakeImapServer(**courier, literal_names=True) as srv:  # type: ignore[arg-type]
        acc = srv.account("ana@old.test", PASSWORD)
        acc.box("INBOX").add(eml("Un"), ["\\Seen", "\\Deleted"])
        acc.box("INBOX").add(eml("Deux"), ["\\Seen"])
        acc.box("INBOX.Sent").add(eml("Envoi"), ["\\Seen"])
        acc.box("INBOX.Clients.Alpha").add(eml("Alpha"), [])
        result = MailboxMigration(client).run(lambda: ImapSource(settings_for(srv)), ana.id)
    assert result.copied == 3  # the \\Deleted message is not copied
    assert messages(fake, ana, "/Sent") == [(b"Envoi", "s")]
    assert messages(fake, ana, "/Clients/Alpha") == [(b"Alpha", "u")]


def _tls_context(tmp_path: Path) -> ssl.SSLContext:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "imap.old.test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "srv.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "srv.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "srv.crt", tmp_path / "srv.key")
    return context


def test_tls_verification(tmp_path: Path, client: CarbonioClient, ana: Mailbox) -> None:
    with FakeImapServer(tls=_tls_context(tmp_path)) as srv:
        srv.account("ana@old.test", PASSWORD).box("INBOX").add(eml("Chiffré"))
        engine = MailboxMigration(client)
        with pytest.raises(ImapError) as err:
            engine.run(lambda: ImapSource(settings_for(srv, security="ssl")), ana.id)
        assert not err.value.retryable and "certificate" in str(err.value)
        ok = engine.run(
            lambda: ImapSource(settings_for(srv, security="ssl", verify_tls=False)), ana.id
        )
        assert ok.copied == 1


def test_worker_runs_migration_jobs(
    server: FakeImapServer,
    client: CarbonioClient,
    fake: FakeCarbonio,
    ana: Mailbox,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from carborewind_agent.api_client import AgentApi
    from carborewind_agent.backup import AccountBackup
    from carborewind_agent.repository import Repository, generate_key
    from carborewind_agent.worker import Worker

    from .test_worker import FakeApi

    imap = {
        "host": "127.0.0.1",
        "port": server.port,
        "security": "none",
        "verify_tls": True,
        "username": "ana@old.test",
        "password": PASSWORD,
    }
    base = {"kind": "migrate", "attempt": 1, "lease_seconds": 300, "checkpoint": None}
    target = {"carbonio_id": ana.id, "account_email": ana.name}
    jobs = [
        {**base, **target, "job_id": "j-1", "migration": {"imap": imap, "options": {}}},
        {**base, **target, "job_id": "j-2", "migration": {}},
        {
            **base,
            **target,
            "job_id": "j-3",
            "migration": {"imap": {**imap, "password": "wrong"}},
        },
    ]
    fake_api = FakeApi(jobs)
    repo = Repository.init(tmp_path / "repo", generate_key())
    worker = Worker(
        AgentApi(_settings(tmp_path), http=fake_api.client()),
        client,
        AccountBackup(client, repo),
        repo=repo,
        migration=MailboxMigration(client),
    )
    caplog.set_level("DEBUG")
    assert worker.run_once() and worker.run_once() and worker.run_once()
    done = [b for p, b in fake_api.calls if p == "/jobs/j-1/migrate-complete"]
    assert done and done[0]["copied"] == 6 and done[0]["state"]["folders"]["INBOX"]
    fails = {p: b for p, b in fake_api.calls if p.endswith("/fail")}
    assert fails["/jobs/j-2/fail"]["retryable"] is False
    assert fails["/jobs/j-3/fail"]["retryable"] is False
    assert "authentication failed" in fails["/jobs/j-3/fail"]["error"]
    assert PASSWORD not in caplog.text and "wrong" not in str(fails)
    # Without a migration engine the kind is refused.
    fake_api.jobs.append({**base, **target, "job_id": "j-4", "migration": {}})
    bare = Worker(
        AgentApi(_settings(tmp_path), http=fake_api.client()), client, AccountBackup(client, repo)
    )
    assert bare.run_once()
    assert fake_api.calls[-1][0] == "/jobs/j-4/fail"
