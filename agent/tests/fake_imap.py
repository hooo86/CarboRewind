"""Tiny IMAP4rev1 server for tests: just what a read-only migration uses.

It runs on a real socket (optionally with TLS) so that the agent's ``imaplib`` code path is
exercised end to end: CAPABILITY, LOGIN, AUTHENTICATE PLAIN (with an authorization
identity for a master user), NAMESPACE, LIST, EXAMINE/SELECT, UID SEARCH, UID FETCH, LOGOUT.
"""

from __future__ import annotations

import base64
import re
import socketserver
import ssl
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class Message:
    uid: int
    raw: bytes
    flags: list[str] = field(default_factory=list)
    date: datetime = field(default_factory=lambda: datetime(2026, 1, 15, 9, 30, tzinfo=UTC))


@dataclass
class Box:
    name: str  # raw (modified UTF-7) name
    flags: list[str] = field(default_factory=list)
    uidvalidity: int = 1000
    messages: list[Message] = field(default_factory=list)
    next_uid: int = 1

    def add(self, raw: bytes, flags: list[str] | None = None, **kw: Any) -> Message:
        msg = Message(self.next_uid, raw, flags or [], **kw)
        self.next_uid += 1
        self.messages.append(msg)
        return msg


@dataclass
class Account:
    password: str
    boxes: dict[str, Box] = field(default_factory=dict)

    def box(self, name: str, flags: list[str] | None = None) -> Box:
        if name not in self.boxes:
            self.boxes[name] = Box(name, flags or [])
        return self.boxes[name]


