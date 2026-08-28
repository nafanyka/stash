"""Make the plugins importable, and provide the fakes the tests share.

Neither plugin is an installed package - Stash runs each from its own directory - so
the tests put those directories on the path the same way the entry points do. The two
plugins are independent packages (`scrapediscovery`, `fastdiscovery`) and are tested
independently; the fixtures below are prefixed accordingly.

FastDiscovery's fake server lives in fd_common.py rather than here, because it is
substantial enough to be worth reading on its own.
"""

from __future__ import annotations

import os
import sys

import pytest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "plugins", "ScrapeDiscovery")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from fd_common import FD_PLUGIN_DIR  # noqa: E402,F401  (adds it to sys.path)
from fastdiscovery import settings as fd_settings_module  # noqa: E402
from fastdiscovery.db import repo as fd_repo_module  # noqa: E402
from scrapediscovery import settings as settings_module  # noqa: E402
from scrapediscovery.db import repo as repo_module  # noqa: E402


@pytest.fixture
def config():
    """Default configuration, as an untouched Stash install would produce."""
    return settings_module.parse({})


@pytest.fixture
def repo(tmp_path):
    """A fresh migrated database per test."""
    store = repo_module.Repo.open(str(tmp_path / "scrape-discovery.sqlite"))
    yield store
    store.close()


class FakeClient:
    """A Stash server that answers from canned data.

    Only the methods the code under test calls are implemented; anything else raising
    AttributeError is the point - a test should not silently exercise a call this fake
    has quietly stubbed out.
    """

    def __init__(self, scene=None, scrapers=None, responses=None, boxes=None):
        self.scene = scene or {}
        self.scrapers = scrapers or []
        self.responses = responses or {}
        self.boxes = boxes or []
        self.calls = []
        self.updates = []

    # -- reads

    def version(self):
        return {"version": "v0.31.1", "hash": "test"}

    def plugin_settings(self, plugin_id):
        return {}

    def list_scene_scrapers(self):
        return self.scrapers

    def stash_boxes(self):
        return self.boxes

    def find_scene(self, scene_id):
        if str(self.scene.get("id")) != str(scene_id):
            return None
        return self.scene

    def config_dir(self):
        return ""

    # -- scraping. `responses` is keyed by the scraper id or the URL, and a value that
    # is an Exception is raised, which is how error and timeout paths are exercised.

    def scrape_scene(self, source, scrape_input, selection, timeout=None):
        key = source.get("scraper_id") or source.get("stash_box_endpoint")
        self.calls.append(("scrape_scene", key, scrape_input, timeout))
        return self._answer(key)

    def scrape_scene_url(self, url, selection, timeout=None):
        self.calls.append(("scrape_scene_url", url, None, timeout))
        return self._answer(url)

    def _answer(self, key):
        value = self.responses.get(key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    # -- writes

    def scene_update(self, values):
        self.updates.append(("scene_update", values))
        return {"id": values.get("id")}

    def bulk_scene_add(self, scene_id, tag_ids=None, performer_ids=None, urls=None):
        self.updates.append(("bulk_add", scene_id, tag_ids, performer_ids, urls))
        return [{"id": str(scene_id)}]

    def run_plugin_task(self, plugin_id, task_name, args=None):
        self.calls.append(("run_plugin_task", task_name, args, None))
        return "job-1"


@pytest.fixture
def scene():
    """A scene shaped as `findScene` returns it."""
    return {
        "id": "42",
        "title": "Example Scene",
        "code": None,
        "details": None,
        "director": None,
        "date": "2024-01-20",
        "urls": ["https://example.com/scene/1"],
        "organized": False,
        "studio": {"id": "5", "name": "Example Studio"},
        "performers": [{"id": "9", "name": "Alice"}],
        "tags": [{"id": "3", "name": "Existing Tag"}],
        "groups": [],
        "stash_ids": [],
        "files": [{
            "id": "100",
            "path": "/media/example_scene_1080p.mp4",
            "basename": "example_scene_1080p.mp4",
            "size": 1234567,
            "duration": 1800.0,
            "width": 1920,
            "height": 1080,
            "fingerprints": [{"type": "oshash", "value": "abc123"},
                             {"type": "phash", "value": "def456"}],
        }],
    }


@pytest.fixture
def scrapers():
    """listScrapers rows covering each capability combination."""
    return [
        {"id": "SiteA", "name": "Site A",
         "scene": {"urls": ["sitea.com/scene/"], "supported_scrapes": ["URL", "FRAGMENT"]}},
        {"id": "SiteB", "name": "Site B",
         "scene": {"urls": ["siteb.com/v/"], "supported_scrapes": ["URL"]}},
        {"id": "Searcher", "name": "Searcher",
         "scene": {"urls": [], "supported_scrapes": ["NAME", "FRAGMENT"]}},
        {"id": "Example", "name": "Example Site",
         "scene": {"urls": ["example.com/scene/"],
                   "supported_scrapes": ["URL", "FRAGMENT", "NAME"]}},
        {"id": "Filename", "name": "Filename",
         "scene": {"urls": [], "supported_scrapes": ["FRAGMENT"]}},
    ]


def scraped(title=None, date=None, urls=None, studio=None, performers=None,
            tags=None, duration=None, image=None, code=None, details=None):
    """A `ScrapedScene` payload, with only the fields a test cares about."""
    payload = {}
    if title is not None:
        payload["title"] = title
    if date is not None:
        payload["date"] = date
    if urls is not None:
        payload["urls"] = list(urls)
    if studio is not None:
        payload["studio"] = {"name": studio}
    if performers is not None:
        payload["performers"] = [{"name": name} for name in performers]
    if tags is not None:
        payload["tags"] = [{"name": name} for name in tags]
    if duration is not None:
        payload["duration"] = duration
    if image is not None:
        payload["image"] = image
    if code is not None:
        payload["code"] = code
    if details is not None:
        payload["details"] = details
    return payload


# ------------------------------------------------- FastDiscovery

@pytest.fixture
def fd_config():
    """Default configuration, as an untouched Stash install would produce."""
    return fd_settings_module.parse({})


@pytest.fixture
def fd_repo(tmp_path):
    """A fresh migrated database per test."""
    store = fd_repo_module.Repo.open(str(tmp_path / "fastdiscovery.sqlite"))
    yield store
    store.close()


@pytest.fixture
def fd_scene():
    """A scene shaped as `findScene` returns it."""
    return {
        "id": "295",
        "title": "Old title",
        "code": None,
        "details": None,
        "director": None,
        "date": None,
        "rating100": None,
        "urls": ["https://sitea.com/scene/1"],
        "organized": False,
        "updated_at": "2026-01-01T00:00:00Z",
        "paths": {"screenshot": "http://localhost:9999/scene/295/screenshot"},
        "studio": None,
        "performers": [{"id": "9", "name": "Angela White"}],
        "tags": [{"id": "3", "name": "Existing Tag"}],
        "groups": [],
        "stash_ids": [],
        "files": [{
            "id": "100", "path": "/media/example_scene_1080p.mp4",
            "basename": "example_scene_1080p.mp4", "size": 1234567,
            "duration": 1800.0, "width": 1920, "height": 1080,
            "fingerprints": [{"type": "oshash", "value": "abc123"},
                             {"type": "phash", "value": "def456"}],
        }],
    }
