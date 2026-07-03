"""
Centralized logging for CEC4HTPC.

Everything funnels into cec4htpc.log (rotated at 2 MB, 3 backups) next to the
script, so sleep/resume/adapter issues that only show up hours later can be
diagnosed after the fact instead of needing a console attached at the time.
"""

import logging
import logging.handlers
import sys
from pathlib import Path

_LOG_PATH = Path(__file__).parent / "cec4htpc.log"

_FMT = logging.Formatter(
    "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def setup_logging(level=logging.DEBUG) -> logging.Logger:
    logger = logging.getLogger("cec4htpc")
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(_FMT)
    logger.addHandler(file_handler)

    # pythonw.exe has no console (sys.stdout is None) — only attach this
    # when one actually exists (e.g. running via `python cec4htpc.py`).
    if sys.stdout is not None:
        try:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(_FMT)
            logger.addHandler(console)
        except Exception:
            pass

    return logger