def _tokens(line: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == " ":
            i += 1
        elif ch == '"':
            j, buf = i + 1, []
            while line[j] != '"':
                if line[j] == "\\":
                    j += 1
                buf.append(line[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif ch == "(":
            depth, j = 0, i
            while True:
                depth += {"(": 1, ")": -1}.get(line[j], 0)
                j += 1
                if depth == 0:
                    break
            out.append(line[i:j])
            i = j
        else:
            j = line.find(" ", i)
            j = len(line) if j == -1 else j
            out.append(line[i:j])
            i = j
    return out


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeImapServer:
    def __init__(
        self,
        *,
        delimiter: str = "/",
        prefix: str = "",
        master: tuple[str, str] | None = None,
        tls: ssl.SSLContext | None = None,
        flags_after_body: bool = False,
        literal_names: bool = False,
    ) -> None:
        self.accounts: dict[str, Account] = {}
        self.delimiter = delimiter
        self.prefix = prefix
        self.master = master
        self.flags_after_body = flags_after_body
        self.literal_names = literal_names
        self.commands: list[str] = []
        self.logins: list[tuple[str, str]] = []  # (authenticated as, acting for)
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                outer._session(self)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

            def get_request(self) -> tuple[Any, Any]:
                sock, addr = super().get_request()
                if tls is not None:
                    sock = tls.wrap_socket(sock, server_side=True)
                return sock, addr

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeImapServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def account(self, login: str, password: str) -> Account:
        self.accounts[login] = Account(password)
        return self.accounts[login]

    # ------------------------------------------------------------------ protocol

    def _session(self, h: socketserver.StreamRequestHandler) -> None:
        def send(data: str | bytes) -> None:
            h.wfile.write(data.encode() if isinstance(data, str) else data)
            h.wfile.flush()

        send("* OK fake IMAP ready\r\n")
        user: Account | None = None
        selected: Box | None = None
        while True:
            raw = h.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            tag, _, rest = line.partition(" ")
            cmd, _, args = rest.partition(" ")
            cmd = cmd.upper()
            self.commands.append(f"{cmd} {args}".strip() if cmd != "LOGIN" else "LOGIN")
            if cmd == "CAPABILITY":
                send("* CAPABILITY IMAP4rev1 NAMESPACE AUTH=PLAIN SPECIAL-USE\r\n")
                send(f"{tag} OK done\r\n")
            elif cmd == "LOGIN":
                login, password = _tokens(args)[:2]
                user = self._auth(login, password, login)
                ok = f"{tag} OK logged in\r\n"
                send(ok if user else f"{tag} NO [AUTHENTICATIONFAILED] no\r\n")
            elif cmd == "AUTHENTICATE":
                send("+ \r\n")
                token = base64.b64decode(h.rfile.readline().strip()).decode()
                authz, authc, password = token.split("\0")
                user = self._auth(authc, password, authz or authc)
                send(f"{tag} OK authenticated\r\n" if user else f"{tag} NO bad credentials\r\n")
            elif cmd == "LOGOUT":
                send("* BYE\r\n")
                send(f"{tag} OK bye\r\n")
                return
            elif user is None:
                send(f"{tag} BAD authenticate first\r\n")
            elif cmd == "NAMESPACE":
                send(f'* NAMESPACE (({_quote(self.prefix)} "{self.delimiter}")) NIL NIL\r\n')
                send(f"{tag} OK done\r\n")
            elif cmd == "LIST":
                for box in user.boxes.values():
                    flags = " ".join(box.flags)
                    if self.literal_names:
                        name = box.name.encode()
                        head = f'* LIST ({flags}) "{self.delimiter}" {{{len(name)}}}\r\n'
                        send(head.encode() + name + b"\r\n")
                    else:
                        send(f'* LIST ({flags}) "{self.delimiter}" {_quote(box.name)}\r\n')
                send(f"{tag} OK done\r\n")
            elif cmd in ("SELECT", "EXAMINE"):
                name = _tokens(args)[0]
                selected = user.boxes.get(name)
                if selected is None or "\\Noselect" in selected.flags:
                    send(f"{tag} NO no such mailbox\r\n")
                    selected = None
                    continue
                send(f"* {len(selected.messages)} EXISTS\r\n")
                send(f"* OK [UIDVALIDITY {selected.uidvalidity}] ok\r\n")
                send(f"{tag} OK [READ-ONLY] done\r\n")
            elif cmd == "UID" and selected is not None:
                sub, _, uargs = args.partition(" ")
                if sub.upper() == "SEARCH":
                    send(f"* SEARCH {' '.join(str(u) for u in self._search(selected, uargs))}\r\n")
                    send(f"{tag} OK done\r\n")
                elif sub.upper() == "FETCH":
                    uid = int(uargs.split(" ", 1)[0])
                    for seq, msg in enumerate(selected.messages, 1):
                        if msg.uid == uid:
                            send(self._fetch_line(seq, msg))
                    send(f"{tag} OK done\r\n")
                else:
                    send(f"{tag} BAD unsupported\r\n")
            elif cmd == "NOOP":
                send(f"{tag} OK done\r\n")
            else:
                send(f"{tag} BAD unsupported {cmd}\r\n")

    def _auth(self, login: str, password: str, acting_for: str) -> Account | None:
        if self.master and login == self.master[0]:
            if password != self.master[1]:
                return None
            self.logins.append((login, acting_for))
            return self.accounts.get(acting_for)
        account = self.accounts.get(login)
        if account is None or account.password != password or acting_for != login:
            return None
        self.logins.append((login, login))
        return account

    @staticmethod
    def _search(box: Box, criteria: str) -> list[int]:
        m = re.search(r"UID (\d+):\*", criteria)
        start = int(m.group(1)) if m else 1
        uids = [msg.uid for msg in box.messages if msg.uid >= start]
        if not uids and box.messages and m:
            uids = [box.messages[-1].uid]  # "n:*" matches the last message
        since = re.search(r"SINCE (\S+)", criteria)
        before = re.search(r"BEFORE (\S+)", criteria)

        def day(text: str) -> datetime:
            d, mon, y = text.split("-")
            return datetime(int(y), _MONTHS.index(mon) + 1, int(d), tzinfo=UTC)

        by_uid = {msg.uid: msg for msg in box.messages}
        if since:
            uids = [u for u in uids if by_uid[u].date >= day(since.group(1))]
        if before:
            uids = [u for u in uids if by_uid[u].date < day(before.group(1))]
        return uids

    def _fetch_line(self, seq: int, msg: Message) -> bytes:
        flags = f"FLAGS ({' '.join(msg.flags)})"
        date = f'INTERNALDATE "{msg.date.strftime("%d-%b-%Y %H:%M:%S +0000")}"'
        head = f"* {seq} FETCH (UID {msg.uid} "
        if self.flags_after_body:
            return (
                f"{head}{date} BODY[] {{{len(msg.raw)}}}\r\n".encode()
                + msg.raw
                + f" {flags})\r\n".encode()
            )
        return f"{head}{flags} {date} BODY[] {{{len(msg.raw)}}}\r\n".encode() + msg.raw + b")\r\n"
