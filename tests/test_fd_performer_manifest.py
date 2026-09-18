"""The `_MyFastPerformerScrapper` shim: what it must never do.

Mirrors `test_fd_manifest.py`'s `TestScraperShim`. This scraper is deliberately not a
real entry point (see its own .yml for why: Stash's `scrapeSinglePerformer` never hands
a scraper_id-based scrape a reliable performer id, and the Performer edit page's
"Scrape with..." menu always opens the search dialog first), so what matters here is
that it can never merge anything into the wrong performer.
"""

from __future__ import annotations

import os
import re

import yaml
from fd_common import FD_PLUGIN_DIR  # noqa: F401  (adds FastDiscovery to sys.path)

SCRAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(FD_PLUGIN_DIR)),
                           "scrapers", "_MyFastPerformerScrapper")
SCRAPER_MANIFEST = os.path.join(SCRAPER_DIR, "_MyFastPerformerScrapper.yml")
SCRAPER_SOURCE = os.path.join(SCRAPER_DIR, "_MyFastPerformerScrapper.py")


def manifest():
    with open(SCRAPER_MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class TestIdentity:
    def test_the_scraper_is_named_after_its_folder(self):
        assert manifest()["name"] == "_MyFastPerformerScrapper"

    def test_the_build_can_read_its_version_and_description(self):
        text = open(SCRAPER_MANIFEST, encoding="utf-8").read()
        assert re.search(r"^#\s*version:\s*\S+", text, re.MULTILINE)
        assert re.search(r"^#\s*description:\s*\S+", text, re.MULTILINE)


class TestNeverAScrapeAnyoneCanMerge:
    def test_it_registers_as_a_performer_name_scraper_only(self):
        # Not FRAGMENT: a FRAGMENT-only performer scraper does not even appear in the
        # Performer edit page's "Scrape with..." menu (checked against the v0.31.1
        # frontend), so NAME is the only way this scraper is reachable at all - and
        # every path there opens the search dialog regardless of what it declares.
        config = manifest()
        assert config["performerByName"]["action"] == "script"
        assert config["performerByName"]["script"][:2] == \
            ["python", "_MyFastPerformerScrapper.py"]
        assert "performerByFragment" not in config
        assert "performerByURL" not in config

    def test_the_shim_always_answers_with_no_results(self):
        # The one property that matters: whatever is typed into the search dialog,
        # nothing comes back that could be picked and merged into the wrong performer.
        source = open(SCRAPER_SOURCE, encoding="utf-8").read()
        printed = re.findall(r"^\s*print\((.+)\)\s*$", source, re.MULTILINE)
        assert printed
        assert all(call.strip() == "json.dumps([])" for call in printed), printed

    def test_the_shim_holds_no_discovery_logic(self):
        source = open(SCRAPER_SOURCE, encoding="utf-8").read()
        for forbidden in ("scrapeSinglePerformer", "scrapePerformerURL",
                          "import sqlite3", "fastdiscovery", "runPluginTask",
                          "runPluginOperation"):
            assert forbidden not in source, forbidden
