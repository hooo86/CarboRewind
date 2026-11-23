"""High-level Carbonio CE operations used by backup and restore.

Every method maps to one documented Carbonio/Zimbra API call; names are listed in
``docs/carbonio-integration.md`` together with the CE vs Advanced notes and the assumptions
still to verify (🔎). Edition-specific code stays behind this class so that an
``Advanced`` provider (native Backup module) can be added later without touching callers.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .mime7bit import to_7bit
from .soap import NS_ADMIN, NS_MAIL, SoapClient, SoapFaultError, content

log = logging.getLogger(__name__)

# Messages larger than this go through the upload servlet (no JSON overhead).
INLINE_MAX = 4 * 2**20

MUST_RESYNC = "mail.MUST_RESYNC"

# Account attributes read by the inventory (``SearchDirectoryRequest`` ``attrs``).
INVENTORY_ATTRS = (
    "zimbraId",
    "zimbraAccountStatus",
    "zimbraCOSId",
    "zimbraMailQuota",
    "zimbraMailAlias",
    "displayName",
    "givenName",
    "sn",
    "zimbraIsAdminAccount",
    "zimbraLastLogonTimestamp",
    "zimbraCreateTimestamp",
    # Profile (LDAP / Carbonio admin console "contact" fields).
    "title",
    "company",
    "ou",
    "telephoneNumber",
    "mobile",
    "homePhone",
    "street",
    "postalCode",
    "l",
    "st",
    "co",
    "description",
    "zimbraNotes",
    "employeeNumber",
)

# Item element names in SyncResponse / folder listings -> our item kinds.
ITEM_ELEMENTS = {
    "m": "message",
    "cn": "contact",
    "appt": "appointment",
    "task": "task",
    "doc": "document",
}


@dataclass(frozen=True)
class AccountInfo:
    id: str
    name: str
    status: str | None
    attrs: dict[str, list[str]]


@dataclass(frozen=True)
class FolderInfo:
    id: int
    parent_id: int | None
    path: str
    view: str
    attrs: dict[str, Any]


@dataclass
class SyncDelta:
    """Result of one SyncRequest page."""

    token: str
    # (kind, item id, folder id, raw attributes)
    changed: list[tuple[str, int, int, dict[str, Any]]] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    folders: list[FolderInfo] = field(default_factory=list)
    more: bool = False


@dataclass
class _Token:
    value: str
    expires_at: float


# Mail queue actions offered in the interface, and the shape of a Postfix queue id.
QUEUE_OPS = frozenset({"requeue", "hold", "release", "delete"})
QUEUE_ID = re.compile(r"[0-9A-Za-z]{5,32}")

LOGIN_FILTER = {
    "zimbraInvalidLoginFilterMaxFailedLogin": "max_failed",
    "zimbraInvalidLoginFilterDelayInMinBetwnReqBeforeReinstating": "block_minutes",
    "zimbraInvalidLoginFilterReinstateIpTaskIntervalInMin": "task_minutes",
}
SAFE_IPS = "zimbraHttpThrottleSafeIPs"


def _attrs(entry: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for a in entry.get("a", []):
        out.setdefault(a["n"], []).append(str(a.get("_content", "")))
    return out


def _ids(value: str | None) -> list[int]:
    return [int(x) for x in value.split(",") if x] if value else []


class CarbonioClient:
    def __init__(
        self,
        admin: SoapClient,
        mail: SoapClient,
        rest: httpx.Client,
        admin_name: str,
        admin_password: str,
        *,
        delegate_seconds: int = 3600,
    ) -> None:
        self._admin = admin
        self._mail = mail
        self._rest = rest
        self._admin_name = admin_name
        self._admin_password = admin_password
        self._delegate_seconds = delegate_seconds
        self._admin_token: _Token | None = None
        self._delegated: dict[str, _Token] = {}
        # Largest message sent inline; lowered when Carbonio answers "inline message too
        # large" (its limit is a server setting, lower than the SOAP request limit).
        self._inline_max = INLINE_MAX

    # ------------------------------------------------------------------ authentication

    def _admin_auth(self) -> str:
        if self._admin_token and self._admin_token.expires_at > time.time() + 60:
            return self._admin_token.value
        resp = self._admin.invoke(
            "Auth", NS_ADMIN, {"name": self._admin_name, "password": self._admin_password}
        )
        token = content(resp.get("authToken"))
        if not token:
            raise SoapFaultError("client.BAD_RESPONSE", "AuthResponse without authToken")
        lifetime_ms = int(resp.get("lifetime", 3_600_000))
        self._admin_token = _Token(token, time.time() + lifetime_ms / 1000)
        return token

    def delegate(self, account_id: str) -> str:
        """Short-lived token acting as the account (``DelegateAuthRequest``), cached."""
        cached = self._delegated.get(account_id)
        if cached and cached.expires_at > time.time() + 60:
            return cached.value
        resp = self._admin.invoke(
            "DelegateAuth",
            NS_ADMIN,
            {"account": {"by": "id", "_content": account_id}, "duration": self._delegate_seconds},
            token=self._admin_auth(),
        )
        token = content(resp.get("authToken"))
        if not token:
            raise SoapFaultError("client.BAD_RESPONSE", "DelegateAuthResponse without authToken")
        lifetime_s = int(resp.get("lifetime", self._delegate_seconds * 1000)) / 1000
        self._delegated[account_id] = _Token(token, time.time() + lifetime_s)
        return token

    # ------------------------------------------------------------------ inventory

    def iter_accounts(self, domain: str | None = None, page: int = 500) -> Iterator[AccountInfo]:
        """All accounts (optionally of one domain), paged with ``SearchDirectoryRequest``."""
        offset = 0
        while True:
            body: dict[str, Any] = {
                "query": "",
                "types": "accounts",
                "limit": page,
                "offset": offset,
                "attrs": ",".join(INVENTORY_ATTRS),
            }
            if domain:
                body["domain"] = domain
            resp = self._admin.invoke("SearchDirectory", NS_ADMIN, body, token=self._admin_auth())
            accounts = resp.get("account", [])
            for entry in accounts:
                attrs = _attrs(entry)
                status = attrs.get("zimbraAccountStatus", [None])[0]
                yield AccountInfo(entry["id"], entry["name"], status, attrs)
            offset += len(accounts)
            if not resp.get("more") or not accounts:
                return

    def cos_names(self) -> dict[str, str]:
        """Class-of-service id -> name (``GetAllCosRequest``)."""
        resp = self._admin.invoke("GetAllCos", NS_ADMIN, {}, token=self._admin_auth())
        return {c["id"]: c.get("name", c["id"]) for c in resp.get("cos", [])}

    def cos_info(self) -> dict[str, tuple[str, int | None]]:
        """Class-of-service id -> (name, mail quota in bytes or None), ``GetAllCosRequest``.

        The quota of an account without its own ``zimbraMailQuota`` comes from its COS.
        """
        resp = self._admin.invoke("GetAllCos", NS_ADMIN, {}, token=self._admin_auth())
        out: dict[str, tuple[str, int | None]] = {}
        for c in resp.get("cos", []):
            quota = (_attrs(c).get("zimbraMailQuota") or [""])[0]
            out[c["id"]] = (c.get("name", c["id"]), int(quota) if quota.isdigit() else None)
        return out

    def service_status(self) -> list[tuple[str, str, bool, int]]:
        """``(server, service, running, checked at in s)`` from ``GetServiceStatusRequest``.

        Carbonio's stats service records this every few minutes (what ``zmcontrol status``
        shows), so it needs no shell access on the server.
        """
        resp = self._admin.invoke("GetServiceStatus", NS_ADMIN, {}, token=self._admin_auth())
        out = []
        for entry in resp.get("status", []):
            content = str(entry.get("_content", "")).strip()
            out.append(
                (
                    str(entry.get("server", "")),
                    str(entry.get("service", "")),
                    content == "1",
                    int(entry.get("t") or 0),
                )
            )
        return out

    def mail_queue(self, server: str) -> dict[str, int]:
        """Messages per MTA queue (``GetMailQueueInfoRequest``): deferred, active, hold…"""
        resp = self._admin.invoke(
            "GetMailQueueInfo", NS_ADMIN, {"server": {"name": server}}, token=self._admin_auth()
        )
        out: dict[str, int] = {}
        for srv in resp.get("server", []):
            for queue in srv.get("queue", []):
                out[str(queue.get("name"))] = out.get(str(queue.get("name")), 0) + int(
                    queue.get("n") or 0
                )
        return out

    def mail_queue_items(
        self, server: str, queue: str, limit: int = 50, wait: int = 3
    ) -> dict[str, Any]:
        """Messages of one MTA queue (``GetMailQueueRequest``): ``{"total", "items",
        "reasons"}``. ``scan`` asks Carbonio to rescan the queue; ``wait`` how many seconds
        it may wait for that scan."""
        resp = self._admin.invoke(
            "GetMailQueue",
            NS_ADMIN,
            {
                "server": {
                    "name": server,
                    "queue": {
                        "name": queue,
                        "scan": 1,
                        "wait": wait,
                        "query": {"limit": limit, "offset": 0},
                    },
                }
            },
            token=self._admin_auth(),
        )
        total, items, reasons = 0, [], []
        for srv in resp.get("server", []):
            for q in srv.get("queue", []):
                total += int(q.get("total") or 0)
                for qi in q.get("qi", []):
                    items.append(
                        {
                            "id": str(qi.get("id", "")),
                            "from": str(qi.get("from", ""))[:320],
                            "to": str(qi.get("to", ""))[:1000],
                            "time": int(qi.get("time") or 0) // 1000 or None,
                            "reason": str(qi.get("reason", ""))[:500],
                            "origip": str(qi.get("origip", ""))[:64],
                        }
                    )
                for qs in q.get("qs", []):
                    if qs.get("type") == "reason":
                        reasons = [
                            {"reason": str(x.get("t", ""))[:300], "count": int(x.get("n") or 0)}
                            for x in qs.get("qsi", [])
                        ][:10]
        return {"total": total, "items": items[:limit], "reasons": reasons}

    def mail_queue_action(self, server: str, queue: str, op: str, ids: list[str]) -> None:
        """``requeue`` / ``hold`` / ``release`` / ``delete`` messages of a queue by id
        (``MailQueueActionRequest``)."""
        if op not in QUEUE_OPS:
            raise ValueError(f"unknown queue action {op!r}")
        if not ids or not all(QUEUE_ID.fullmatch(i) for i in ids):
            raise ValueError("invalid queue ids")
        self._admin.invoke(
            "MailQueueAction",
            NS_ADMIN,
            {
                "server": {
                    "name": server,
                    "queue": {
                        "name": queue,
                        "action": {"op": op, "by": "id", "_content": ",".join(ids)},
                    },
                }
            },
            token=self._admin_auth(),
        )

    def mail_queue_flush(self, server: str) -> None:
        """Try every queued message again now (``MailQueueFlushRequest``, ``postqueue -f``)."""
        self._admin.invoke(
            "MailQueueFlush", NS_ADMIN, {"server": {"name": server}}, token=self._admin_auth()
        )

    def mailbox_size(self, account_id: str) -> int:
        """Bytes used by one mailbox (``GetMailboxRequest``, exact, no cache)."""
        resp = self._admin.invoke(
            "GetMailbox", NS_ADMIN, {"mbox": {"id": account_id}}, token=self._admin_auth()
        )
        boxes = resp.get("mbox") or [{}]
        return int(boxes[0].get("s", 0))

    def quota_usage_detail(self, page: int = 1000) -> dict[str, tuple[int, int]]:
        """Account id -> (bytes used, effective quota), ``GetQuotaUsageRequest`` paged."""
        usage: dict[str, tuple[int, int]] = {}
        offset = 0
        while True:
            resp = self._admin.invoke(
                "GetQuotaUsage",
                NS_ADMIN,
                {"allServers": 1, "limit": page, "offset": offset, "sortBy": "totalUsed"},
                token=self._admin_auth(),
            )
            accounts = resp.get("account", [])
            for entry in accounts:
                usage[entry["id"]] = (int(entry.get("used", 0)), int(entry.get("limit", 0)))
            offset += len(accounts)
            if not resp.get("more") or not accounts:
                return usage

    def quota_usage(self, page: int = 1000) -> dict[str, int]:
        """Account id -> bytes used, for every account (``GetQuotaUsageRequest``, paged)."""
        used: dict[str, int] = {}
        offset = 0
        while True:
            resp = self._admin.invoke(
                "GetQuotaUsage",
                NS_ADMIN,
                {"allServers": 1, "limit": page, "offset": offset, "sortBy": "totalUsed"},
                token=self._admin_auth(),
            )
            accounts = resp.get("account", [])
            for entry in accounts:
                used[entry["id"]] = int(entry.get("used", 0))
            offset += len(accounts)
            if not resp.get("more") or not accounts:
                return used

    def login_filter(self) -> dict[str, Any]:
        """Invalid login filter settings and trusted addresses (``GetAllConfigRequest``)."""
        resp = self._admin.invoke("GetAllConfig", NS_ADMIN, {}, token=self._admin_auth())
        attrs = _attrs(resp)
        out: dict[str, Any] = {"safe_ips": attrs.get(SAFE_IPS, [])}
        for attr, key in LOGIN_FILTER.items():
            value = (attrs.get(attr) or [""])[0]
            out[key] = int(value) if value.isdigit() else None
        return out

    def set_trusted_ip(self, ip: str, trusted: bool) -> None:
        """Add to / remove from the global trusted addresses (``ModifyConfigRequest``)."""
        sign = "+" if trusted else "-"
        self._admin.invoke(
            "ModifyConfig",
            NS_ADMIN,
            {"a": [{"n": f"{sign}{SAFE_IPS}", "_content": ip}]},
            token=self._admin_auth(),
        )

    def set_account_status(self, account_id: str, status: str) -> None:
        """Lock (``locked``) or reactivate (``active``) an account (``ModifyAccountRequest``)."""
        if status not in {"locked", "active"}:
            raise ValueError(f"unsupported status {status!r}")
        attrs = [{"n": "zimbraAccountStatus", "_content": status}]
        if status == "active":
            # Also forget the failed logins that put the account in "lockout": otherwise
            # one more wrong password would lock it again at once.
            attrs.append({"n": "zimbraPasswordLockoutFailureTime", "_content": ""})
        self._admin.invoke(
            "ModifyAccount", NS_ADMIN, {"id": account_id, "a": attrs}, token=self._admin_auth()
        )

    def set_quota(self, account_id: str, quota_bytes: int) -> None:
        """Set the mail quota of an account (``zimbraMailQuota``, bytes; 0 = unlimited)."""
        if quota_bytes < 0:
            raise ValueError("negative quota")
        self._admin.invoke(
            "ModifyAccount",
            NS_ADMIN,
            {"id": account_id, "a": [{"n": "zimbraMailQuota", "_content": str(int(quota_bytes))}]},
            token=self._admin_auth(),
        )

    def find_account(self, by: str, value: str) -> AccountInfo | None:
        """``GetAccountRequest`` by ``id`` or ``name``; None when the account does not exist."""
        if by not in {"id", "name"}:
            raise ValueError(f"unsupported key {by!r}")
        try:
            resp = self._admin.invoke(
                "GetAccount",
                NS_ADMIN,
                {"account": {"by": by, "_content": value}},
                token=self._admin_auth(),
            )
        except SoapFaultError as exc:
            if exc.code == "account.NO_SUCH_ACCOUNT":
                return None
            raise
        entry = resp["account"][0]
        attrs = _attrs(entry)
        return AccountInfo(
            entry["id"], entry["name"], attrs.get("zimbraAccountStatus", [None])[0], attrs
        )

    def create_account(self, name: str, password: str, attrs: dict[str, str]) -> str:
        """``CreateAccountRequest``; returns the new account's id."""
        resp = self._admin.invoke(
            "CreateAccount",
            NS_ADMIN,
            {
                "name": name,
                "password": password,
                "a": [{"n": k, "_content": v} for k, v in attrs.items() if v],
            },
            token=self._admin_auth(),
        )
        return str(resp["account"][0]["id"])

    def domain_id(self, name: str) -> str | None:
        """Id of a Carbonio domain (``GetDomainRequest``); None when it does not exist."""
        try:
            resp = self._admin.invoke(
                "GetDomain",
                NS_ADMIN,
                {"domain": {"by": "name", "_content": name}, "attrs": "zimbraId"},
                token=self._admin_auth(),
            )
        except SoapFaultError as exc:
            if exc.code == "account.NO_SUCH_DOMAIN":
                return None
            raise
        return str(resp["domain"][0]["id"])

    def update_blocked_senders(self, domain_id: str, add: list[str], remove: list[str]) -> None:
        """Add / remove values of ``amavisBlacklistSender`` on a domain (``+`` / ``-``)."""
        attrs = [{"n": "+amavisBlacklistSender", "_content": v} for v in add]
        attrs += [{"n": "-amavisBlacklistSender", "_content": v} for v in remove]
        if attrs:
            self._admin.invoke(
                "ModifyDomain", NS_ADMIN, {"id": domain_id, "a": attrs}, token=self._admin_auth()
            )

    def delete_account(self, account_id: str) -> None:
        """Delete the account and its mailbox from Carbonio (``DeleteAccountRequest``)."""
        self._admin.invoke("DeleteAccount", NS_ADMIN, {"id": account_id}, token=self._admin_auth())

    def get_account(self, account_id: str) -> AccountInfo:
        """All attributes of one account (``GetAccountRequest``), for recreating it later."""
        resp = self._admin.invoke(
            "GetAccount",
            NS_ADMIN,
            {"account": {"by": "id", "_content": account_id}},
            token=self._admin_auth(),
        )
        entry = resp["account"][0]
        attrs = _attrs(entry)
        return AccountInfo(
            entry["id"], entry["name"], attrs.get("zimbraAccountStatus", [None])[0], attrs
        )

    # ------------------------------------------------------------------ mailbox content

    def sync(self, account_id: str, token: str | None, change_limit: int = 1000) -> SyncDelta:
        """One page of changes since ``token`` (``SyncRequest``); None = initial full sync.

        Raises :class:`SoapFaultError` with code :data:`MUST_RESYNC` when the token is too old;
        the caller then falls back to a full backup of the account.
        """
        body: dict[str, Any] = {"typed": 1, "changeLimit": change_limit, "deleteLimit": 10_000}
        if token:
            body["token"] = token
        resp = self._mail.invoke("Sync", NS_MAIL, body, token=self.delegate(account_id))
        delta = SyncDelta(token=str(resp["token"]), more=bool(resp.get("more")))
        for root in resp.get("folder", []):
            self._walk_folders(root, None, "", delta)
        for element, kind in ITEM_ELEMENTS.items():
            for item in resp.get(element, []):
                delta.changed.append((kind, int(item["id"]), int(item.get("l", 0)), item))
        for deleted in resp.get("deleted", []):
            delta.deleted.extend(_ids(deleted.get("ids")))
        return delta

    def _walk_folders(
        self, folder: dict[str, Any], parent: int | None, prefix: str, delta: SyncDelta
    ) -> None:
        fid = int(folder["id"])
        name = folder.get("name", "")
        path = f"{prefix}/{name}" if name and fid != 1 else prefix or "/"
        if fid != 1:  # 1 is the invisible root
            meta = {k: v for k, v in folder.items() if k not in ITEM_ELEMENTS and k != "folder"}
            delta.folders.append(FolderInfo(fid, parent, path, folder.get("view", "message"), meta))
        # Initial sync lists the ids contained in each folder as {"m": [{"ids": "1,2"}]}.
        for element, kind in ITEM_ELEMENTS.items():
            for group in folder.get(element, []):
                for item_id in _ids(group.get("ids")):
                    delta.changed.append((kind, item_id, fid, {}))
        for child in folder.get("folder", []):
            self._walk_folders(child, fid, path if fid != 1 else "", delta)

    def get_folders(self, account_id: str) -> list[FolderInfo]:
        """Current folder tree of a mailbox (``GetFolderRequest`` from the root)."""
        resp = self._mail.invoke(
            "GetFolder", NS_MAIL, {"folder": {"l": "1"}}, token=self.delegate(account_id)
        )
        out: list[FolderInfo] = []

        def walk(folder: dict[str, Any], parent: int | None, prefix: str) -> None:
            fid = int(folder["id"])
            name = folder.get("name", "")
            path = f"{prefix}/{name}" if fid != 1 else ""
            if fid != 1:
                out.append(FolderInfo(fid, parent, path, folder.get("view", "message"), {}))
            for child in folder.get("folder", []):
                walk(child, fid, path)

        for root in resp.get("folder", []):
            walk(root, None, "")
        return out

    def fetch_raw(self, account_name: str, account_id: str, item_id: int) -> bytes:
        """Raw content of one item via REST (``/home/<account>/?id=<n>``)."""
        response = self._rest.get(
            f"/home/{account_name}/",
            params={"id": item_id},
            headers={"Cookie": f"ZM_AUTH_TOKEN={self.delegate(account_id)}"},
        )
        if response.status_code == 404:
            raise SoapFaultError("mail.NO_SUCH_ITEM", f"item {item_id} not found")
        response.raise_for_status()
        return response.content

    def get_item_meta(self, account_id: str, item_id: int) -> dict[str, Any]:
        """Flags, tags, folder, dates of one item (``GetItemRequest``)."""
        resp = self._mail.invoke(
            "GetItem", NS_MAIL, {"item": {"id": str(item_id)}}, token=self.delegate(account_id)
        )
        for element in ITEM_ELEMENTS:
            if resp.get(element):
                item: dict[str, Any] = resp[element][0]
                return item
        raise SoapFaultError("mail.NO_SUCH_ITEM", f"item {item_id} not found")

    def messages_meta(self, account_id: str, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Flags, tags, folder, dates of many messages at once (``GetMsgMetadataRequest``)."""
        if not item_ids:
            return {}
        resp = self._mail.invoke(
            "GetMsgMetadata",
            NS_MAIL,
            {"m": {"ids": ",".join(str(i) for i in item_ids)}},
            token=self.delegate(account_id),
        )
        return {int(m["id"]): m for m in resp.get("m", []) if "id" in m}

    def has_message_id(self, account_id: str, folder_id: int, message_id: str) -> bool:
        """True if the folder already holds a message with this Message-ID header.

        ``SearchRequest`` with the ``msgid:`` operator; used by migrations to avoid
        duplicates when a folder has no incremental state.
        """
        escaped = message_id.replace("\\", "\\\\").replace('"', '\\"')
        resp = self._mail.invoke(
            "Search",
            NS_MAIL,
            {"query": f'inid:{folder_id} msgid:"{escaped}"', "types": "message", "limit": 1},
            token=self.delegate(account_id),
        )
        return bool(resp.get("m"))

    def search_messages(
        self, account_id: str, query: str, *, offset: int = 0, limit: int = 500
    ) -> tuple[list[dict[str, Any]], bool]:
        """One page of messages matching ``query`` (``SearchRequest``, oldest first).

        Each hit carries ``id``, ``l`` (folder), ``f`` (flags), ``d`` (date, ms) and ``s``
        (size). Returns ``(hits, more)``.
        """
        resp = self._mail.invoke(
            "Search",
            NS_MAIL,
            {
                "query": query,
                "types": "message",
                "sortBy": "dateAsc",
                "offset": offset,
                "limit": limit,
            },
            token=self.delegate(account_id),
        )
        return list(resp.get("m", [])), bool(resp.get("more"))

    def delete_messages(self, account_id: str, item_ids: list[int]) -> None:
        """Delete messages for good (``MsgActionRequest op=delete``, not via the Trash)."""
        if not item_ids:
            return
        self._mail.invoke(
            "MsgAction",
            NS_MAIL,
            {"action": {"op": "delete", "id": ",".join(str(i) for i in item_ids)}},
            token=self.delegate(account_id),
        )

    def item_action(
        self, account_id: str, item_ids: list[int], op: str, **attrs: str | int
    ) -> None:
        """``ItemActionRequest`` on several items at once: ``move`` (``l``), ``update``
        (``f`` flags, ``tn`` tag names) or ``delete`` (for good, not via the Trash)."""
        if op not in {"move", "update", "delete"}:
            raise ValueError(f"unsupported item action {op!r}")
        if not item_ids:
            return
        self._mail.invoke(
            "ItemAction",
            NS_MAIL,
            {"action": {"op": op, "id": ",".join(str(i) for i in item_ids), **attrs}},
            token=self.delegate(account_id),
        )

    def empty_folder(self, account_id: str, folder_id: int) -> None:
        """Delete every item of a folder, keeping the folder (``FolderAction op=empty``)."""
        self._mail.invoke(
            "FolderAction",
            NS_MAIL,
            {"action": {"op": "empty", "id": str(folder_id), "recursive": 0}},
            token=self.delegate(account_id),
        )

    # ------------------------------------------------------------------ restore primitives

    def create_folder(
        self, account_id: str, parent_id: int, name: str, view: str = "message"
    ) -> int:
        resp = self._mail.invoke(
            "CreateFolder",
            NS_MAIL,
            {"folder": {"l": str(parent_id), "name": name, "view": view, "fie": 1}},
            token=self.delegate(account_id),
        )
        return int(resp["folder"][0]["id"])

    def delete_folder(self, account_id: str, folder_id: int) -> None:
        """Delete a folder and everything in it, for good (``FolderActionRequest op=delete``)."""
        self._mail.invoke(
            "FolderAction",
            NS_MAIL,
            {"action": {"op": "delete", "id": str(folder_id)}},
            token=self.delegate(account_id),
        )

    def add_message(
        self,
        account_id: str,
        folder_id: int,
        raw: bytes,
        *,
        flags: str = "",
        tags: str = "",
        received_ms: int | None = None,
    ) -> int:
        """Inject a raw RFC 5322 message (``AddMsgRequest``), keeping flags and tags.

        UTF-8/ASCII messages are sent inline; anything else (8-bit legacy charsets) is first
        uploaded byte-for-byte to ``/service/upload?fmt=raw`` and referenced by its ``aid``.
        If the upload servlet refuses, the message is downgraded to 7 bits (same content,
        base64 / RFC 2047 transfer encoding, see :mod:`.mime7bit`) and sent inline.
        """
        msg: dict[str, Any] = {"l": str(folder_id), "f": flags, "tn": tags}
        if received_ms is not None:
            msg["d"] = received_ms
        try:
            text: str | None = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        token = self.delegate(account_id)
        if text is not None and len(raw) <= min(self._inline_max, INLINE_MAX):
            try:
                resp = self._mail.invoke(
                    "AddMsg", NS_MAIL, {"m": {**msg, "content": {"_content": text}}}, token=token
                )
                return int(resp["m"][0]["id"])
            except SoapFaultError as exc:
                if "inline message too large" not in str(exc).lower():
                    raise
                # Remember Carbonio's limit; this message and bigger ones go by upload.
                self._inline_max = min(self._inline_max, len(raw) - 1)
                log.info("inline limit of Carbonio below %s bytes: upload used", len(raw))
        # 8-bit content, or a big message: inline JSON would inflate it past Carbonio's
        # limits, while the upload sends it as is.
        try:
            msg["aid"] = self.upload(account_id, raw)
        except (SoapFaultError, httpx.HTTPError) as exc:
            inline = text if text is not None else to_7bit(raw)
            if inline is None or len(inline) > self._inline_max:
                if isinstance(exc, SoapFaultError):
                    raise
                # One message, not the whole mailbox: reported as a per-message fault.
                raise SoapFaultError("client.UPLOAD_FAILED", f"upload: {exc}"[:300]) from exc
            log.warning("upload refused (%s), message sent inline", exc)
            msg["content"] = {"_content": inline}
        resp = self._mail.invoke("AddMsg", NS_MAIL, {"m": msg}, token=token)
        return int(resp["m"][0]["id"])

    def upload(self, account_id: str, data: bytes) -> str:
        """Upload bytes to the upload servlet; returns the attachment id (``aid``).

        ASSUMPTION 🔎: answer body ``200,'null','<aid>'`` as in Zimbra's FileUploadServlet.
        """
        response = self._rest.post(
            "/service/upload",
            # lbfums: limited by zimbraFileUploadMaxSize; without it Carbonio applies the
            # (often smaller) MTA limit zimbraMtaMaxMessageSize to the upload.
            params={"fmt": "raw", "lbfums": "1"},
            content=data,
            headers={
                "Cookie": f"ZM_AUTH_TOKEN={self.delegate(account_id)}",
                "Content-Type": "message/rfc822",
                "Content-Disposition": 'attachment; filename="message.eml"',
            },
        )
        response.raise_for_status()
        parts = [p.strip().strip("'\"") for p in response.text.strip().split(",")]
        if parts and parts[0] == "413":
            raise SoapFaultError(
                "client.UPLOAD_TOO_LARGE",
                f"message of {len(data)} bytes over Carbonio's upload limit "
                "(zimbraFileUploadMaxSize)",
            )
        if len(parts) < 3 or parts[0] != "200" or not parts[2]:
            raise SoapFaultError(
                "client.BAD_RESPONSE",
                f"unexpected upload answer: {' '.join(response.text.split())[:120]!r}",
            )
        return parts[2]

    def import_rest(
        self, account_name: str, account_id: str, folder_path: str, fmt: str, data: bytes
    ) -> None:
        """Import ``.ics`` / ``.vcf`` / ``.tgz`` through REST (``POST ?fmt=…``)."""
        if fmt not in {"ics", "vcf", "tgz"}:
            raise ValueError(f"unsupported format {fmt!r}")
        segments = "/".join(quote(part, safe="") for part in folder_path.strip("/").split("/"))
        response = self._rest.post(
            f"/home/{quote(account_name, safe='@')}/{segments}",
            params={"fmt": fmt, "resolve": "skip"},
            content=data,
            headers={"Cookie": f"ZM_AUTH_TOKEN={self.delegate(account_id)}"},
        )
        response.raise_for_status()
