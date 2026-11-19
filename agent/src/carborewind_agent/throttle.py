"""Load limiting for hot backups: bandwidth token bucket and system-load back-off."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable


class Throttle:
    """Thread-safe token bucket on bytes, plus a pause while the load average is too high.

    ``max_bytes_per_second`` 0 disables the bandwidth limit; ``max_load`` 0 disables the
    load back-off (load average per CPU, 1-minute window).
    """

    def __init__(
        self,
        max_bytes_per_second: int = 0,
        max_load: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        loadavg: Callable[[], float] | None = None,
    ) -> None:
        self._rate = max_bytes_per_second
        self._max_load = max_load
        self._clock = clock
        self._sleep = sleep
        self._loadavg = loadavg or (lambda: os.getloadavg()[0] / (os.cpu_count() or 1))
        self._lock = threading.Lock()
        self._allowance = float(max_bytes_per_second)
        self._last = clock()
        self.waited = 0.0  # total seconds spent waiting (exported as a metric)

    @classmethod
    def from_mbps(cls, mbps: int, max_load: float = 0.0) -> Throttle:
        return cls(mbps * 125_000, max_load)

    def configure(self, mbps: int, max_load: float) -> None:
        """New limits, applied to the next transfers (settings changed in the interface)."""
        with self._lock:
            self._rate = max(0, int(mbps)) * 125_000
            self._allowance = float(self._rate)
            self._last = self._clock()
            self._max_load = max(0.0, float(max_load))

    def consume(self, nbytes: int) -> None:
        """Block until ``nbytes`` may be transferred."""
        self._wait_for_load()
        if self._rate <= 0:
            return
        with self._lock:
            now = self._clock()
            self._allowance = min(
                float(self._rate), self._allowance + (now - self._last) * self._rate
            )
            self._last = now
            self._allowance -= nbytes
            deficit = -self._allowance
        if deficit > 0:
            delay = deficit / self._rate
            self.waited += delay
            self._sleep(delay)

    def _wait_for_load(self, poll: float = 5.0, max_wait: float = 300.0) -> None:
        if self._max_load <= 0:
            return
        waited = 0.0
        while self._loadavg() > self._max_load and waited < max_wait:
            self._sleep(poll)
            waited += poll
        self.waited += waited
