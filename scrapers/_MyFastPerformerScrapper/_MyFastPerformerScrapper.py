#!/usr/bin/env python3
"""_MyFastPerformerScrapper - a deliberately empty performer-name scraper.

Read the module docstring in `_MyFastPerformerScrapper.yml` first; the short version:
Stash gives a `scraper_id`-based performer scrape only a name query or a client-built
fragment, never a reliable performer id, and its own frontend always opens the
search/pick dialog for anything in the Performer edit page's "Scrape with..." menu.
Neither lets this scraper do what Fast Performer Discovery needs - queue a background
job for *this* performer, no dialog, a toast on success - so it does not try. It
answers every search with nothing and says why, in the log, so a user who did click it
here sees an explanation instead of a name search silently going nowhere.

No GraphQL, no plugin database, no discovery logic: everything Fast/Full Performer
Discovery does lives in the FastDiscovery plugin, reached from the performer page/card/
list, never from here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import log  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def read_fragment() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main() -> int:
    fragment = read_fragment()
    query = str(fragment.get("name") or "").strip()

    log.warning("_MyFastPerformerScrapper does not search - it exists only to sit at "
               "the top of the scraper list. Fast Performer Discovery starts from the "
               "⚡ Fast Discovery button on a performer's page, card or list, "
               "which the FastDiscovery plugin adds; it never runs from a name "
               "search, because Stash does not hand a scraper a reliable performer id "
               "this way (see this scraper's README/yml for the full explanation).")
    if query:
        log.info("searched for %r - answering with no results, on purpose" % query)

    # Always empty, always deliberately: an empty result list is the one answer that
    # cannot be picked in the search dialog and merged into the wrong performer.
    print(json.dumps([]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
