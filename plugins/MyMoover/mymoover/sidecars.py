"""Sidecar discovery: files that belong with a media file but aren't Stash File rows.

Matching is against a media file's own basename stem, in its own source directory
only - never recursive (requirement 30). A pattern is the suffix appended to the
stem: ".funscript" -> "<stem>.funscript"; a single "*" stands for exactly one
wildcard segment, so ".*.funscript" also matches "<stem>.L0.funscript",
"<stem>.R1.funscript", etc. (requirement 10's multi-axis funscripts).
"""

from __future__ import annotations

import os
import re

_WILDCARD_SEGMENT = "[^" + re.escape(os.sep) + "]*"


def _pattern_body(pattern: str) -> str:
    """A raw setting pattern like ".*.funscript" -> a regex fragment, "*" escaped
    literally and expanded to one wildcard segment everywhere else."""
    segments = str(pattern).split("*")
    return _WILDCARD_SEGMENT.join(re.escape(segment) for segment in segments)


def regexes_for_basename(media_basename: str, patterns) -> list:
    """One compiled, case-insensitive, anchored regex per pattern, for this exact
    media file's basename stem."""
    stem = os.path.splitext(media_basename)[0]
    return [
        re.compile("^" + re.escape(stem) + _pattern_body(pattern) + "$", re.IGNORECASE)
        for pattern in patterns
        if stem
    ]


class DirCache:
    """One `scandir` per source directory, no matter how many files share it."""

    def __init__(self):
        self._cache: dict[str, list[str]] = {}

    def names(self, directory: str) -> list[str]:
        if directory not in self._cache:
            try:
                with os.scandir(directory) as entries:
                    self._cache[directory] = [e.name for e in entries if e.is_file()]
            except OSError:
                self._cache[directory] = []
        return self._cache[directory]


def find_sidecars(directory: str, media_basename: str, patterns, dir_cache: DirCache) -> list[str]:
    """Filenames (not paths) in `directory` that match a sidecar pattern for this
    media file, excluding the media file itself."""
    regexes = regexes_for_basename(media_basename, patterns)
    if not regexes:
        return []
    matches = []
    for name in dir_cache.names(directory):
        if name == media_basename:
            continue
        if any(regex.match(name) for regex in regexes):
            matches.append(name)
    return matches
