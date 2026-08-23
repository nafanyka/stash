#!/usr/bin/env python3
"""ScrapeAllSettings - carries ScrapeAll's configuration, and reports it back.

The plugin exists for its `settings` block: Stash stores plugin settings in its
own config, where a scraper can read them, and gives them a UI. The one task here
parses those settings exactly as the scraper will and logs the result, so a
whitelist typo shows up on demand instead of silently dropping a field.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config, log, settings  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage
    except (AttributeError, OSError):
        pass


def show(url: str, api_key: str | None, cookie: str | None) -> str:
    raw = settings.fetch(url, api_key, cookie)
    parsed = settings.parse(raw)

    log.info("ScrapeAllSettings - effective configuration for the ScrapeAll scraper")
    log.info("  raw values     : " + (json.dumps(raw, ensure_ascii=False) if raw else "(none set)"))
    for line in parsed.describe():
        log.info("  " + line)

    for note in parsed.notes:
        log.warning("  " + note)
    for problem in parsed.errors:
        log.error("  " + problem)

    log.info("")
    log.info("How each allowed field is combined across sources:")
    for field, rule in settings.MERGE_RULES.items():
        mark = "  " if parsed.permits(field) else "x " if parsed.allowed is not None else "  "
        log.info("  " + mark + field.ljust(11) + rule)
    if parsed.allowed is not None:
        log.info('  ("x" marks a field the whitelist excludes)')

    if parsed.errors:
        return "settings parsed with " + str(len(parsed.errors)) + " problem(s) - see the log"
    return "; ".join(parsed.describe())


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": "could not read the plugin input: " + str(exc)}))
        return 0

    url, api_key, cookie = config.from_plugin_input(payload)
    if not (payload.get("server_connection") or {}):
        # Run outside Stash: fall back to the scraper's own discovery order.
        url, api_key, origin = config.load(__file__)
        log.debug("no server_connection on stdin - using " + url + " (from " + origin + ")")

    mode = str(((payload.get("args") or {}).get("mode")) or "show")
    try:
        if mode != "show":
            raise ValueError("unknown mode " + repr(mode))
        print(json.dumps({"output": show(url, api_key, cookie)}))
    except Exception as exc:
        log.error("ScrapeAllSettings failed: " + repr(exc))
        print(json.dumps({"error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
