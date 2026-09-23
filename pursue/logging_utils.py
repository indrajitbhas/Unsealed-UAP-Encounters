"""Shared logging setup.

A single console + file handler configuration used by every module. Call
:func:`setup_logging` once from the runner; modules just do
``log = logging.getLogger(__name__)``.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    """Minimal ANSI colouring for console readability (no dependencies)."""

    COLORS = {
        logging.DEBUG: "\033[37m",     # grey
        logging.INFO: "\033[36m",      # cyan
        logging.WARNING: "\033[33m",   # yellow
        logging.ERROR: "\033[31m",     # red
        logging.CRITICAL: "\033[41m",  # red bg
    }
    RESET = "\033[0m"

    def __init__(self, use_color: bool):
        super().__init__("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                         datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        msg = super().format(record)
        if self.use_color:
            color = self.COLORS.get(record.levelno, "")
            return f"{color}{msg}{self.RESET}"
        return msg


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> Path:
    """Configure root logging. Returns the path of the log file."""
    global _CONFIGURED
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "pursue_probe.log"

    if _CONFIGURED:
        return log_file

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler (colour if a TTY).
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))
    ch.setLevel(level)
    root.addHandler(ch)

    # File handler (plain, always).
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    # Quiet the very chatty Azure SDK HTTP logger.
    logging.getLogger("azure").setLevel(logging.WARNING)

    _CONFIGURED = True
    return log_file
