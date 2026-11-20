"""Failed logins and IPs blocked by Carbonio, read from mailbox.log and the MTA log."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.authlog import MAX_ACCOUNTS, AuthAggregator, mailbox_time
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.mailwatch import MailMonitor

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings
from .test_maillog import FakeServer

NOW = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
HOUR = "2026-11-19T10:00:00+00:00"

FAILED = (
    "2026-11-19 10:05:01,123 INFO  [qtp1-22:https://mail.lnhc.dz/service/soap/AuthRequest] "
    "[name=ana@lnhc.dz;oip=203.0.113.9;ip=192.168.8.40;ua=zclient/10;] SoapEngine - "
    "handler exception: authentication failed for [ana@lnhc.dz], invalid password"
)
FAILED_IMAP = (
    "2026-11-19 10:06:00,001 INFO  [ImapSSLServer-3] [ip=198.51.100.7;] imap - "
    "authentication failed for [Ben@lnhc.dz]"
)
LOOPBACK = (
    "2026-11-19 10:06:00,001 INFO  [ImapServer-1] [ip=127.0.0.1;] imap - "
    "authentication failed for [x@lnhc.dz]"
)
SUSPENDED = (
    "2026-11-19 10:07:30,500 INFO  [qtp1-30] [] misc - Access to IP 203.0.113.9 suspended, "
    "for repeated failed login."
)
SASL = (
    "Nov 19 10:08:00 mail postfix/submission/smtpd[4242]: warning: unknown[203.0.113.9]: "
    "SASL LOGIN authentication failed: UGFzc3dvcmQ6"
)
SASL_USER = (
    "Nov 19 10:09:00 mail postfix/smtpd[4243]: warning: host.example[2001:db8::5]: "
    "SASL PLAIN authentication failed: , sasl_username=carl@lnhc.dz"
)


def test_counts_failures_and_suspensions_per_ip() -> None:
    agg = AuthAggregator(tz=UTC)
    for line in (FAILED, FAILED_IMAP, LOOPBACK, SUSPENDED, "noise", "2026-13-40 99:99:99 x"):
        agg.feed_mailbox(line)
    agg.feed_mailbox("2026-13-40 99:99:99 authentication failed for [x]")  # bad date
    for line in (SASL, SASL_USER, "Nov 19 10:09:00 mail postfix/smtpd[1]: connect from x"):
        agg.feed_mta(line, NOW)
    [hour] = agg.payload()
    assert hour["hour"] == HOUR
    ips = hour["ips"]
    assert set(ips) == {"203.0.113.9", "198.51.100.7", "2001:db8::5"}  # loopback ignored
    attacker = ips["203.0.113.9"]
    assert attacker["failed_mailbox"] == 1 and attacker["failed_smtp"] == 1
    assert attacker["suspended"] == 1
    assert attacker["last_suspended"] == "2026-11-19T10:07:30+00:00"
    assert attacker["accounts"] == ["ana@lnhc.dz"]  # oip wins over the proxy's ip
    assert ips["198.51.100.7"]["accounts"] == ["ben@lnhc.dz"]
    assert ips["2001:db8::5"] == {
        "failed_mailbox": 0,
        "failed_smtp": 1,
        "suspended": 0,
        "last_suspended": None,
        "accounts": ["carl@lnhc.dz"],
    }


def test_bounds_prune_and_state() -> None:
    agg = AuthAggregator(tz=UTC)
    for n in range(MAX_ACCOUNTS + 5):
        agg.feed_mailbox(FAILED.replace("[ana@lnhc.dz]", f"[u{n}@lnhc.dz]"))
    assert len(agg.hours[HOUR]["203.0.113.9"].accounts) == MAX_ACCOUNTS
    copy = AuthAggregator(tz=UTC)
    copy.restore(json.loads(json.dumps(agg.to_json())))
    assert copy.hours[HOUR]["203.0.113.9"].failed_mailbox == MAX_ACCOUNTS + 5
    agg.prune(NOW + timedelta(days=3))
    assert agg.hours == {} and agg.payload() == []
    assert mailbox_time("2026-02-30 10:00:00,000 x", UTC) is None


def test_ip_limit_per_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("carborewind_agent.authlog.MAX_IPS", 1)
    agg = AuthAggregator(tz=UTC)
    agg.feed_mailbox(FAILED)
    agg.feed_mailbox(FAILED_IMAP)
    assert list(agg.hours[HOUR]) == ["203.0.113.9"]


class IpServer(FakeServer):
    def __init__(self) -> None:
        super().__init__()
        self.ip_actions: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        if request.url.path.endswith("/mail-stats") and not self.down:
            pending, self.ip_actions = self.ip_actions, []
            return httpx.Response(200, json={**response.json(), "ip_actions": pending})
        return response


@pytest.fixture
def monitor(tmp_path: Path) -> tuple[MailMonitor, IpServer, Path, Path]:
    fake = FakeCarbonio()
    fake.add_mailbox("ana@lnhc.dz")
    client: CarbonioClient = build_client(_settings(tmp_path), transport=fake.transport())
    server = IpServer()
    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(server.handler))
    mta = tmp_path / "carbonio.log"
    mta.write_text("")
    mailbox = tmp_path / "mailbox.log"
    mailbox.write_text("")
    mon = MailMonitor(
        AgentApi(_settings(tmp_path), http=http),
        client,
        [mta],
        tmp_path / "state" / "mail.json",
        mailbox_log=mailbox,
    )
    mon.auth.tz = UTC
    mon.fake = fake  # type: ignore[attr-defined]
    return mon, server, mta, mailbox


def test_monitor_reports_failed_logins(monitor: tuple[Any, ...], tmp_path: Path) -> None:
    mon, server, mta, mailbox = monitor
    mta.write_text(SASL + "\n")
    mailbox.write_text(FAILED + "\n" + SUSPENDED + "\n")
    mon.tick(NOW)
    _, body = server.calls[-1]
    assert body["security"][0]["ips"]["203.0.113.9"]["suspended"] == 1
    assert body["security"][0]["ips"]["203.0.113.9"]["failed_smtp"] == 1
    assert body["mailbox_log"] == {"path": str(mailbox), "exists": True, "readable": True}
    mon.tick(NOW)
    assert "security" not in server.calls[-1][1]  # nothing new
    # The state survives a restart: nothing is read or sent twice.
    again = MailMonitor(
        mon._api, mon._carbonio, [mta], tmp_path / "state" / "mail.json", mailbox_log=mailbox
    )
    assert again.mailbox_reader is not None and again.mailbox_reader.read_new() == []
    assert again.auth.hours[HOUR]["203.0.113.9"].suspended == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_monitor_survives_an_unreadable_mailbox_log(monitor: tuple[Any, ...]) -> None:
    mon, server, _mta, mailbox = monitor
    mailbox.write_text(FAILED + "\n")
    mailbox.chmod(0)
    mon.tick(NOW)
    mon.tick(NOW)
    assert mon._mailbox_denied and "security" not in server.calls[-1][1]
    assert server.calls[-1][1]["mailbox_log"]["readable"] is False


def test_monitor_reports_the_login_filter_and_trusts_ips(monitor: tuple[Any, ...]) -> None:
    mon, server, _mta, _mailbox = monitor
    fake = mon.fake
    mon.tick(NOW)
    assert server.calls[-1][1]["login_filter"] == {
        "safe_ips": ["192.168.8.0/24"],
        "max_failed": 10,
        "block_minutes": 15,
        "task_minutes": 5,
    }
    mon.tick(NOW)
    assert "login_filter" not in server.calls[-1][1]  # read every few minutes only
    server.ip_actions = [
        {"id": "i1", "action": "trust", "ip": "203.0.113.9"},
        {"id": "i2", "action": "untrust", "ip": "198.51.100.1"},  # not in the list
        {"id": "i3", "action": "explode", "ip": "1.2.3.4"},
    ]
    mon.tick(NOW)
    assert fake.config["zimbraHttpThrottleSafeIPs"] == ["192.168.8.0/24", "203.0.113.9"]
    done = {p: b for p, b in server.calls if p.startswith("/ip-actions/")}
    assert done["/ip-actions/i1/done"] == {"error": None}
    assert "INVALID_REQUEST" in done["/ip-actions/i2/done"]["error"]
    assert "unknown action" in done["/ip-actions/i3/done"]["error"]
    mon.tick(NOW)  # the new list is reported at once
    assert "203.0.113.9" in server.calls[-1][1]["login_filter"]["safe_ips"]
    server.ip_actions = [{"id": "i4", "action": "untrust", "ip": "203.0.113.9"}]
    mon.tick(NOW)
    assert fake.config["zimbraHttpThrottleSafeIPs"] == ["192.168.8.0/24"]
