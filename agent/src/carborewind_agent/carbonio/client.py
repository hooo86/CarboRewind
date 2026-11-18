"""High-level Carbonio CE operations used by backup and restore.

Every method maps to one documented Carbonio/Zimbra API call; names are listed in
``docs/carbonio-integration.md`` together with the CE vs Advanced notes and the assumptions
still to verify (🔎). Edition-specific code stays behind this class so that an
``Advanced`` provider (native Backup module) can be added later without touching callers.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .soap import NS_ADMIN, NS_MAIL, SoapClient, SoapFaultError, content

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
        """
        msg: dict[str, Any] = {"l": str(folder_id), "f": flags, "tn": tags}
        try:
            msg["content"] = {"_content": raw.decode("utf-8")}
        except UnicodeDecodeError:
            msg["aid"] = self.upload(account_id, raw)
        if received_ms is not None:
            msg["d"] = received_ms
        resp = self._mail.invoke("AddMsg", NS_MAIL, {"m": msg}, token=self.delegate(account_id))
        return int(resp["m"][0]["id"])

    def upload(self, account_id: str, data: bytes) -> str:
        """Upload bytes to the upload servlet; returns the attachment id (``aid``).

        ASSUMPTION 🔎: answer body ``200,'null','<aid>'`` as in Zimbra's FileUploadServlet.
        """
        response = self._rest.post(
            "/service/upload",
            params={"fmt": "raw"},
            content=data,
            headers={
                "Cookie": f"ZM_AUTH_TOKEN={self.delegate(account_id)}",
                "Content-Type": "message/rfc822",
            },
        )
        response.raise_for_status()
        parts = [p.strip().strip("'\"") for p in response.text.strip().split(",")]
        if len(parts) < 3 or parts[0] != "200" or not parts[2]:
            raise SoapFaultError("client.BAD_RESPONSE", "unexpected upload answer")
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
