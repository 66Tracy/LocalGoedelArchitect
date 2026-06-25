"""Logging utilities - UTF-8 file + ASCII-safe console.

Concurrency-safe per-run log routing
--------------------------------------
When the benchmark runner executes N problems in parallel with a
ThreadPoolExecutor, naively adding a FileHandler per Pipeline to the shared
``local_goedel`` logger would cross-contaminate every run.log file (each
handler sees all records from all threads) and duplicate console lines N
times.

The fix uses a single ``RoutingFileHandler`` installed ONCE on the
``local_goedel`` logger.  It reads a ``contextvars.ContextVar`` to decide
which file to write to.  Because ContextVars are *thread-local-inherited*
(each new thread gets a copy of the creator's value, which is ``None``), each
worker thread sets its own copy at the start of a run without affecting other
threads.  The handler skips the record when the var is ``None`` (e.g. when
logging from the main thread without an active run).

Usage pattern in pipeline.run / run_one::

    from local_goedel.logging_utils import current_run_logfile

    token = current_run_logfile.set(str(artifacts.log_path()))
    try:
        ...  # all logging inside here goes to the run's own run.log
    finally:
        current_run_logfile.reset(token)
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from pathlib import Path


# ---------------------------------------------------------------------------
# Module-level ContextVar: set to the path of the current run's log file.
# None means "no active run log" (records are silently dropped by the router).
# ---------------------------------------------------------------------------
current_run_logfile: ContextVar[str | None] = ContextVar(
    "current_run_logfile", default=None
)


def _safe_str(s: str) -> str:
    """Convert string to ASCII-safe representation."""
    return s.encode("ascii", errors="backslashreplace").decode("ascii")


class AsciiSafeFormatter(logging.Formatter):
    """Formatter that strips/escapes non-ASCII to avoid GBK errors on Windows."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return _safe_str(original)


class RoutingFileHandler(logging.Handler):
    """A single handler that routes each log record to the current run's file.

    It reads ``current_run_logfile`` on every emit().  If the var is ``None``
    the record is silently discarded (no active run context).  Otherwise it
    opens (or reuses) the appropriate FileHandler.

    FileHandler caching: we keep a dict of path → FileHandler so that we
    don't open/close the same file on every log call.  Handlers are closed
    when ``close()`` is called (at process exit or test teardown).  For a
    thousands-problem benchmark the dict can grow, but each entry is just an
    open file handle, which is acceptable.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._handlers: dict[str, logging.FileHandler] = {}
        self._fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _get_handler(self, path: str) -> logging.FileHandler:
        if path not in self._handlers:
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(self._fmt)
            self._handlers[path] = fh
        return self._handlers[path]

    def emit(self, record: logging.LogRecord) -> None:
        path = current_run_logfile.get()
        if path is None:
            return
        # Ensure parent directory exists (make_run_id creates it, but be safe).
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            handler = self._get_handler(path)
            handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        for fh in self._handlers.values():
            fh.close()
        self._handlers.clear()
        super().close()


# Module-level singleton so we install it only once.
_routing_handler: RoutingFileHandler | None = None


def _ensure_routing_handler(logger: logging.Logger) -> None:
    """Install the routing handler on *logger* if not already present."""
    global _routing_handler
    # Guard: only add if not already there (idempotent).
    for h in logger.handlers:
        if isinstance(h, RoutingFileHandler):
            _routing_handler = h
            return
    _routing_handler = RoutingFileHandler()
    logger.addHandler(_routing_handler)


def get_logger(name: str, log_dir: str | Path | None = None) -> logging.Logger:
    """Get a logger writing UTF-8 to file + ASCII-safe to console.

    The first call on the root ``local_goedel`` logger also installs the
    module-level ``RoutingFileHandler`` so that per-run log routing works
    under concurrent execution without duplicating console lines.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured; ensure the routing handler is present on the
        # root local_goedel logger even if this call is for a child logger.
        root_lg = logging.getLogger("local_goedel")
        _ensure_routing_handler(root_lg)
        return logger

    logger.setLevel(logging.DEBUG)

    # File handler - full UTF-8 (global log, not the per-run routing handler)
    if log_dir is None:
        log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "local_goedel.log"

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    # Console handler - ASCII-safe (one per logger, no duplication)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = AsciiSafeFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # Install the routing handler on the root local_goedel logger.
    root_lg = logging.getLogger("local_goedel")
    _ensure_routing_handler(root_lg)

    return logger
