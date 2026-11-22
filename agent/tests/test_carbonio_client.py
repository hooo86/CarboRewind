"""Contract tests of the Carbonio client against the in-memory simulator."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from carborewind_agent.__main__ import main
from carborewind_agent.carbonio import MUST_RESYNC, CarbonioClient, SoapClient
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.carbonio.probe import run_probe
from carborewind_agent.carbonio.soap import SoapFaultError, content
from carborewind_agent.config import AgentSettings
from carborewind_agent.repository.keyfile import KeyFileError

from .fake_carbonio import ADMIN_PASSWORD, FakeCarbonio, Mailbox

EML = b"From: bob@example.test\r\nSubject: Hello\r\n\r\nBody\r\n"
LATIN1 = "From: bob@example.test\r\nSubject: Caf\xe9\r\n\r\nD\xe9j\xe0\r\n".encode("latin-1")


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


@pytest.fixture
def alice(fake: FakeCarbonio) -> Mailbox:
    box = fake.add_mailbox("alice@example.test", zimbraCOSId="default")
    box.add("m", 2, EML, flags="u", tags="Important", date=1_700_000_000_000)
    box.add("m", 5, LATIN1, flags="s")
    box.add("cn", 7, b"BEGIN:VCARD\r\nFN:Bob\r\nEND:VCARD\r\n")
    return box


def _settings(tmp_path: Path, password: str = ADMIN_PASSWORD) -> AgentSettings:
    secret = tmp_path / "admin.password"
    secret.write_text(password + "\n")
    os.chmod(secret, 0o600)
    return AgentSettings(
        api_url="https://api.example.test",  # type: ignore[arg-type]
        carbonio_admin_name="carborewind@example.test",
        carbonio_admin_password_file=secret,
    )


@pytest.fixture
def client(tmp_path: Path, fake: FakeCarbonio) -> CarbonioClient:
    return build_client(_settings(tmp_path), transport=fake.transport())


def test_accounts_paging_and_domain_filter(
    client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox
) -> None:
    for i in range(7):
        fake.add_mailbox(f"user{i}@other.test")
    names = [a.name for a in client.iter_accounts(page=3)]
    assert len(names) == 8
    assert names == sorted(names)
    only = [a.name for a in client.iter_accounts(domain="example.test", page=3)]
    assert only == ["alice@example.test"]
    info = client.get_account(alice.id)
    assert info.attrs["zimbraCOSId"] == ["default"]
    assert info.status == "active"
    # The admin token is reused across calls.
    assert fake.calls.count("AuthRequest") == 1


def test_wrong_admin_password(tmp_path: Path, fake: FakeCarbonio) -> None:
    client = build_client(_settings(tmp_path, "nope"), transport=fake.transport())
    with pytest.raises(SoapFaultError) as exc:
        list(client.iter_accounts())
    assert exc.value.code == "account.AUTH_FAILED"


def test_delegation_is_cached_and_unknown_account_fails(
    client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox
) -> None:
    assert client.delegate(alice.id) == client.delegate(alice.id)
    assert fake.calls.count("DelegateAuthRequest") == 1
    with pytest.raises(SoapFaultError, match="NO_SUCH_ACCOUNT"):
        client.delegate("does-not-exist")


def test_initial_sync_lists_folders_and_items(client: CarbonioClient, alice: Mailbox) -> None:
    delta = client.sync(alice.id, None)
    paths = {f.path: f for f in delta.folders}
    assert {"/Inbox", "/Sent", "/Contacts", "/Calendar"} <= set(paths)
    assert paths["/Contacts"].view == "contact"
    kinds = sorted((kind, folder) for kind, _, folder, _ in delta.changed)
    assert kinds == [("contact", 7), ("message", 2), ("message", 5)]
    assert delta.token == str(alice.seq)


def test_nested_folder_paths(client: CarbonioClient, alice: Mailbox) -> None:
    sub = client.create_folder(alice.id, 2, "Factures")
    subsub = client.create_folder(alice.id, sub, "2026")
    assert client.create_folder(alice.id, 2, "Factures") == sub  # fie=1: idempotent
    paths = {f.id: f.path for f in client.sync(alice.id, None).folders}
    assert paths[subsub] == "/Inbox/Factures/2026"


def test_incremental_sync_changes_deletions_and_paging(
    client: CarbonioClient, alice: Mailbox
) -> None:
    token = client.sync(alice.id, None).token
    first, second, third = sorted(alice.items)
    alice.modify(first, flags="f")  # flagged
    alice.delete(second)
    new = alice.add("m", 2, b"new message")
    page1 = client.sync(alice.id, token, change_limit=1)
    assert page1.more
    assert [iid for _, iid, _, _ in page1.changed] == [first]
    assert page1.changed[0][3]["f"] == "f"
    page2 = client.sync(alice.id, page1.token, change_limit=10)
    assert not page2.more
    assert [iid for _, iid, _, _ in page2.changed] == [new.id]
    assert page2.deleted == [second]
    assert third in alice.items
    # Nothing new since the last token.
    empty = client.sync(alice.id, page2.token)
    assert empty.changed == [] and empty.deleted == []


def test_expired_token_requires_resync(client: CarbonioClient, alice: Mailbox) -> None:
    token = client.sync(alice.id, None).token
    alice.delete(next(iter(alice.items)))
    alice.purge_tombstones()
    with pytest.raises(SoapFaultError) as exc:
        client.sync(alice.id, token)
    assert exc.value.code == MUST_RESYNC


def test_fetch_raw_and_meta(client: CarbonioClient, alice: Mailbox) -> None:
    first = sorted(alice.items)[0]
    assert client.fetch_raw(alice.name, alice.id, first) == EML
    meta = client.get_item_meta(alice.id, first)
    assert meta["tn"] == "Important"
    assert meta["l"] == "2"
    with pytest.raises(SoapFaultError, match="NO_SUCH_ITEM"):
        client.fetch_raw(alice.name, alice.id, 99_999)
    with pytest.raises(SoapFaultError, match="NO_SUCH_ITEM"):
        client.get_item_meta(alice.id, 99_999)


def test_add_message_inline_and_uploaded(
    client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox
) -> None:
    folder = client.create_folder(alice.id, 1, "Restored-2026-11-18")
    utf8_id = client.add_message(
        alice.id, folder, EML, flags="u", tags="Important", received_ms=1_700_000_000_000
    )
    latin_id = client.add_message(alice.id, folder, LATIN1)
    assert alice.items[utf8_id].raw == EML
    assert alice.items[utf8_id].tags == "Important"
    assert alice.items[utf8_id].date == 1_700_000_000_000
    # 8-bit content went through the upload servlet byte-for-byte.
    assert alice.items[latin_id].raw == LATIN1
    assert any(c.startswith("upload:") for c in fake.calls)
    with pytest.raises(SoapFaultError, match="NO_SUCH_FOLDER"):
        client.add_message(alice.id, 4242, EML)


def test_add_message_too_large_inline_goes_by_upload(
    client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox
) -> None:
    fake.inline_max = 100
    small = b"Subject: s\r\n\r\nok\r\n"
    big = b"Subject: big\r\n\r\n" + b"x" * 500 + b"\r\n"
    big_id = client.add_message(alice.id, 2, big)
    assert alice.items[big_id].raw == big
    uploads = sum(c.startswith("upload:") for c in fake.calls)
    assert uploads == 1
    # The limit is remembered: the next big message goes straight to the upload.
    calls = len(fake.calls)
    client.add_message(alice.id, 2, big + b"y")
    assert sum(c.startswith("upload:") for c in fake.calls) == 2
    assert len(fake.calls) - calls <= 3  # no inline attempt first
    small_id = client.add_message(alice.id, 2, small)
    assert alice.items[small_id].raw == small
    assert sum(c.startswith("upload:") for c in fake.calls) == 2  # small stays inline
    # Upload refused and too big for inline: the error comes back.
    fake.upload_reply = "401,'null'"
    with pytest.raises(SoapFaultError):
        client.add_message(alice.id, 2, big + b"z")
    # An HTTP error of the upload servlet is a per-message fault too.
    fake.upload_reply = None
    fake.fail_next = [500]
    with pytest.raises(SoapFaultError, match="UPLOAD_FAILED"):
        client.add_message(alice.id, 2, big + b"w")


def test_rest_import_formats(client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox) -> None:
    client.import_rest(
        alice.name, alice.id, "/Calendar", "ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR"
    )
    assert "import:ics" in fake.calls
    with pytest.raises(ValueError, match="unsupported"):
        client.import_rest(alice.name, alice.id, "/Calendar", "exe", b"")


def test_transient_errors_are_retried(
    client: CarbonioClient, fake: FakeCarbonio, alice: Mailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    fake.fail_next = [503, 502]
    assert [a.name for a in client.iter_accounts()] == [alice.name]


def test_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    soap = SoapClient(
        httpx.Client(transport=httpx.MockTransport(boom)), "https://x/soap", retries=2
    )
    with pytest.raises(SoapFaultError) as exc:
        soap.invoke("Ping", "urn:zimbraAdmin")
    assert exc.value.code == "client.NETWORK"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, text="<html>oops</html>"), "client.BAD_RESPONSE"),
        (httpx.Response(200, json={"Body": {}}), "client.BAD_RESPONSE"),
        (httpx.Response(500, json={"Body": {"Fault": {}}}), "service.FAILURE"),
    ],
)
def test_malformed_answers(response: httpx.Response, code: str) -> None:
    soap = SoapClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: response)), "https://x/soap"
    )
    with pytest.raises(SoapFaultError) as exc:
        soap.invoke("Ping", "urn:zimbraAdmin")
    assert exc.value.code == code


def test_content_helper() -> None:
    assert content([{"_content": "a"}]) == "a"
    assert content({"_content": 5}) == "5"
    assert content([]) is None
    assert content("plain") == "plain"


def test_password_file_must_be_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_repository import _pretend_mode

    settings = _settings(tmp_path)
    _pretend_mode(monkeypatch, settings.carbonio_admin_password_file, 0o100640)
    with pytest.raises(KeyFileError, match="group/others"):
        build_client(settings)


# ------------------------------------------------------------------ probe


def test_probe_success(client: CarbonioClient, alice: Mailbox) -> None:
    report = run_probe(client, alice.name)
    assert report.ok, report.render()
    rendered = report.render()
    assert "✓ DelegateAuthRequest" in rendered
    assert f"{len(EML)} bytes" in rendered
    assert "Hello" not in rendered  # never prints mail content


def test_probe_reports_failures(tmp_path: Path, fake: FakeCarbonio, alice: Mailbox) -> None:
    bad = build_client(_settings(tmp_path, "nope"), transport=fake.transport())
    report = run_probe(bad)
    assert not report.ok
    assert "account.AUTH_FAILED" in report.render()
    unknown = run_probe(build_client(_settings(tmp_path), transport=fake.transport()), "x@y")
    assert not unknown.ok


def test_probe_without_messages(client: CarbonioClient, fake: FakeCarbonio) -> None:
    fake.add_mailbox("empty@example.test")
    report = run_probe(client, "empty@example.test")
    assert report.ok
    assert "skipped: no message" in report.render()


def test_cli_probe_configuration_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CARBOREWIND_AGENT_API_URL", raising=False)
    assert main(["carbonio", "probe"]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_probe_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeCarbonio,
    alice: Mailbox,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("CARBOREWIND_AGENT_API_URL", "https://api.example.test")
    monkeypatch.setenv(
        "CARBOREWIND_AGENT_CARBONIO_ADMIN_PASSWORD_FILE", str(settings.carbonio_admin_password_file)
    )
    monkeypatch.setattr(
        "carborewind_agent.carbonio.factory.build_client",
        lambda s: build_client(s, transport=fake.transport()),
    )
    assert main(["carbonio", "probe", "--account", alice.name]) == 0
    assert "✓ SyncRequest (initial, typed)" in capsys.readouterr().out


def test_non_json_answers_are_explained(tmp_path: Path) -> None:
    import httpx

    from carborewind_agent.carbonio.soap import SoapClient, SoapFaultError

    def page(status: int) -> SoapClient:
        transport = httpx.MockTransport(
            lambda r: httpx.Response(
                status, text="<html><body>Request Entity Too Large</body></html>"
            )
        )
        return SoapClient(httpx.Client(transport=transport), "https://x/service/soap", retries=0)

    with pytest.raises(SoapFaultError) as big:
        page(413).invoke("AddMsg", "urn:zimbraMail")
    assert big.value.code == "client.TOO_LARGE" and "HTTP 413" in str(big.value)
    with pytest.raises(SoapFaultError) as other:
        page(200).invoke("AddMsg", "urn:zimbraMail")
    assert other.value.code == "client.BAD_RESPONSE"
    assert "HTTP 200" in str(other.value) and "Request Entity Too Large" in str(other.value)
