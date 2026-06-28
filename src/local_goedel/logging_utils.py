"""Logging utilities - UTF-8 file + ASCII-safe console.

Concurrency-safe per-run log routing
--------------------------------------
When the benchmark runner executes N problems in parallel with a
ThreadPoolExecutor, naively adding a FileHandler per Pipeline to the shared
``local_goedel`` logger would cross-contaminate every run.log file (each
handler sees all records from all threads) and duplicate console lines N
times.

The fix centralises ALL handlers on the single ``local_goedel`` root logger,
configured EXACTLY ONCE under a ``threading.Lock``.  A single
``RoutingFileHandler`` reads a ``contextvars.ContextVar`` on every ``emit``
to decide which file to write to.  Because ContextVars are thread-local-
inherited, each worker thread sets its own copy at the start of a run without
affecting other threads.

When the ContextVar is ``None`` (main thread / orchestration code with no
active run), records are written to the global fallback file
(``logs/local_goedel.log`` by default) rather than being dropped, so
orchestration logs are still captured.  Per-problem records always have the
ContextVar set inside ``pipeline.run``, so they go ONLY to their ``run.log``;
the global file never receives interleaved per-problem agent logs.

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
import threading
from contextvars import ContextVar
from pathlib import Path


# ---------------------------------------------------------------------------
# Module-level ContextVar: set to the path of the current run's log file.
# None means "no active run" — records go to the global fallback file.
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

    It reads ``current_run_logfile`` on every ``emit()``.  If the var is
    ``None`` the record is written to the module-level ``_global_log_file``
    (orchestration / main-thread records).  Otherwise it opens (or reuses)
    the per-run FileHandler identified by the ContextVar path.

    FileHandler caching: we keep a dict of path → FileHandler so that we
    don't open/close the same file on every log call.  Handlers are closed
    when ``close()`` is called (at process exit or test teardown).  Cache
    insertion is guarded by its own lock to be safe under concurrency.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self._handlers: dict[str, logging.FileHandler] = {}
        self._cache_lock = threading.Lock()
        self._fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _get_handler(self, path: str) -> logging.FileHandler:
        with self._cache_lock:
            if path not in self._handlers:
                fh = logging.FileHandler(path, encoding="utf-8")
                fh.setLevel(logging.DEBUG)
                fh.setFormatter(self._fmt)
                self._handlers[path] = fh
            return self._handlers[path]

    def emit(self, record: logging.LogRecord) -> None:
        path = current_run_logfile.get()
        if path is None:
            # Write to global fallback instead of dropping.
            path = _global_log_file
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
        with self._cache_lock:
            for fh in self._handlers.values():
                fh.close()
            self._handlers.clear()
        super().close()


# ---------------------------------------------------------------------------
# Thread-safe one-time configuration of the ``local_goedel`` root logger.
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_configured: bool = False
_global_log_file: str = str(
    Path(os.environ.get("LOG_DIR", "logs")) / "local_goedel.log"
)


def _configure_root(log_dir: str | Path | None = None) -> None:
    """Configure the ``local_goedel`` logger ONCE, thread-safely.

    Installs exactly one console ``StreamHandler`` (level INFO,
    ``AsciiSafeFormatter``) and one ``RoutingFileHandler`` (level DEBUG) on
    the ``local_goedel`` root logger.  Subsequent calls from any thread are
    no-ops once configuration is complete (double-checked locking).

    *log_dir* is only honoured on the first call that actually configures the
    logger; it sets the global fallback log path used by
    ``RoutingFileHandler`` when ``current_run_logfile`` is ``None``.
    """
    global _configured, _global_log_file

    # Fast path — already configured.
    if _configured:
        return

    with _config_lock:
        # Double-checked locking: re-test after acquiring the lock.
        if _configured:
            return

        if log_dir is not None:
            _global_log_file = str(Path(log_dir) / "local_goedel.log")

        root = logging.getLogger("local_goedel")
        root.setLevel(logging.DEBUG)
        root.propagate = False  # don't double-emit via the real root logger

        # Check what is already present (e.g. after test-fixture reset).
        # Identify OUR console handler by its AsciiSafeFormatter so we don't
        # confuse it with pytest's LogCaptureHandler (also a StreamHandler
        # subclass) or any other third-party handler on this logger.
        has_console = any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, RoutingFileHandler)
            and isinstance(getattr(h, "formatter", None), AsciiSafeFormatter)
            for h in root.handlers
        )
        has_routing = any(isinstance(h, RoutingFileHandler) for h in root.handlers)

        if not has_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_fmt = AsciiSafeFormatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
            console_handler.setFormatter(console_fmt)
            root.addHandler(console_handler)

        if not has_routing:
            routing_handler = RoutingFileHandler()
            root.addHandler(routing_handler)

        _configured = True


def get_logger(name: str, log_dir: str | Path | None = None) -> logging.Logger:
    """Return a logger under the ``local_goedel`` hierarchy.

    All I/O is handled by the ``local_goedel`` root logger's handlers (one
    console ``StreamHandler`` + one ``RoutingFileHandler``).  Child loggers
    inherit the root's level and propagate records up — NO per-child handlers
    are added, so there is no risk of duplicated console lines or interleaved
    file writes under concurrent execution.

    *log_dir* sets the directory for the global fallback log file (only
    honoured on the first call that triggers configuration); defaults to the
    ``LOG_DIR`` env var or ``logs/``.
    """
    if log_dir is None:
        log_dir = Path(os.environ.get("LOG_DIR", "logs"))
    _configure_root(log_dir)
    return logging.getLogger(name)
