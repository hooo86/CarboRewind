"""Structured JSON logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes present on every LogRecord; anything else was passed through ``extra=``.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


LOG_KEY = "carborewind:logs:{process}"
KEEP_LINES = 5000
# Too chatty to be useful in the interface: every HTTP request.
_SKIPPED = ("uvicorn.access", "httpx")


class _RedisSink(logging.Handler):
    """Writes formatted records to a capped Redis list (runs in the listener's thread)."""

    def __init__(self, url: str, process: str) -> None:
        super().__init__()
        import redis

        self._redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self._key = LOG_KEY.format(process=process)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_SKIPPED):
            return
        try:
            pipe = self._redis.pipeline()
            pipe.lpush(self._key, self.format(record))
            pipe.ltrim(self._key, 0, KEEP_LINES - 1)
            pipe.execute()
        except Exception:  # noqa: S110 - losing a log line must never break the service
            pass


_listener: logging.handlers.QueueListener | None = None


def configure_logging(level: str, redis_url: str | None = None, process: str = "api") -> None:
    """Install the JSON formatter on the root logger (idempotent).

    With ``redis_url``, the last lines are also kept in Redis (``carborewind:logs:<process>``)
    so that administrators can read them in the interface; written from a background thread,
    a slow or absent Redis never delays the service.
    """
    global _listener
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    handlers: list[logging.Handler] = [handler]
    if _listener is not None:
        _listener.stop()
        _listener = None
    if redis_url:
        sink = _RedisSink(redis_url, process)
        sink.setFormatter(JsonFormatter())
        buffer: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        _listener = logging.handlers.QueueListener(buffer, sink)
        _listener.start()
        handlers.append(logging.handlers.QueueHandler(buffer))
    root.handlers[:] = handlers
    root.setLevel(level)
