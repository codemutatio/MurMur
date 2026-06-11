"""Logging configuration for murmur."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


def configure_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Configure root logger for murmur.

    Writes to stderr (always) and optionally to *log_file*.
    """
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(handlers=handlers, format=fmt, datefmt=datefmt, level=level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
