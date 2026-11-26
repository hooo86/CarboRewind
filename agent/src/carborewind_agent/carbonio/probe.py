"""``carborewind-agent carbonio probe``: check our API assumptions on a real server.

Read-only: it authenticates, lists one account, obtains a delegated token, runs an initial
``SyncRequest`` and downloads one message. Each step prints ✓ or ✗ with the error code, so an
administrator can confirm (or refute) every 🔎 assumption of docs/carbonio-integration.md on
their Carbonio CE version before the first backup. Nothing is modified on the server, and no
mail content is printed (only sizes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from .client import AccountInfo, CarbonioClient, SyncDelta
from .soap import SoapFaultError

T = TypeVar("T")


@dataclass
class ProbeReport:
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.steps)

    def render(self) -> str:
        return "\n".join(
            f"{'✓' if ok else '✗'} {name}: {detail}" for name, ok, detail in self.steps
        )


def run_probe(client: CarbonioClient, account: str | None = None) -> ProbeReport:
    report = ProbeReport()

    def step(name: str, fn: Callable[[], T], describe: Callable[[T], str]) -> T | None:
        try:
            value = fn()
        except (SoapFaultError, OSError, ValueError, KeyError) as exc:
            code = exc.code if isinstance(exc, SoapFaultError) else type(exc).__name__
            report.steps.append((name, False, code))
            return None
        report.steps.append((name, True, describe(value)))
        return value

    def pick() -> AccountInfo:
        for info in client.iter_accounts(page=50):
            if account is None or info.name == account:
                return info
        raise KeyError(account or "no account")

    target = step(
        "AuthRequest + SearchDirectoryRequest (admin, JSON SOAP)",
        pick,
        lambda a: f"account {a.name} status={a.status}",
    )
    if target is None:
        return report
    if (
        step("DelegateAuthRequest", lambda: client.delegate(target.id), lambda _: "token received")
        is None
    ):
        return report
    delta = step(
        "SyncRequest (initial, typed)",
        lambda: client.sync(target.id, None, change_limit=1),
        lambda d: f"{len(d.folders)} folders, {len(d.changed)} items, token={d.token}",
    )
    if delta is None:
        return report
    step(
        "SyncRequest (delta from token)",
        lambda: client.sync(target.id, delta.token, change_limit=1),
        lambda d: f"{len(d.changed)} changed, {len(d.deleted)} deleted",
    )
    message = _first_message(delta)
    if message is None:
        report.steps.append(("REST GET /home/<account>/?id=", True, "skipped: no message"))
        return report
    step(
        "REST GET /home/<account>/?id= (raw message)",
        lambda: client.fetch_raw(target.name, target.id, message),
        lambda raw: f"{len(raw)} bytes",
    )
    step(
        "GetItemRequest (flags, tags, folder)",
        lambda: client.get_item_meta(target.id, message),
        lambda meta: "keys: " + ",".join(sorted(meta)),
    )
    return report


def _first_message(delta: SyncDelta) -> int | None:
    return next((iid for kind, iid, _, _ in delta.changed if kind == "message"), None)


def quota_report(client: CarbonioClient, account: str) -> list[str]:
    """Every source of the quota / usage figures for one account, for diagnosis."""
    from .soap import SoapFaultError

    info = next((a for a in client.iter_accounts() if a.name == account.lower()), None)
    if info is None:
        return [f"{account}: not found by SearchDirectoryRequest"]
    lines = [f"account {info.name} (id {info.id})"]
    lines.append(
        f"  zimbraMailQuota on the account: {info.attrs.get('zimbraMailQuota', ['(none)'])[0]}"
    )
    cos_id = (info.attrs.get("zimbraCOSId") or [""])[0]
    try:
        cos = client.cos_info()
        name, quota = cos.get(cos_id, ("(default COS)", None))
        lines.append(f"  class of service: {name} {cos_id or ''}, zimbraMailQuota: {quota}")
    except SoapFaultError as exc:
        lines.append(f"  GetAllCosRequest refused: {exc}")
    try:
        usage = client.quota_usage_detail()
        lines.append(f"  GetQuotaUsageRequest: {len(usage)} account(s) listed")
        lines.append(f"    this account: (used, limit) = {usage.get(info.id, 'NOT LISTED')}")
    except SoapFaultError as exc:
        lines.append(f"  GetQuotaUsageRequest refused: {exc}")
    try:
        lines.append(f"  GetMailboxRequest size: {client.mailbox_size(info.id)} bytes")
    except SoapFaultError as exc:
        lines.append(f"  GetMailboxRequest refused: {exc}")
    return lines
