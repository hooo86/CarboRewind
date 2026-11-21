"""Mail queue actions asked in the interface (Supervision → File d'attente).

The agent lists the messages waiting in the deferred and hold queues with each services
report (every minute). An administrator may then **requeue** (try again now), **hold**,
**release** or **delete** chosen messages, or **flush** the whole queue. Actions wait in
``agent.storage["queue"]["pending"]`` until the next report, which hands them to the agent
in its answer; the agent applies them at once and reports each outcome (kept in
``results``, the last :data:`KEEP_RESULTS`). An action handed out but not reported within
:data:`RESEND_AFTER` is handed out again (agent restarted in between).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db.models import ActorType, Agent, Outcome

OPS = ("requeue", "hold", "release", "delete", "flush")
QUEUES = ("deferred", "hold", "active", "incoming")
MAX_IDS = 100
KEEP_RESULTS = 20
RESEND_AFTER = timedelta(minutes=5)
EXPIRE_AFTER = timedelta(hours=1)


def _state(agent: Agent) -> dict[str, Any]:
    queue = dict((agent.storage or {}).get("queue") or {})
    queue.setdefault("pending", [])
    queue.setdefault("results", [])
    return queue


def _save(agent: Agent, state: dict[str, Any]) -> None:
    agent.storage = {**(agent.storage or {}), "queue": state}


def request(
    agent: Agent,
    op: str,
    queue: str | None,
    ids: list[str],
    server: str | None,
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Queue one action for the agent; returns it."""
    action = {
        "id": uuid.uuid4().hex,
        "op": op,
        "queue": queue,
        "ids": ids,
        "server": server,
        "by": actor,
        "at": now.isoformat(),
        "sent_at": None,
    }
    state = _state(agent)
    state["pending"] = [*state["pending"], action]
    _save(agent, state)
    return action


def deliver(agent: Agent, now: datetime) -> list[dict[str, Any]]:
    """The actions to hand to the agent in this report's answer."""
    state = _state(agent)
    out: list[dict[str, Any]] = []
    pending = []
    for action in state["pending"]:
        if now - datetime.fromisoformat(action["at"]) > EXPIRE_AFTER:
            state["results"] = [
                {**action, "done_at": now.isoformat(), "error": "the agent did not apply it"},
                *state["results"],
            ][:KEEP_RESULTS]
            continue
        sent = action.get("sent_at")
        if sent is None or now - datetime.fromisoformat(sent) > RESEND_AFTER:
            action = {**action, "sent_at": now.isoformat()}
            out.append(
                {k: action[k] for k in ("id", "op", "queue", "ids", "server") if action.get(k)}
            )
        pending.append(action)
    if out or len(pending) != len(state["pending"]):
        state["pending"] = pending
        _save(agent, state)
    return out


async def done(
    db: AsyncSession, agent: Agent, action_id: str, error: str | None, now: datetime
) -> None:
    """The agent applied an action (or could not): keep the outcome, audit it."""
    state = _state(agent)
    action = next((a for a in state["pending"] if a["id"] == action_id), None)
    if action is None:
        return  # already reported (idempotent) or unknown
    state["pending"] = [a for a in state["pending"] if a["id"] != action_id]
    state["results"] = [
        {**action, "done_at": now.isoformat(), "error": error},
        *state["results"],
    ][:KEEP_RESULTS]
    _save(agent, state)
    await audit.record(
        db,
        action=f"mail_queue.{action['op']}.done",
        outcome=Outcome.FAILURE if error else Outcome.SUCCESS,
        actor_type=ActorType.AGENT,
        actor_name=agent.name,
        target_type="mail_queue",
        target_id=action.get("queue") or "all",
        details={"ids": action.get("ids"), "asked_by": action.get("by"), "error": error},
    )


def view(agent: Agent) -> dict[str, Any]:
    """What the interface shows for one agent."""
    services = agent.services or {}
    state = _state(agent)
    return {
        "agent_id": str(agent.id),
        "agent": agent.name,
        "checked_at": services.get("checked_at"),
        "queue": services.get("queue"),
        "queue_total": services.get("queue_total"),
        "details": services.get("queue_items"),  # None: agent too old to list messages
        "pending": [
            {k: a.get(k) for k in ("id", "op", "queue", "ids", "by", "at", "sent_at")}
            for a in state["pending"]
        ],
        "results": state["results"],
    }
