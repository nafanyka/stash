"""Logging, in the protocol Stash reads.

Stash strips a control prefix off every line a plugin writes to stderr and uses it to
pick the level shown in Settings -> Logs; a line without the prefix arrives unlabelled.
`p` is not a level but a progress fraction, which is how a plugin task drives the
progress bar on its job.

Everything here goes to stderr, because stdout is the plugin's return channel: Stash
parses it as the operation's JSON result, so a stray print there corrupts the response.
"""

from __future__ import annotations

import sys

_START = "\x01"
_END = "\x02"

PREFIX = "[ScrapeDiscovery] "

_debug_enabled = False


def set_debug(enabled) -> None:
    global _debug_enabled
    _debug_enabled = bool(enabled)


def debug_enabled() -> bool:
    return _debug_enabled


def _write(level, message) -> None:
    for line in str(message).split("\n"):
        sys.stderr.write("%s%s%s%s%s\n" % (_START, level, _END, PREFIX, line))
    sys.stderr.flush()


def trace(message) -> None:
    if _debug_enabled:
        _write("t", message)


def debug(message) -> None:
    """Only emitted when the debugLogging setting is on.

    Attempt-level detail is genuinely useful when a scraper misbehaves and pure noise
    the rest of the time - a deep scan is several hundred attempts per scene.
    """
    if _debug_enabled:
        _write("d", message)


def info(message) -> None:
    _write("i", message)


def warning(message) -> None:
    _write("w", message)


def error(message) -> None:
    _write("e", message)


def progress(fraction) -> None:
    """Report 0.0-1.0 to the job's progress bar (clamped)."""
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return
    _write("p", "%.4f" % min(max(value, 0.0), 1.0))


def use_utf8() -> None:
    """Stop a legacy console codepage from turning a scraped title into a crash.

    Windows defaults stdio to cp1252, and a scraper returning a Japanese title would
    otherwise raise UnicodeEncodeError inside the log call itself.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
