"""Logging utilities - UTF-8 file + ASCII-safe console."""
from __future__ import annotations

import logging
import os
from pathlib import Path


def _safe_str(s: str) -> str:
    """Convert string to ASCII-safe representation."""
    return s.encode("ascii", errors="backslashreplace").decode("ascii")


class AsciiSafeFormatter(logging.Formatter):
    """Formatter that strips/escapes non-ASCII to avoid GBK errors on Windows."""

    def format(self, record: logging.LogRecord) -> str:
        original = super().format(record)
        return _safe_str(original)


def get_logger(name: str, log_dir: str | Path | None = None) -> logging.Logger:
    """Get a logger writing UTF-8 to file + ASCII-safe to console."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # File handler - full UTF-8
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

    # Console handler - ASCII-safe
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = AsciiSafeFormatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    return logger
