"""Stash log protocol.

Stash reads a script's stderr and strips a control prefix off every line to pick
the log level shown in Settings -> Logs. Anything written without the prefix ends
up as an unlabelled line, so always go through these helpers.
"""

from __future__ import annotations

import sys

_START = "\x01"
_END = "\x02"


def _write(level: str, message: object) -> None:
    for line in str(message).split("\n"):
        print(f"{_START}{level}{_END}{line}", file=sys.stderr, flush=True)


def trace(message: object) -> None:
    _write("t", message)


def debug(message: object) -> None:
    _write("d", message)


def info(message: object) -> None:
    _write("i", message)


def warning(message: object) -> None:
    _write("w", message)


def error(message: object) -> None:
    _write("e", message)


def progress(fraction: float) -> None:
    """Report task progress as a 0.0-1.0 fraction (clamped)."""
    _write("p", str(min(max(fraction, 0.0), 1.0)))
