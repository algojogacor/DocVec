from __future__ import annotations

import logging
import threading
from collections import deque

_MAX_LOG_LINES = 500
_HANDLER_NAME = "docvec-memory-log"


class DequeLogHandler(logging.Handler):
    def __init__(self, max_lines: int = _MAX_LOG_LINES) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._lines.append(line)

    def lines(self, limit: int | None = None) -> list[str]:
        with self._lock:
            lines = list(self._lines)
        if limit is None or limit >= len(lines):
            return lines
        return lines[-max(0, limit) :]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


_memory_handler: DequeLogHandler | None = None


def configure_docvec_logging() -> DequeLogHandler:
    global _memory_handler
    if _memory_handler is not None:
        return _memory_handler

    handler = DequeLogHandler(max_lines=_MAX_LOG_LINES)
    handler.name = _HANDLER_NAME
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    if not any(getattr(existing, "name", "") == _HANDLER_NAME for existing in root.handlers):
        root.addHandler(handler)
    logging.getLogger("docvec").setLevel(logging.DEBUG)
    _memory_handler = handler
    return handler


def recent_log_lines(limit: int = _MAX_LOG_LINES) -> list[str]:
    handler = configure_docvec_logging()
    return handler.lines(limit=max(0, min(limit, _MAX_LOG_LINES)))


def clear_log_lines() -> None:
    configure_docvec_logging().clear()
