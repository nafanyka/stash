"""Where to reach the local Stash server.

Scrapers - unlike plugins - are not handed the server connection details, so it
has to be discovered. Resolution order, first hit wins:

1. environment: STASH_URL / STASH_API_KEY
2. config.ini next to the script, then one directory up
3. the default http://localhost:9999/graphql with no API key

config.ini format:

    [stash]
    url = http://localhost:9999/graphql
    api_key = eyJhbGciOi...
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

DEFAULT_URL = "http://localhost:9999/graphql"
FILENAME = "config.ini"
SECTION = "stash"


def _candidates(start: Path) -> list[Path]:
    return [start / FILENAME, start.parent / FILENAME]


def load(script_file: str | None = None) -> tuple[str, str | None, str]:
    """Return (graphql_url, api_key, source_description)."""
    env_url, env_key = os.environ.get("STASH_URL"), os.environ.get("STASH_API_KEY")
    if env_url or env_key:
        return _normalise(env_url or DEFAULT_URL), env_key or None, "environment"

    start = Path(script_file).resolve().parent if script_file else Path.cwd()
    for path in _candidates(start):
        if not path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        if not parser.has_section(SECTION):
            continue
        url = parser.get(SECTION, "url", fallback=DEFAULT_URL)
        key = parser.get(SECTION, "api_key", fallback="").strip() or None
        return _normalise(url), key, str(path)

    return DEFAULT_URL, None, "default"


def _normalise(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url:
        return DEFAULT_URL
    return url if url.endswith("/graphql") else url + "/graphql"
