"""In-memory Carbonio CE simulator speaking the JSON SOAP + REST contract we assume.

It implements exactly the calls CarboRewind uses, with Zimbra semantics: change sequence
numbers, ``SyncRequest`` tokens (initial sync lists folder contents as id lists, deltas list
changed items and deleted ids), ``mail.MUST_RESYNC`` for tokens older than the tombstone
horizon, paging with ``more``. It is the reference for the contract tests and will back the
end-to-end tests; it does not replace a check against a real server
(``carborewind-agent carbonio probe``).
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.parser import BytesHeaderParser
from typing import Any
from urllib.parse import unquote

import httpx

ADMIN_PASSWORD = "admin-secret-for-tests"


@dataclass
class Item:
    id: int
    kind: str  # m, cn, appt, task, doc
    folder: int
    raw: bytes
    flags: str = ""
    tags: str = ""
    date: int = 0
    mod: int = 0


@dataclass
class Folder:
    id: int
    name: str
    parent: int | None
    view: str = "message"
    mod: int = 0


@dataclass
class Mailbox:
    id: str
    name: str
    status: str = "active"
    attrs: dict[str, str] = field(default_factory=dict)
    folders: dict[int, Folder] = field(default_factory=dict)
    items: dict[int, Item] = field(default_factory=dict)
    # (change seq, item id) for deletions; tombstones older than `horizon` are purged.
    tombstones: list[tuple[int, int]] = field(default_factory=list)
    seq: int = 1
    next_id: int = 300
    horizon: int = 0

    def __post_init__(self) -> None:
        if not self.folders:
            for fid, name, view in (
                (1, "", "unknown"),
                (2, "Inbox", "message"),
                (5, "Sent", "message"),
                (7, "Contacts", "contact"),
                (10, "Calendar", "appointment"),
            ):
                self.folders[fid] = Folder(fid, name, None if fid == 1 else 1, view)

    def bump(self) -> int:
        self.seq += 1
        return self.seq

    def add(self, kind: str, folder: int, raw: bytes, **kw: Any) -> Item:
        self.next_id += 1
        item = Item(self.next_id, kind, folder, raw, mod=self.bump(), **kw)
        self.items[item.id] = item
        return item

    def modify(self, item_id: int, **changes: Any) -> None:
        item = self.items[item_id]
        for key, value in changes.items():
            setattr(item, key, value)
        item.mod = self.bump()

    def delete(self, item_id: int) -> None:
        del self.items[item_id]
        self.tombstones.append((self.bump(), item_id))

    def purge_tombstones(self) -> None:
        """Simulate Carbonio purging old tombstones: older tokens must resync."""
        self.horizon = self.seq
        self.tombstones.clear()


class FakeCarbonio:
    def __init__(self) -> None:
        self.mailboxes: dict[str, Mailbox] = {}
        self.admin_tokens: set[str] = set()
        self.delegated: dict[str, str] = {}  # token -> account id
        self.uploads: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.fail_next: list[int] = []  # HTTP statuses to return before answering
        self.quota_denied = False
        self.quota_missing: set[str] = set()  # accounts GetQuotaUsage does not list
        self.vip_quota = 5 * 1024**3
        # zmcontrol-like status: service -> (running, checked at, s); queue name -> count.
        self.services: dict[str, tuple[bool, int]] = {}
        self.upload_reply: str | None = None  # e.g. "401,'null'": the servlet refuses
        self.queues: dict[str, int] | None = {"deferred": 0, "active": 0}

    # ------------------------------------------------------------------ setup helpers

    def add_mailbox(self, name: str, **attrs: str) -> Mailbox:
        mbox = Mailbox(id=secrets.token_hex(8), name=name, attrs=attrs)
        self.mailboxes[mbox.id] = mbox
        return mbox

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ HTTP dispatch

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0))
        path = request.url.path
        if request.method == "POST" and path in ("/service/admin/soap", "/service/soap"):
            envelope = json.loads(request.content)
            ((name, body),) = envelope["Body"].items()
            token = envelope.get("Header", {}).get("context", {}).get("authToken")
            self.calls.append(name)
            try:
                handler = getattr(self, f"_{name}")
                result = handler(body, token, admin=path == "/service/admin/soap")
            except _Fault as fault:
                return httpx.Response(
                    500,
                    json={
                        "Body": {
                            "Fault": {
                                "Code": {"Value": "soap:Sender"},
                                "Reason": {"Text": fault.reason},
                                "Detail": {"Error": {"Code": fault.code, "_jsns": "urn:zimbra"}},
                            }
                        }
                    },
                )
            response_name = name.removesuffix("Request") + "Response"
            return httpx.Response(200, json={"Body": {response_name: result}})
        if request.method == "GET" and path.startswith("/home/"):
            return self._rest_get(request)
        if request.method == "POST" and path == "/service/upload":
            account = self._cookie_account(request)
            if self.upload_reply is not None:
                return httpx.Response(200, text=self.upload_reply)
            aid = secrets.token_hex(6)
            self.uploads[aid] = request.content
            self.calls.append(f"upload:{account}")
            return httpx.Response(200, text=f"200,'null','{aid}'")
        if request.method == "POST" and path.startswith("/home/"):
            fmt = request.url.params.get("fmt")
            self.calls.append(f"import:{fmt}")
            mbox = self._cookie_account(request)
            account, _, folder_path = unquote(path.removeprefix("/home/")).partition("/")
            if account != mbox.name:
                return httpx.Response(403)
            folder = self.folder_by_path(mbox, folder_path)
            if folder is None:
                return httpx.Response(404)
            kind = {"vcf": "cn", "ics": "appt"}.get(fmt or "")
            if kind is None:
                return httpx.Response(400)
            mbox.add(kind, folder.id, request.content)
            return httpx.Response(200)
        return httpx.Response(404)

    # ------------------------------------------------------------------ auth

    def _admin_only(self, token: str | None, admin: bool) -> None:
        if not admin or token not in self.admin_tokens:
            raise _Fault("service.AUTH_REQUIRED", "admin token required")

    def _mailbox_for(self, token: str | None) -> Mailbox:
        account = self.delegated.get(token or "")
        if account is None:
            raise _Fault("service.AUTH_EXPIRED", "auth credentials have expired")
        return self.mailboxes[account]

    def _cookie_account(self, request: httpx.Request) -> Mailbox:
        cookie = request.headers.get("cookie", "")
        token = cookie.removeprefix("ZM_AUTH_TOKEN=")
        return self._mailbox_for(token)

    def _AuthRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        if not admin or body.get("password") != ADMIN_PASSWORD:
            raise _Fault("account.AUTH_FAILED", "authentication failed")
        new = secrets.token_hex(16)
        self.admin_tokens.add(new)
        return {"authToken": [{"_content": new}], "lifetime": 43_200_000}

    def _DelegateAuthRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        account_id = body["account"]["_content"]
        if account_id not in self.mailboxes:
            raise _Fault("account.NO_SUCH_ACCOUNT", account_id)
        new = secrets.token_hex(16)
        self.delegated[new] = account_id
        return {"authToken": [{"_content": new}], "lifetime": int(body["duration"]) * 1000}

    # ------------------------------------------------------------------ admin

    def _SearchDirectoryRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        boxes = sorted(self.mailboxes.values(), key=lambda m: m.name)
        if body.get("domain"):
            boxes = [m for m in boxes if m.name.endswith("@" + body["domain"])]
        offset, limit = int(body.get("offset", 0)), int(body.get("limit", 100))
        page = boxes[offset : offset + limit]
        return {
            "account": [self._account_entry(m) for m in page],
            "more": offset + limit < len(boxes),
            "searchTotal": len(boxes),
        }

    def _GetAllCosRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        return {
            "cos": [
                {
                    "id": "cos-default",
                    "name": "default",
                    "a": [{"n": "zimbraMailQuota", "_content": "0"}],
                },
                {
                    "id": "cos-vip",
                    "name": "vip",
                    "a": [{"n": "zimbraMailQuota", "_content": str(self.vip_quota)}],
                },
            ]
        }

    def _GetMailboxRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        mbox = self.mailboxes.get(body["mbox"]["id"])
        if mbox is None:
            raise _Fault("account.NO_SUCH_ACCOUNT", "no such account")
        return {"mbox": [{"mbid": "1", "s": sum(len(i.raw) for i in mbox.items.values())}]}

    def _GetQuotaUsageRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        if self.quota_denied:
            raise _Fault("service.PERM_DENIED", "permission denied")
        boxes = sorted(self.mailboxes.values(), key=lambda m: m.name)
        offset, limit = int(body.get("offset", 0)), int(body.get("limit", 100))
        page = boxes[offset : offset + limit]
        return {
            "account": [
                {
                    "id": m.id,
                    "name": m.name,
                    "used": sum(len(i.raw) for i in m.items.values()),
                    "limit": int(q) if (q := m.attrs.get("zimbraMailQuota", "0")).isdigit() else 0,
                }
                for m in page
                if m.id not in self.quota_missing
            ],
            "more": offset + limit < len(boxes),
        }

    def _GetServiceStatusRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        return {
            "status": [
                {
                    "server": "mail.example.test",
                    "service": name,
                    "t": t,
                    "_content": "1" if up else "0",
                }
                for name, (up, t) in self.services.items()
            ]
        }

    def _GetMailQueueInfoRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        if self.queues is None:
            raise _Fault("service.FAILURE", "queue info unavailable")
        queues = [{"name": k, "n": v} for k, v in self.queues.items()]
        return {"server": [{"name": body["server"]["name"], "queue": queues}]}

    def _ModifyAccountRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        mbox = self.mailboxes.get(body["id"])
        if mbox is None:
            raise _Fault("account.NO_SUCH_ACCOUNT", "no such account")
        for attr in body.get("a", []):
            if attr["n"] == "zimbraAccountStatus":
                mbox.status = attr["_content"]
            else:
                mbox.attrs[attr["n"]] = attr["_content"]
        return {"account": [self._account_entry(mbox)]}

    def _GetAccountRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        self._admin_only(token, admin)
        mbox = self.mailboxes.get(body["account"]["_content"])
        if mbox is None:
            raise _Fault("account.NO_SUCH_ACCOUNT", "no such account")
        return {"account": [self._account_entry(mbox)]}

    @staticmethod
    def _account_entry(m: Mailbox) -> dict[str, Any]:
        attrs = {"zimbraId": m.id, "zimbraAccountStatus": m.status, **m.attrs}
        return {
            "id": m.id,
            "name": m.name,
            "a": [{"n": k, "_content": v} for k, v in attrs.items()],
        }

    # ------------------------------------------------------------------ mail

    def _SyncRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        since = body.get("token")
        limit = int(body.get("changeLimit", 10_000))
        if since is None:
            return {"token": str(mbox.seq), "md": 0, "folder": [self._tree(mbox, 1)]}
        since_seq = int(since)
        if since_seq < mbox.horizon:
            raise _Fault("mail.MUST_RESYNC", "token too old, resync required")
        changed = sorted((i for i in mbox.items.values() if i.mod > since_seq), key=lambda i: i.mod)
        deleted = [iid for seq, iid in mbox.tombstones if seq > since_seq]
        page, more = changed[:limit], len(changed) > limit
        new_token = str(page[-1].mod) if more else str(mbox.seq)
        out: dict[str, Any] = {"token": new_token, "md": 0, "more": more}
        for item in page:
            out.setdefault(item.kind, []).append(self._item_entry(item))
        if deleted and not more:
            out["deleted"] = [{"ids": ",".join(map(str, deleted))}]
        return out

    def _tree(self, mbox: Mailbox, fid: int) -> dict[str, Any]:
        f = mbox.folders[fid]
        node: dict[str, Any] = {"id": str(fid), "name": f.name, "view": f.view}
        for kind in ("m", "cn", "appt", "task", "doc"):
            ids = [str(i.id) for i in mbox.items.values() if i.folder == fid and i.kind == kind]
            if ids:
                node[kind] = [{"ids": ",".join(ids)}]
        children = [c.id for c in mbox.folders.values() if c.parent == fid]
        if children:
            node["folder"] = [self._tree(mbox, c) for c in sorted(children)]
        return node

    @staticmethod
    def _item_entry(item: Item) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "l": str(item.folder),
            "f": item.flags,
            "tn": item.tags,
            "d": item.date,
            "ms": item.mod,
            "s": len(item.raw),
        }

    def _GetItemRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        item = mbox.items.get(int(body["item"]["id"]))
        if item is None:
            raise _Fault("mail.NO_SUCH_ITEM", "no such item")
        return {item.kind: [self._item_entry(item)]}

    def _GetMsgMetadataRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        self.meta_batches = getattr(self, "meta_batches", 0) + 1
        ids = [int(i) for i in body["m"]["ids"].split(",")]
        return {
            "m": [
                self._item_entry(mbox.items[i])
                for i in ids
                if i in mbox.items and mbox.items[i].kind == "m"
            ]
        }

    def _CreateFolderRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        spec = body["folder"]
        parent = int(spec["l"])
        for f in mbox.folders.values():
            if f.parent == parent and f.name == spec["name"]:
                if spec.get("fie"):
                    return {"folder": [{"id": str(f.id)}]}
                raise _Fault("mail.ALREADY_EXISTS", "folder exists")
        mbox.next_id += 1
        folder = Folder(
            mbox.next_id, spec["name"], parent, spec.get("view", "message"), mbox.bump()
        )
        mbox.folders[folder.id] = folder
        return {"folder": [{"id": str(folder.id)}]}

    def _AddMsgRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        m = body["m"]
        raw = self.uploads.pop(m["aid"]) if "aid" in m else m["content"]["_content"].encode("utf-8")
        if int(m["l"]) not in mbox.folders:
            raise _Fault("mail.NO_SUCH_FOLDER", "no such folder")
        item = mbox.add(
            "m",
            int(m["l"]),
            raw,
            flags=m.get("f", ""),
            tags=m.get("tn", ""),
            date=int(m.get("d", 0)),
        )
        return {"m": [{"id": str(item.id)}]}

    def _SearchRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        """``inid:<folder> msgid:"<id>"`` (migrations) and ``before:M/D/YYYY`` (archiving)."""
        mbox = self._mailbox_for(token)
        before = re.fullmatch(r"before:(\d{1,2})/(\d{1,2})/(\d{4})", body["query"])
        if before:
            month, day, year = (int(g) for g in before.groups())
            limit_ms = int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)
            found = sorted(
                (i for i in mbox.items.values() if i.kind == "m" and i.date < limit_ms),
                key=lambda i: (i.date, i.id),
            )
            offset, limit = int(body.get("offset", 0)), int(body.get("limit", 100))
            page = found[offset : offset + limit]
            return {
                "m": [self._item_entry(i) for i in page],
                "more": offset + limit < len(found),
            }
        m = re.fullmatch(r'inid:(\d+) msgid:"((?:\\.|[^"\\])*)"', body["query"])
        if not m:
            raise _Fault("mail.QUERY_PARSE_ERROR", body["query"])
        folder = int(m.group(1))
        wanted = m.group(2).replace('\\"', '"').replace("\\\\", "\\")
        hits = []
        for item in mbox.items.values():
            if item.kind != "m" or item.folder != folder:
                continue
            head = BytesHeaderParser().parsebytes(item.raw)
            if " ".join(str(head.get("Message-ID", "")).split()) == wanted:
                hits.append({"id": str(item.id)})
        return {"m": hits[: int(body.get("limit", 100))]} if hits else {}

    def _MsgActionRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)
        action = body["action"]
        if action["op"] != "delete":
            raise _Fault("service.INVALID_REQUEST", action["op"])
        ids = [int(i) for i in action["id"].split(",")]
        missing = [i for i in ids if i not in mbox.items]
        if missing:
            raise _Fault("mail.NO_SUCH_ITEM", f"no such item: {missing[0]}")
        for item_id in ids:
            mbox.delete(item_id)
        return {"action": {"op": "delete", "id": action["id"]}}

    def _GetFolderRequest(self, body: dict[str, Any], token: str | None, admin: bool) -> Any:
        mbox = self._mailbox_for(token)

        def node(fid: int) -> dict[str, Any]:
            f = mbox.folders[fid]
            out: dict[str, Any] = {"id": str(fid), "name": f.name, "view": f.view}
            children = sorted(c.id for c in mbox.folders.values() if c.parent == fid)
            if children:
                out["folder"] = [node(c) for c in children]
            return out

        return {"folder": [node(1)]}

    def folder_by_path(self, mbox: Mailbox, path: str) -> Folder | None:
        current = mbox.folders[1]
        for name in [p for p in path.split("/") if p]:
            match = [
                f
                for f in mbox.folders.values()
                if f.parent == current.id and f.name.lower() == name.lower()
            ]
            if not match:
                return None
            current = match[0]
        return current

    # ------------------------------------------------------------------ REST

    def _rest_get(self, request: httpx.Request) -> httpx.Response:
        mbox = self._cookie_account(request)
        account = unquote(request.url.path.removeprefix("/home/").strip("/"))
        if account != mbox.name:
            return httpx.Response(403)
        item = mbox.items.get(int(request.url.params.get("id", "0")))
        self.calls.append("rest:get")
        if item is None:
            return httpx.Response(404)
        return httpx.Response(200, content=item.raw)


class _Fault(Exception):  # noqa: N818 - mirrors the SOAP vocabulary
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
