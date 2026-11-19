"""Mail traffic statistics from Postfix/Amavis log lines, and the watcher thread."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from carborewind_agent.api_client import AgentApi
from carborewind_agent.carbonio import CarbonioClient
from carborewind_agent.carbonio.factory import build_client
from carborewind_agent.maillog import (
    Aggregator,
    LogReader,
    load_state,
    parse_time,
    save_state,
)
from carborewind_agent.mailwatch import MailMonitor

from .fake_carbonio import FakeCarbonio
from .test_carbonio_client import _settings

NOW = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)
LOCAL = {
    "ana@lnhc.dz": "ana@lnhc.dz",
    "contact@lnhc.dz": "ana@lnhc.dz",  # alias
    "ben@lnhc.dz": "ben@lnhc.dz",
}


def sent(qid: str, sender: str, nrcpt: int, msgid: str, t: str = "Nov 19 10:12:01") -> list[str]:
    return [
        f"{t} mail postfix/submission/smtpd[11]: {qid}: client=pc[10.0.0.5], "
        f"sasl_method=PLAIN, sasl_username={sender}",
        f"{t} mail postfix/cleanup[12]: {qid}: message-id=<{msgid}>",
        f"{t} mail postfix/qmgr[13]: {qid}: from=<{sender}>, size=2048, nrcpt={nrcpt} "
        "(queue active)",
    ]


SPAM_IN = (
    "Nov 19 10:20:00 mail amavis[2001]: (02001-05) Blocked SPAM {DiscardedInbound,Quarantined}, "
    "[203.0.113.9]:51234 [203.0.113.9] <promo@spam.example> -> <ana@lnhc.dz>,<ben@lnhc.dz>, "
    "quarantine: spam-x, Queue-ID: AB12, Message-ID: <s1@spam.example>, Hits: 15.2, 312 ms"
)
SPAMMY_IN = (
    "2026-11-19T11:05:00.123+01:00 mail amavis[2002]: (02002-01) Passed SPAMMY "
    "{RelayedTaggedInbound}, [198.51.100.7]:4000 [198.51.100.7] <promo@spam.example> -> "
    "<contact@lnhc.dz>, Queue-ID: CD34, Hits: 5.5, size: 900, 120 ms"
)
SPAM_OUT = (
    "Nov 19 10:25:00 mail amavis[2003]: (02003-02) Blocked SPAM {DiscardedOutbound,Quarantined}, "
    "ORIGINATING LOCAL [10.0.0.5]:50000 [10.0.0.5] <ben@lnhc.dz> -> <x@gmail.com>, Hits: 20, 50 ms"
)
CLEAN = (
    "Nov 19 10:21:00 mail amavis[2004]: (02004-01) Passed CLEAN {RelayedInbound}, "
    "[192.0.2.1]:25 [192.0.2.1] <friend@ext.example> -> <ana@lnhc.dz>, Hits: -1, 80 ms"
)


def test_parse_time() -> None:
    algiers = timezone(timedelta(hours=1))
    assert parse_time("Nov 19 10:12:01 mail x", NOW, algiers) == datetime(
        2026, 11, 19, 9, 12, 1, tzinfo=UTC
    )
    january = datetime(2027, 1, 2, tzinfo=UTC)
    assert parse_time("Dec 31 23:00:00 mail x", january, UTC) == datetime(
        2026, 12, 31, 23, 0, tzinfo=UTC
    )
    assert parse_time("2026-11-19T11:05:00.123+01:00 mail x", NOW, UTC) == datetime(
        2026, 11, 19, 10, 5, 0, 123000, tzinfo=UTC
    )
    assert parse_time("2026-11-19T11:05:00Z mail", NOW, UTC) is not None
    for bad in ("garbage", "Foo 19 10:00:00 x", "Feb 30 10:00:00 x", "2026-13-40T99:00:00Z x"):
        assert parse_time(bad, NOW, UTC) is None


def test_counts_sent_mail_and_spam() -> None:
    agg = Aggregator(LOCAL, UTC)
    lines = [
        *sent("Q1", "ana@lnhc.dz", 3, "m1@lnhc.dz"),
        # The content filter re-injects the same message under a new queue id.
        "Nov 19 10:12:02 mail postfix/cleanup[12]: Q2: message-id=<m1@lnhc.dz>",
        "Nov 19 10:12:02 mail postfix/qmgr[13]: Q2: from=<ana@lnhc.dz>, size=2100, nrcpt=3 "
        "(queue active)",
        *sent("Q3", "Contact@lnhc.dz", 1, "m2@lnhc.dz"),  # alias, any case
        *sent("Q4", "outsider@ext.example", 50, "m3@ext"),  # inbound: not counted
        "Nov 19 10:13:00 mail postfix/qmgr[13]: Q5: from=<>, size=900, nrcpt=1 (queue active)",
        # qmgr without a cleanup line (log started mid-way): counted by queue id.
        "Nov 19 10:14:00 mail postfix/qmgr[13]: Q6: from=<ben@lnhc.dz>, size=10, nrcpt=40 "
        "(queue active)",
        *sent("Q7", "ana@lnhc.dz", 2, "m4@lnhc.dz", t="Nov 19 11:01:00"),
        SPAM_IN,
        SPAMMY_IN,
        SPAM_OUT,
        CLEAN,
        "Nov 19 10:30:00 mail postfix/smtpd[9]: connect from unknown[1.2.3.4]",
        "Nov 19 10:30:00 mail dovecot: imap-login: Login: user=<ana@lnhc.dz>",
        "garbage postfix/qmgr[13]: Q8: from=<ana@lnhc.dz>, size=1, nrcpt=1",
    ]
    for line in lines:
        agg.feed(line, NOW)
    ten = agg.buckets["2026-11-19T10:00:00+00:00"]
    eleven = agg.buckets["2026-11-19T11:00:00+00:00"]
    assert ten.sent == {"ana@lnhc.dz": [2, 4], "ben@lnhc.dz": [1, 40]}
    assert eleven.sent == {"ana@lnhc.dz": [1, 2]}
    # The SPAMMY line (to the alias) is stamped 11:05+01:00, i.e. 10:05 UTC.
    assert ten.spam_in == {"ana@lnhc.dz": 2, "ben@lnhc.dz": 1}
    assert ten.spam_out == {"ben@lnhc.dz": 1}
    assert ten.spam_senders == {
        "promo@spam.example": {"count": 2, "ips": ["203.0.113.9", "198.51.100.7"]}
    }
    assert eleven.spam_in == {}
    payload = agg.payload()
    assert [p["hour"] for p in payload] == sorted(agg.dirty)
    assert "seen" not in payload[0]
    agg.prune(NOW + timedelta(days=3))
    assert agg.buckets == {} and agg.payload() == []


def test_spam_senders_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from carborewind_agent import maillog

    monkeypatch.setattr(maillog, "MAX_SPAM_SENDERS", 2)
    agg = Aggregator(LOCAL, UTC)
    for i in range(4):
        agg.feed(SPAM_IN.replace("promo@", f"p{i}@").replace("203.0.113.9]:", f"10.0.0.{i}]:"))
    for _ in range(12):
        agg.feed(SPAM_IN.replace("[203.0.113.9]:51234", f"[10.1.1.{_}]:1"))
    bucket = next(iter(agg.buckets.values()))
    assert len(bucket.spam_senders) == 2
    agg2 = Aggregator(LOCAL, UTC)
    for i in range(12):
        agg2.feed(SPAM_IN.replace("[203.0.113.9]:51234", f"[10.1.1.{i}]:1"), NOW)
    entry = next(iter(agg2.buckets.values())).spam_senders["promo@spam.example"]
    assert entry["count"] == 12 and len(entry["ips"]) == 10


def test_reader_follows_appends_rotation_and_truncation(tmp_path: Path) -> None:
    log = tmp_path / "carbonio.log"
    log.write_text("old line 1\nold line 2\n")
    reader = LogReader(log)
    assert reader.read_new() == ["old line 1", "old line 2"]  # small file: read whole
    with open(log, "a") as fh:
        fh.write("new 1\npartial")
    assert reader.read_new() == ["new 1"]
    with open(log, "a") as fh:
        fh.write(" end\n")
    assert reader.read_new() == ["partial end"]
    # Rotation: the rest of the old file is read, then the new one from the start.
    with open(log, "a") as fh:
        fh.write("last of old\n")
    os.rename(log, tmp_path / "carbonio.log.1")
    log.write_text("first of new\n")
    assert reader.read_new() == ["last of old", "first of new"]
    log.write_text("")  # truncated in place
    log.write_text("again\n")
    reader.offset = 999
    assert reader.read_new() == ["again"]
    os.remove(log)
    assert reader.read_new() == []


def test_first_read_only_takes_the_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from carborewind_agent import maillog

    monkeypatch.setattr(maillog, "FIRST_READ", 20)
    log = tmp_path / "carbonio.log"
    log.write_text("a" * 50 + "\nline A\nline B\n")
    assert LogReader(log).read_new() == ["line A", "line B"]


def test_state_round_trip(tmp_path: Path) -> None:
    log = tmp_path / "carbonio.log"
    log.write_text("\n".join(sent("Q1", "ana@lnhc.dz", 2, "m1")) + "\n")
    reader, agg = LogReader(log), Aggregator(LOCAL, UTC)
    for line in reader.read_new():
        agg.feed(line, NOW)
    state = tmp_path / "state.json"
    save_state(state, reader, agg)
    assert oct(state.stat().st_mode)[-3:] == "600"
    reader2, agg2 = LogReader(log), Aggregator(LOCAL, UTC)
    load_state(state, reader2, agg2)
    assert reader2.offset == reader.offset and reader2.read_new() == []
    assert agg2.buckets["2026-11-19T10:00:00+00:00"].sent == {"ana@lnhc.dz": [1, 2]}
    assert agg2.dirty == agg.dirty
    load_state(tmp_path / "missing.json", LogReader(log), Aggregator())  # no crash


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/agent")
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if self.down:
            return httpx.Response(503, text="maintenance")
        if path == "/mail-stats":
            actions, self.actions = self.actions, []
            return httpx.Response(200, json={"actions": actions})
        return httpx.Response(204)


@pytest.fixture
def setup(tmp_path: Path) -> tuple[MailMonitor, FakeServer, FakeCarbonio, Path]:
    fake = FakeCarbonio()
    fake.add_mailbox("ana@lnhc.dz", zimbraMailAlias="contact@lnhc.dz")
    fake.add_mailbox("ben@lnhc.dz")
    client: CarbonioClient = build_client(_settings(tmp_path), transport=fake.transport())
    server = FakeServer()
    http = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(server.handler))
    log = tmp_path / "carbonio.log"
    log.write_text("")
    monitor = MailMonitor(
        AgentApi(_settings(tmp_path), http=http),
        client,
        [tmp_path / "missing.log", log],
        tmp_path / "state" / "mail.json",
        interval=0.01,
        protected={"Ana@lnhc.dz"},
    )
    return monitor, server, fake, log


def test_monitor_sends_counters_and_locks(setup: tuple[Any, ...]) -> None:
    monitor, server, fake, log = setup
    with open(log, "a") as fh:
        fh.write("\n".join(sent("Q1", "contact@lnhc.dz", 5, "m1")) + "\n")
    out = monitor.tick(NOW)
    assert out == {"lines": 3, "actions": 0}
    path, body = server.calls[-1]
    assert path == "/mail-stats" and body["log"] == str(log)
    assert body["hours"][0]["sent"] == {"ana@lnhc.dz": [1, 5]}
    # Nothing new: an empty report still fetches pending actions.
    ben = next(m for m in fake.mailboxes.values() if m.name == "ben@lnhc.dz")
    server.actions = [
        {"id": "a1", "action": "lock", "carbonio_id": ben.id, "email": ben.name, "reason": "x"},
        {"id": "a2", "action": "lock", "carbonio_id": "nope", "email": "?", "reason": "x"},
        {"id": "a3", "action": "explode", "carbonio_id": ben.id},
    ]
    assert monitor.tick(NOW)["actions"] == 3
    assert server.calls[-4][1]["hours"] == []
    assert ben.status == "locked"
    done = {p: b for p, b in server.calls if p.startswith("/account-actions/")}
    assert done["/account-actions/a1/done"] == {"error": None}
    assert "NO_SUCH_ACCOUNT" in done["/account-actions/a2/done"]["error"]
    assert "unknown action" in done["/account-actions/a3/done"]["error"]
    server.actions = [{"id": "a4", "action": "unlock", "carbonio_id": ben.id}]
    monitor.tick(NOW)
    assert ben.status == "active"
    ana = next(m for m in fake.mailboxes.values() if m.name == "ana@lnhc.dz")
    server.actions = [{"id": "a5", "action": "lock", "carbonio_id": ana.id, "email": ana.name}]
    monitor.tick(NOW)
    assert ana.status == "active"  # the agent's own account is never locked
    assert "protected" in server.calls[-1][1]["error"]


def test_monitor_keeps_counters_when_the_server_is_down(setup: tuple[Any, ...]) -> None:
    monitor, server, _fake, log = setup
    with open(log, "a") as fh:
        fh.write("\n".join(sent("Q1", "ana@lnhc.dz", 1, "m1")) + "\n")
    server.down = True
    stop = threading.Event()
    thread = threading.Thread(target=monitor.run, args=(stop,))
    thread.start()
    while len(server.calls) < 2:
        pass
    stop.set()
    thread.join(timeout=5)
    assert monitor.agg.dirty  # not lost: sent again once the server answers
    server.down = False
    monitor.tick(NOW)
    assert server.calls[-1][1]["hours"][0]["sent"] == {"ana@lnhc.dz": [1, 1]}
    assert not monitor.agg.dirty
