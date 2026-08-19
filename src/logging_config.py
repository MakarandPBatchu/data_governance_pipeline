"""Logging configuration for the data governance pipeline."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOGS_DIR = Path("logs")


def configure_logging(level: str = "INFO", log_format: str = DEFAULT_LOG_FORMAT) -> Path:
    """Configure root logging to console and a timestamped file under ``logs/``.

    Creates ``logs/`` if it does not exist. Each pipeline run writes to a new
    file named ``log_YYYYMMDD_HHMMSS.txt``.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, or ERROR).
        log_format: Format string for log records.

    Returns:
        Path to the log file created for this run.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper()))

    formatter = logging.Formatter(log_format)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger(__name__).debug("Logging to %s", log_file)
    return log_file
