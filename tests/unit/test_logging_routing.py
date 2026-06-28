"""Unit tests for concurrent log routing via ContextVar.

These tests verify that:
1. Two "pipeline" contexts running concurrently write ONLY their own records
   to their respective run.log files (no cross-contamination).
2. A record logged from a thread with no active run context (ContextVar=None)
   goes to the GLOBAL FALLBACK file (not dropped).
3. The local_goedel root logger is configured EXACTLY ONCE even when
   get_logger() is called concurrently from many threads (no duplicate handlers).
4. A single emitted record produces exactly ONE line in the target file.
5. The RoutingFileHandler does not accumulate open handlers on any logger.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import local_goedel.logging_utils as _lu
from local_goedel.logging_utils import (
    AsciiSafeFormatter,
    RoutingFileHandler,
    _configure_root,
    current_run_logfile,
    get_logger,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_logging_state(tmp_path):
    """Reset global logging state before and after each test for isolation.

    Clears all handlers from the ``local_goedel`` root logger and resets the
    ``_configured`` flag so every test starts from a clean slate.
    """
    def _clear():
        root = logging.getLogger("local_goedel")
        for h in list(root.handlers):
            try:
                h.close()
            except Exception:
                pass
            root.removeHandler(h)
        _lu._configured = False
        # Point fallback to a per-test tmp dir so we don't accumulate files.
        _lu._global_log_file = str(tmp_path / "fallback" / "local_goedel.log")

    _clear()   # setup
    yield
    _clear()   # teardown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
# Core tests
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
        """Each thread's run.log must contain ONLY that thread's messages.

        Uses a standalone RoutingFileHandler on a dedicated logger (propagate
        disabled) so the test is fully self-contained and does not rely on
        the centralised root-logger setup.
        """
        log_a = tmp_path / "run_a" / "run.log"
        log_b = tmp_path / "run_b" / "run.log"
        log_a.parent.mkdir(parents=True)
        log_b.parent.mkdir(parents=True)

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

        # Cross-contamination check.
        for msg in msgs_b:
            assert msg not in content_a, f"Cross-contamination: '{msg}' found in log_a"
        for msg in msgs_a:
            assert msg not in content_b, f"Cross-contamination: '{msg}' found in log_b"

    def test_no_log_without_context_goes_to_fallback(self, tmp_path):
        """A record logged with ContextVar=None must land in the GLOBAL FALLBACK
        file, not be silently dropped and not appear in any run.log."""
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)

        # Configure the root logger so the fallback path is our tmp dir.
        _configure_root(fallback_dir)
        fallback_log = fallback_dir / "local_goedel.log"

        lg = get_logger("local_goedel.test_routing_none_fallback")
        token = current_run_logfile.set(None)
        try:
            lg.warning("THIS_SHOULD_GO_TO_FALLBACK")
        finally:
            current_run_logfile.reset(token)

        assert fallback_log.exists(), (
            "Global fallback log was not created when ContextVar was None"
        )
        content = fallback_log.read_text(encoding="utf-8")
        assert "THIS_SHOULD_GO_TO_FALLBACK" in content, (
            f"Expected message not found in global fallback log:\n{content}"
        )

    def test_handler_idempotent_configuration(self, tmp_path):
        """_configure_root() must not add duplicate handlers even when called
        many times — exactly ONE console StreamHandler and ONE RoutingFileHandler
        must be present on the local_goedel root logger."""
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)

        # Call many times from the same thread.
        for _ in range(10):
            _configure_root(fallback_dir)

        root = logging.getLogger("local_goedel")
        console_count = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RoutingFileHandler)
            and isinstance(getattr(h, "formatter", None), AsciiSafeFormatter)
        )
        routing_count = sum(
            1 for h in root.handlers if isinstance(h, RoutingFileHandler)
        )
        assert console_count == 1, (
            f"Expected exactly 1 console StreamHandler, found {console_count}"
        )
        assert routing_count == 1, (
            f"Expected exactly 1 RoutingFileHandler, found {routing_count}"
        )

    def test_get_logger_no_duplicate_handlers(self, tmp_path):
        """Calling get_logger() for the same name from many concurrent threads
        must NOT produce duplicate handlers on the local_goedel root logger,
        and a single emitted record must produce exactly ONE line in the target
        file (no doubling from extra handlers)."""
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        run_log = tmp_path / "run.log"

        barrier = threading.Barrier(8)

        def call_get_logger():
            barrier.wait(timeout=5)
            get_logger("local_goedel.concurrent_init_test", log_dir=fallback_dir)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(call_get_logger) for _ in range(8)]
            for f in futures:
                f.result()

        root = logging.getLogger("local_goedel")
        console_count = sum(
            1
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, RoutingFileHandler)
            and isinstance(getattr(h, "formatter", None), AsciiSafeFormatter)
        )
        routing_count = sum(
            1 for h in root.handlers if isinstance(h, RoutingFileHandler)
        )
        assert console_count == 1, (
            f"Expected exactly 1 console StreamHandler, found {console_count}"
        )
        assert routing_count == 1, (
            f"Expected exactly 1 RoutingFileHandler, found {routing_count}"
        )

        # A single emitted record must produce exactly ONE line in the file.
        lg = get_logger("local_goedel.concurrent_init_test")
        token = current_run_logfile.set(str(run_log))
        try:
            lg.warning("UNIQUE_SINGLE_LINE_CHECK")
        finally:
            current_run_logfile.reset(token)

        content = run_log.read_text(encoding="utf-8")
        count = content.count("UNIQUE_SINGLE_LINE_CHECK")
        assert count == 1, (
            f"Expected exactly 1 log line, found {count}:\n{content}"
        )

    def test_concurrent_routing_no_crosstalk(self, tmp_path):
        """Two threads sharing the same get_logger() logger must each write
        ONLY their own records — no cross-contamination via the centralised
        RoutingFileHandler."""
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        log_a = tmp_path / "run_a" / "run.log"
        log_b = tmp_path / "run_b" / "run.log"
        log_a.parent.mkdir(parents=True)
        log_b.parent.mkdir(parents=True)

        lg = get_logger("local_goedel.test_crosstalk", log_dir=fallback_dir)
        barrier = threading.Barrier(2)

        msgs_a = ["THREAD_A_MSG_1", "THREAD_A_MSG_2"]
        msgs_b = ["THREAD_B_MSG_1", "THREAD_B_MSG_2"]

        t_a = threading.Thread(
            target=self._write_logs_in_thread,
            args=(log_a, msgs_a, barrier, lg),
        )
        t_b = threading.Thread(
            target=self._write_logs_in_thread,
            args=(log_b, msgs_b, barrier, lg),
        )

        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        content_a = log_a.read_text(encoding="utf-8")
        content_b = log_b.read_text(encoding="utf-8")

        for msg in msgs_a:
            assert msg in content_a, f"Expected '{msg}' in log_a"
        for msg in msgs_b:
            assert msg in content_b, f"Expected '{msg}' in log_b"
        for msg in msgs_b:
            assert msg not in content_a, f"Cross-contamination: '{msg}' found in log_a"
        for msg in msgs_a:
            assert msg not in content_b, f"Cross-contamination: '{msg}' found in log_b"

    def test_many_runs_no_handler_accumulation(self, tmp_path):
        """Simulating many sequential runs must not accumulate open handlers
        on the logger itself (the RoutingFileHandler caches file handles
        internally, but there should be only one RoutingFileHandler on the
        logger at all times)."""
        test_logger = logging.getLogger("local_goedel.test_routing_accumulate")
        test_logger.setLevel(logging.DEBUG)
        test_logger.propagate = False
        test_logger.handlers = [
            h
            for h in test_logger.handlers
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
