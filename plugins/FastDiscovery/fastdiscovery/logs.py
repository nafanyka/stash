"""Logging, in the protocol Stash reads.

Stash strips a control prefix off every stderr line to pick the level shown in
Settings -> Logs; `p` is not a level but the job's progress fraction. Everything goes
to stderr because stdout is the plugin's return channel - a stray print there corrupts
the JSON Stash parses as the result.

Nothing here ever formats a credential: `sanitise` is applied to every message, so an
API key that turns up inside a scraper's error text does not reach the log or the
database (requirement 43).
"""

from __future__ import annotations

import re
import sys

_START = "\x01"
_END = "\x02"

PREFIX = "[FastDiscovery] "

# Anything that looks like a credential in text we did not write ourselves. Scraper and
# server errors quote the request they failed on, which is where these turn up.
_SECRETS = (
    re.compile(r"(?i)\b(api[-_ ]?key|apikey|authorization|bearer|session|token|cookie|"
               r"password|passwd|secret)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bApiKey\s+\S+"),
    re.compile(r"(?i)([?&](?:api[-_]?key|token|auth|key|password)=)[^&\s]+"),
)

_debug_enabled = False


def set_debug(enabled) -> None:
    global _debug_enabled
    _debug_enabled = bool(enabled)


def debug_enabled() -> bool:
    return _debug_enabled


def sanitise(message) -> str:
    text = str(message)
    for pattern in _SECRETS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "")
                           + "<redacted>", text)
    return text


def _write(level, message) -> None:
    for line in sanitise(message).split("\n"):
        sys.stderr.write("%s%s%s%s%s\n" % (_START, level, _END, PREFIX, line))
    sys.stderr.flush()


def debug(message) -> None:
    """Only emitted with debugLogging on - one run is dozens of source lines."""
    if _debug_enabled:
        _write("d", message)


def info(message) -> None:
    _write("i", message)


def warning(message) -> None:
    _write("w", message)


def error(message) -> None:
    _write("e", message)


def progress(fraction) -> None:
    try:
        value = float(fraction)
    except (TypeError, ValueError):
        return
    _write("p", "%.4f" % min(max(value, 0.0), 1.0))


def use_utf8() -> None:
    """Stop a legacy console codepage turning a scraped Japanese title into a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
