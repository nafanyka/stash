"""Effective MyMoover settings.

Stash offers no declarable default for plugin settings, so every default lives here,
the same approach FastDiscovery uses (see its settings.py) - the manifest's `settings:`
block exists only so the values are editable from Stash's own plugin panel.
"""

from __future__ import annotations

import re

PLUGIN_ID = "MyMoover"

DEFAULT_SIDECAR_PATTERNS = (
    ".funscript",
    ".*.funscript",
    ".srt",
    ".vtt",
    ".ass",
    ".ssa",
    ".nfo",
    ".json",
)

_SPLIT = re.compile(r"[,\n]+")


def parse_patterns(raw) -> tuple[str, ...]:
    """A comma/newline-separated setting string -> a deduplicated pattern tuple.

    Empty input means "use the defaults" (requirement 11: sidecars must have a
    sensible default list, not one the user is forced to type out to get anything).
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_SIDECAR_PATTERNS
    seen = []
    for part in _SPLIT.split(text):
        pattern = part.strip()
        if pattern and pattern not in seen:
            seen.append(pattern)
    return tuple(seen) or DEFAULT_SIDECAR_PATTERNS


class Settings:
    def __init__(self, move_sidecars, sidecar_patterns, debug_logging):
        self.move_sidecars = move_sidecars
        self.sidecar_patterns = sidecar_patterns
        self.debug_logging = debug_logging


def parse(raw: dict | None) -> Settings:
    raw = raw or {}
    move_sidecars = raw.get("moveSidecars")
    move_sidecars = True if move_sidecars is None else bool(move_sidecars)
    return Settings(
        move_sidecars=move_sidecars,
        sidecar_patterns=parse_patterns(raw.get("sidecarPatterns")),
        debug_logging=bool(raw.get("debugLogging")),
    )
