"""Shared test settings."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_api_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent waits for an unreachable server; tests must not."""
    import carborewind_agent.api_client as api_client

    monkeypatch.setattr(api_client, "RETRY_DELAYS", (0, 0))


@pytest.fixture(autouse=True)
def _plenty_of_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memory back-off must not depend on the machine running the tests."""
    import carborewind_agent.throttle as throttle

    monkeypatch.setattr(throttle, "available_memory", lambda *a: 0.5)
