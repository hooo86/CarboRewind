"""7-bit downgrade of 8-bit messages: ASCII out, same content for the reader."""

from __future__ import annotations

import email
import email.policy
from email.header import decode_header, make_header
from pathlib import Path

import pytest

from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.carbonio.mime7bit import to_7bit

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings

LATIN = (
    "From: Hélène Badi <h.badi@lnhc.dz>\r\n"
    "To: Réda <reda@example.test>, rmq@lnhc.dz\r\n"
    "Subject: Réunion à 10 h — ordre du jour\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: text/plain; charset="windows-1252"\r\n'
    "Content-Transfer-Encoding: 8bit\r\n\r\n"
    "Bonjour, voici l'ordre du jour : élection, trésorerie… À bientôt.\r\n"
).encode("windows-1252")

MULTI = (
    b"From: a@example.test\r\nTo: b@example.test\r\nSubject: =?utf-8?q?D=C3=A9j=C3=A0?=\r\n"
    b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="x"\r\n\r\n--x\r\n'
    b"Content-Type: text/plain; charset=iso-8859-1\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
    + "Café crème\r\n".encode("iso-8859-1")
    + b"--x\r\nContent-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    b"JVBERi0x\r\n--x--\r\n"
)


def _text(value: str) -> str:
    return str(make_header(decode_header(value)))


def test_single_part_latin_message() -> None:
    out = to_7bit(LATIN)
    assert out is not None and out.isascii()
    msg = email.message_from_string(out, policy=email.policy.default)
    assert str(msg["Subject"]) == "Réunion à 10 h — ordre du jour"
    assert msg["From"].addresses[0].display_name == "Hélène Badi"
    assert msg["From"].addresses[0].addr_spec == "h.badi@lnhc.dz"
    assert [a.addr_spec for a in msg["To"].addresses] == ["reda@example.test", "rmq@lnhc.dz"]
    assert msg["Content-Transfer-Encoding"] in ("base64", "quoted-printable")
    assert msg.get_content().startswith("Bonjour, voici l'ordre du jour : élection, trésorerie…")


def test_multipart_keeps_other_parts() -> None:
    out = to_7bit(MULTI)
    assert out is not None and out.isascii()
    msg = email.message_from_string(out, policy=email.policy.default)
    assert _text(msg["Subject"]) == "Déjà"
    text, pdf = list(msg.iter_parts())
    assert text.get_content() == "Café crème\n" or text.get_content().startswith("Café crème")
    assert pdf.get_payload(decode=True) == b"%PDF-1"


def test_ascii_message_is_unchanged_in_substance() -> None:
    raw = b"From: a@example.test\r\nSubject: hi\r\n\r\nhello\r\n"
    out = to_7bit(raw)
    assert out is not None and "hello" in out and "base64" not in out


@pytest.fixture
def fake() -> FakeCarbonio:
    return FakeCarbonio()


def test_add_message_falls_back_when_the_upload_is_refused(
    tmp_path: Path, fake: FakeCarbonio
) -> None:
    from carborewind_agent.carbonio.factory import build_client

    client: CarbonioClient = build_client(_settings(tmp_path), transport=fake.transport())
    box = fake.add_mailbox("rmq@lnhc.dz")
    fake.upload_reply = "401,'null'"
    item_id = client.add_message(box.id, 2, LATIN, flags="s")
    stored = box.items[item_id].raw
    assert stored.isascii()
    msg = email.message_from_bytes(stored, policy=email.policy.default)
    assert str(msg["Subject"]) == "Réunion à 10 h — ordre du jour"
    # When the servlet works, the message is stored byte for byte.
    fake.upload_reply = None
    item_id = client.add_message(box.id, 2, LATIN)
    assert box.items[item_id].raw == LATIN
