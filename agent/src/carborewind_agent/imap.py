"""Read-only IMAP client for mailbox migrations (standard library ``imaplib`` only).

The remote server is reached with TLS (implicit or STARTTLS) and certificate verification
unless the administrator explicitly disabled it for that source. Folders are only ever
``EXAMINE``-d (read-only) and messages fetched with ``BODY.PEEK[]``: a migration never
changes anything on the source server, not even the \\Seen flag.

Authentication:

* ``login``: ``LOGIN user password`` (the mailbox's own password, or a Dovecot-style
  ``user*master`` login built by the API);
* ``plain``: ``AUTHENTICATE PLAIN`` with an authorization identity, so that an administrator
  (Zimbra/Carbonio admin, Dovecot master user) reads the mailbox of ``authzid``.
"""

from __future__ import annotations

import base64
import contextlib
import imaplib
import re
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

Security = Literal["ssl", "starttls", "none"]
AuthMode = Literal["login", "plain"]

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?P<delim>"(?:\\.|[^"\\])*"|NIL) ?(?P<name>.*)$')
_UID_RE = re.compile(rb"\bUID (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_DATE_RE = re.compile(rb'\bINTERNALDATE "[^"]+"')
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ImapError(Exception):
    """IMAP failure. ``retryable`` is False when retrying cannot help (bad credentials)."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    security: Security
    verify_tls: bool
    username: str
    password: str
    auth: AuthMode = "login"
    authzid: str | None = None
    timeout: float = 60.0

    def __repr__(self) -> str:  # never print the password
        return f"ImapSettings({self.username}@{self.host}:{self.port}, {self.security})"

    @classmethod
    def from_job(cls, data: dict[str, Any]) -> ImapSettings:
        return cls(
            host=str(data["host"]),
            port=int(data["port"]),
            security=data.get("security", "ssl"),
            verify_tls=bool(data.get("verify_tls", True)),
            username=str(data["username"]),
            password=str(data["password"]),
            auth=data.get("auth", "login"),
            authzid=data.get("authzid"),
            timeout=float(data.get("timeout", 60)),
        )


@dataclass(frozen=True)
class RemoteFolder:
    raw: str  # name as the server spells it (modified UTF-7), used in commands
    name: str  # decoded, for display and mapping
    delimiter: str | None
    flags: frozenset[str]

    @property
    def selectable(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.flags)


@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    flags: frozenset[str]
    internal_ms: int | None
    raw: bytes


# ------------------------------------------------------------------ encodings


def decode_mutf7(value: str) -> str:
    """IMAP modified UTF-7 (RFC 3501 §5.1.3) -> str. ``&-`` is ``&``."""
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = value.find("-", i)
        if end == -1:  # malformed: keep as is
            out.append(value[i:])
            break
        chunk = value[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                out.append(value[i : end + 1])
        i = end + 1
    return "".join(out)


def quote(value: str) -> str:
    """IMAP quoted string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(value: bytes) -> str:
    text = value.decode("utf-8", "replace").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return text


def imap_date(d: date) -> str:
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


# ------------------------------------------------------------------ response parsing


def parse_list(data: list[Any]) -> list[RemoteFolder]:
    """Parse the data of a ``LIST`` response (names may come as literals)."""
    folders: list[RemoteFolder] = []
    for entry in data:
        if entry is None:
            continue
        if isinstance(entry, tuple):  # (b'(\\HasNoChildren) "/" {12}', b'Folder name')
            head, literal = entry[0], entry[1]
            line = re.sub(rb"\{\d+\}$", b"", head) + b'"' + literal.replace(b'"', b'\\"') + b'"'
        else:
            line = entry
        m = _LIST_RE.match(line)
        if not m:
            continue
        delim_raw = m.group("delim")
        delim = None if delim_raw == b"NIL" else _unquote(delim_raw)
        raw = _unquote(m.group("name"))
        flags = frozenset(f.lower() for f in m.group("flags").decode().split())
        folders.append(RemoteFolder(raw, decode_mutf7(raw), delim or None, flags))
    return folders


def parse_namespace(data: list[Any]) -> str:
    """Personal namespace prefix from a ``NAMESPACE`` response (``""`` when none)."""
    if not data or not isinstance(data[0], bytes):
        return ""
    m = re.match(rb'^\(\("((?:\\.|[^"\\])*)"', data[0])
    return _unquote(b'"' + m.group(1) + b'"') if m else ""


def parse_fetch(data: list[Any]) -> list[FetchedMessage]:
    """Parse a ``UID FETCH (UID FLAGS INTERNALDATE BODY.PEEK[])`` response.

    imaplib returns ``(header, literal)`` tuples followed by the rest of the line, where some
    servers put FLAGS after the literal.
    """
    out: list[FetchedMessage] = []
    i = 0
    while i < len(data):
        entry = data[i]
        if isinstance(entry, tuple) and len(entry) == 2:
            meta = entry[0]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += b" " + data[i + 1]
                i += 1
            uid = _UID_RE.search(meta)
            if uid:
                flags = _FLAGS_RE.search(meta)
                date_m = _DATE_RE.search(meta)
                internal_ms = None
                if date_m:
                    tup = imaplib.Internaldate2tuple(date_m.group(0))
                    if tup is not None:
                        internal_ms = int(time.mktime(tup) * 1000)
                out.append(
                    FetchedMessage(
                        uid=int(uid.group(1)),
                        flags=frozenset(
                            f.lower() for f in (flags.group(1).decode().split() if flags else [])
                        ),
                        internal_ms=internal_ms,
                        raw=bytes(entry[1]),
                    )
                )
        i += 1
    return out


# ------------------------------------------------------------------ client


class ImapSource:
    """One read-only session on the source server. Use as a context manager."""

    def __init__(self, settings: ImapSettings) -> None:
        self._s = settings
        self._imap: imaplib.IMAP4 | None = None

    def __enter__(self) -> ImapSource:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if not self._s.verify_tls:  # explicit choice of the administrator for this source
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    @property
    def imap(self) -> imaplib.IMAP4:
        if self._imap is None:
            raise ImapError("not connected")
        return self._imap

    def connect(self) -> None:
        s = self._s
        try:
            if s.security == "ssl":
                self._imap = imaplib.IMAP4_SSL(
                    s.host, s.port, ssl_context=self._context(), timeout=s.timeout
                )
            else:
                self._imap = imaplib.IMAP4(s.host, s.port, timeout=s.timeout)
                if s.security == "starttls":
                    self._imap.starttls(ssl_context=self._context())
        except ssl.SSLCertVerificationError as exc:
            raise ImapError(
                f"TLS certificate of {s.host} rejected: {exc}", retryable=False
            ) from exc
        except (OSError, imaplib.IMAP4.error) as exc:
            raise ImapError(f"cannot connect to {s.host}:{s.port}: {exc}") from exc
        try:
            if s.auth == "plain":
                token = f"{s.authzid or ''}\0{s.username}\0{s.password}".encode()
                self.imap.authenticate("PLAIN", lambda _challenge: token)
            else:
                self.imap.login(s.username, s.password)
        except imaplib.IMAP4.error as exc:
            self.close()
            who = s.authzid or s.username
            raise ImapError(f"IMAP authentication failed for {who}", retryable=False) from exc
        except OSError as exc:
            self.close()
            raise ImapError(f"connection lost during login: {exc}") from exc

    def close(self) -> None:
        if self._imap is not None:
            with contextlib.suppress(OSError, imaplib.IMAP4.error):
                self._imap.logout()
            self._imap = None

    def _ok(self, typ: str, data: list[Any], what: str) -> list[Any]:
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data and data[0] else ""
            raise ImapError(f"{what}: {detail}".strip(": "), retryable=False)
        return data

    def _call(self, what: str, fn: Any, *args: Any) -> list[Any]:
        try:
            typ, data = fn(*args)
        except imaplib.IMAP4.abort as exc:
            raise ImapError(f"{what}: connection lost ({exc})") from exc
        except OSError as exc:  # includes timeouts
            raise ImapError(f"{what}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise ImapError(f"{what}: {exc}", retryable=False) from exc
        return self._ok(typ, data, what)

    def personal_prefix(self) -> str:
        if "NAMESPACE" not in self.imap.capabilities:
            return ""
        return parse_namespace(self._call("NAMESPACE", self.imap.namespace))

    def folders(self) -> list[RemoteFolder]:
        return parse_list(self._call("LIST", self.imap.list, '""', "*"))

    def examine(self, folder: RemoteFolder) -> tuple[int, int]:
        """Select read-only; returns (UIDVALIDITY, number of messages)."""
        data = self._call(f"EXAMINE {folder.name}", self.imap.select, quote(folder.raw), True)
        exists = int(data[0]) if data and data[0] and data[0].isdigit() else 0
        _, validity = self.imap.response("UIDVALIDITY")
        uidvalidity = int(validity[0]) if validity and validity[0] else 0
        return uidvalidity, exists

    def uids(
        self, start: int = 1, since: date | None = None, before: date | None = None
    ) -> list[int]:
        """UIDs >= ``start`` of the selected folder (optionally within a date range)."""
        criteria = [f"UID {max(start, 1)}:*"]
        if since:
            criteria.append(f"SINCE {imap_date(since)}")
        if before:
            criteria.append(f"BEFORE {imap_date(before)}")
        data = self._call("UID SEARCH", self.imap.uid, "SEARCH", *criteria)
        found = sorted({int(x) for x in (data[0] or b"").split()}) if data else []
        # "n:*" always matches the last message, even when its UID is below n.
        return [u for u in found if u >= start]

    def fetch(self, uid: int) -> FetchedMessage | None:
        data = self._call(
            f"UID FETCH {uid}",
            self.imap.uid,
            "FETCH",
            str(uid),
            "(UID FLAGS INTERNALDATE BODY.PEEK[])",
        )
        messages = [m for m in parse_fetch(data) if m.uid == uid]
        return messages[0] if messages else None

    def iter_fetch(self, uids: list[int]) -> Iterator[FetchedMessage | int]:
        """Fetched messages; a UID that vanished meanwhile is yielded as an int."""
        for uid in uids:
            message = self.fetch(uid)
            yield message if message is not None else uid
