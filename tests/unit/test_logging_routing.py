"""Unit tests for concurrent log routing via ContextVar.

These tests verify that:
1. Two "pipeline" contexts running concurrently write ONLY their own records
   to their respective run.log files (no cross-contamination).
2. A record logged from a thread with no active run context (ContextVar=None)
   does NOT appear in any run.log (it goes only to the global log + console).
3. The RoutingFileHandler is NOT accumulated on the logger across multiple
   get_logger() calls (idempotent install).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest

from local_goedel.logging_utils import (
    RoutingFileHandler,
    _ensure_routing_handler,
    current_run_logfile,
    get_logger,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_routing_handler() -> RoutingFileHandler:
    """Return a standalone RoutingFileHandler (not attached to any logger)."""
    return RoutingFileHandler()


def _run_in_thread(func, *args):
    """Run func(*args) in a new thread; re-raise exceptions in the caller."""
    exc_box: list[BaseException] = []

    def wrapper():
        try:
            func(*args)
        except BaseException as e:
            exc_box.append(e)

    t = threading.Thread(target=wrapper)
    t.start()
    t.join()
    if exc_box:
        raise exc_box[0]


# ---------------------------------------------------------------------------
# Core concurrency test
# ---------------------------------------------------------------------------

class TestConcurrentLogRouting:
    """Drive the logger + ContextVar from two threads simultaneously."""

    def _write_logs_in_thread(
        self,
        log_path: Path,
        messages: list[str],
        barrier: threading.Barrier,
        logger: logging.Logger,
    ) -> None:
        """Set the ContextVar, wait at the barrier, then log the messages."""
        token = current_run_logfile.set(str(log_path))
        try:
            # Synchronise so both threads are active at the same time.
            barrier.wait(timeout=5)
            for msg in messages:
                logger.info(msg)
            # Wait again so both threads are still active while the other logs.
            barrier.wait(timeout=5)
        finally:
            current_run_logfile.reset(token)

    def test_no_cross_contamination(self, tmp_path):
        """Each thread's run.log must contain ONLY that thread's messages."""
        log_a = tmp_path / "run_a" / "run.log"
        log_b = tmp_path / "run_b" / "run.log"
        log_a.parent.mkdir(parents=True)
        log_b.parent.mkdir(parents=True)

        # Use a dedicated logger so the test is self-contained.
        test_logger = logging.getLogger("local_goedel.test_routing_cc")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False

        handler = RoutingFileHandler()
        test_logger.addHandler(handler)

        barrier = threading.Barrier(2)

        msgs_a = ["ALPHA_only_message_1", "ALPHA_only_message_2"]
        msgs_b = ["BETA_only_message_1", "BETA_only_message_2"]

        t_a = threading.Thread(
            target=self._write_logs_in_thread,
            args=(log_a, msgs_a, barrier, test_logger),
        )
        t_b = threading.Thread(
            target=self._write_logs_in_thread,
            args=(log_b, msgs_b, barrier, test_logger),
        )

        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        handler.close()
        test_logger.removeHandler(handler)

        content_a = log_a.read_text(encoding="utf-8")
        content_b = log_b.read_text(encoding="utf-8")

        # Each file must contain its own messages.
        for msg in msgs_a:
            assert msg in content_a, f"Expected '{msg}' in log_a"
        for msg in msgs_b:
            assert msg in content_b, f"Expected '{msg}' in log_b"

        # Cross-contamination check: no BETA message in A and vice-versa.
        for msg in msgs_b:
            assert msg not in content_a, f"Cross-contamination: '{msg}' found in log_a"
        for msg in msgs_a:
            assert msg not in content_b, f"Cross-contamination: '{msg}' found in log_b"

    def test_no_log_without_context(self, tmp_path):
        """A record logged from a thread with ContextVar=None must NOT appear
        in any run.log (the RoutingFileHandler silently drops it)."""
        log_c = tmp_path / "run_c" / "run.log"
        log_c.parent.mkdir(parents=True)

        test_logger = logging.getLogger("local_goedel.test_routing_none")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False

        handler = RoutingFileHandler()
        test_logger.addHandler(handler)

        # Log WITHOUT setting the ContextVar (it defaults to None).
        token = current_run_logfile.set(None)
        try:
            test_logger.info("THIS_SHOULD_NOT_APPEAR_ANYWHERE")
        finally:
            current_run_logfile.reset(token)

        handler.close()
        test_logger.removeHandler(handler)

        # The log file should not exist at all (nothing was written to it).
        assert not log_c.exists(), "run.log was created even though ContextVar was None"

    def test_handler_idempotent_installation(self, tmp_path):
        """_ensure_routing_handler must not add duplicate handlers."""
        test_logger = logging.getLogger("local_goedel.test_routing_idem")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False

        # Remove any pre-existing RoutingFileHandlers.
        test_logger.handlers = [
            h for h in test_logger.handlers
            if not isinstance(h, RoutingFileHandler)
        ]

        # Call twice — should result in exactly one RoutingFileHandler.
        _ensure_routing_handler(test_logger)
        _ensure_routing_handler(test_logger)

        routing_count = sum(
            1 for h in test_logger.handlers if isinstance(h, RoutingFileHandler)
        )
        assert routing_count == 1, (
            f"Expected exactly 1 RoutingFileHandler, found {routing_count}"
        )

        # Cleanup
        for h in list(test_logger.handlers):
            if isinstance(h, RoutingFileHandler):
                h.close()
                test_logger.removeHandler(h)

    def test_many_runs_no_handler_accumulation(self, tmp_path):
        """Simulating many sequential runs must not accumulate open handlers
        on the logger itself (the RoutingFileHandler caches file handles
        internally, but there should be only one RoutingFileHandler on the
        logger at all times)."""
        test_logger = logging.getLogger("local_goedel.test_routing_accumulate")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False
        test_logger.handlers = [
            h for h in test_logger.handlers
            if not isinstance(h, RoutingFileHandler)
        ]

        handler = RoutingFileHandler()
        test_logger.addHandler(handler)

        # Simulate 20 sequential "runs" (set, log, reset).
        for i in range(20):
            log_p = tmp_path / f"run_{i:03d}" / "run.log"
            log_p.parent.mkdir(parents=True)
            token = current_run_logfile.set(str(log_p))
            try:
                test_logger.info("run_%03d message", i)
            finally:
                current_run_logfile.reset(token)

        # The logger must still have exactly ONE RoutingFileHandler.
        routing_count = sum(
            1 for h in test_logger.handlers if isinstance(h, RoutingFileHandler)
        )
        assert routing_count == 1

        # All 20 run.log files must exist and contain the right message.
        for i in range(20):
            log_p = tmp_path / f"run_{i:03d}" / "run.log"
            assert log_p.exists(), f"run_{i:03d}/run.log missing"
            assert f"run_{i:03d} message" in log_p.read_text(encoding="utf-8")

        handler.close()
        test_logger.removeHandler(handler)
