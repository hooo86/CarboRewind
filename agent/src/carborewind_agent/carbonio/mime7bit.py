"""Turn a message with raw 8-bit bytes into a 7-bit (ASCII) one, same content.

``AddMsgRequest`` takes the message as a JSON string, which cannot carry bytes that are not
UTF-8 (messages written in Windows-1252 / ISO-8859-x with ``8bit`` transfer encoding, typical
of Outlook). The byte-exact path is the upload servlet; when it is not usable, this
re-encodes such a message the way a standard MTA would downgrade it (RFC 6152):

* body parts holding 8-bit data get a 7-bit transfer encoding (base64 or
  quoted-printable; the decoded content, charset and every other header stay the same);
* header values holding raw 8-bit bytes (not allowed by RFC 5322, but common) are decoded with
  the part's charset (else Windows-1252) and re-encoded as RFC 2047 encoded-words; in address
  headers only the display names are encoded, so the addresses stay intact.

What the user reads is unchanged; only the transfer encoding differs.
"""

from __future__ import annotations

import email
import email.encoders
import email.policy
from email.header import Header
from email.message import Message
from email.utils import formataddr, getaddresses

ADDRESS_HEADERS = {"from", "to", "cc", "bcc", "reply-to", "sender", "resent-from", "resent-to"}
FALLBACK_CHARSET = "windows-1252"


def _has_surrogates(value: str) -> bool:
    return any("\udc80" <= ch <= "\udcff" for ch in value)


def _decode(raw: bytes, charset: str | None) -> str:
    for cs in (charset, "utf-8", FALLBACK_CHARSET):
        if not cs:
            continue
        try:
            return raw.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1")


def _fix_headers(part: Message, charset: str | None) -> None:
    # raw_items: header values as parsed (8-bit bytes kept as surrogates), not sanitised.
    for name, value in list(part.raw_items()):
        if not isinstance(value, str) or not _has_surrogates(value):
            continue
        text = _decode(value.encode("ascii", "surrogateescape"), charset)
        if name.lower() in ADDRESS_HEADERS:
            new: str = ", ".join(
                formataddr((n, a), charset="utf-8") for n, a in getaddresses([text])
            )
        else:
            new = Header(text, "utf-8", header_name=name).encode()
        part.replace_header(name, new)


def to_7bit(raw: bytes) -> str | None:
    """ASCII version of ``raw`` (None if it cannot be made ASCII)."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    top_charset = msg.get_content_charset()
    for part in msg.walk():
        charset = part.get_content_charset() or top_charset
        _fix_headers(part, charset)
        if part.is_multipart():
            continue
        payload = part.get_payload()
        if not isinstance(payload, str) or not _has_surrogates(payload):
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        del part["Content-Transfer-Encoding"]
        part.set_payload(data)
        email.encoders.encode_base64(part)
    out = msg.as_string()
    return out if out.isascii() else None
