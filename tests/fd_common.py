"""The fake Stash the FastDiscovery tests run against, and the sample data.

The plugin is not an installed package - Stash runs it from its own directory - so this
puts that directory on the path the same way the entry point does. No Stash server is
needed anywhere: `FakeStash` answers the handful of calls the code under test makes, and
raises AttributeError for anything else, which is the point - a test should not silently
exercise a call this fake has quietly stubbed out.

The pytest fixtures built on top of this live in conftest.py, which is shared with the
ScrapeDiscovery tests.
"""

from __future__ import annotations

import os
import sys

FD_PLUGIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins", "FastDiscovery")
if FD_PLUGIN_DIR not in sys.path:
    sys.path.insert(0, FD_PLUGIN_DIR)


BOXES = [
    {"name": "StashDB", "endpoint": "https://stashdb.org/graphql"},
    {"name": "ThePornDB", "endpoint": "https://theporndb.net/graphql"},
]

SCRAPERS = [
    {"id": "Pornhub", "name": "Pornhub",
     "scene": {"urls": ["pornhub.com"], "supported_scrapes": ["URL", "FRAGMENT"]}},
    {"id": "SiteA", "name": "Site A",
     "scene": {"urls": ["sitea.com"], "supported_scrapes": ["URL"]}},
    {"id": "SiteB", "name": "Site B",
     "scene": {"urls": ["siteb.com"], "supported_scrapes": ["URL", "FRAGMENT"]}},
    {"id": "SiteC", "name": "Site C",
     "scene": {"urls": ["sitec.com"], "supported_scrapes": ["URL"]}},
    # Two scrapers claiming the same host, which is the ambiguous-URL case.
    {"id": "SharedOne", "name": "Shared One",
     "scene": {"urls": ["shared.com"], "supported_scrapes": ["URL", "FRAGMENT"]}},
    {"id": "SharedTwo", "name": "Shared Two",
     "scene": {"urls": ["shared.com"], "supported_scrapes": ["URL"]}},
]


class FakeStash:
    """A Stash server that answers from canned data.

    `responses` is keyed by the stash-box endpoint, by the URL for a plain URL scrape,
    or by "<scraper_id>@<url>" for an aimed fragment scrape. A value that is an
    Exception is raised, which is how the error and timeout paths are exercised.
    """

    def __init__(self, scene=None, scrapers=None, boxes=None, responses=None,
                 entities=None):
        self.scene = scene or {}
        self.scrapers = scrapers if scrapers is not None else SCRAPERS
        self.boxes = boxes if boxes is not None else BOXES
        self.responses = responses or {}
        self.entities = entities or {}
        self.calls = []
        self.updates = []
        self.created = []
        self.settings = {}

    # -- reads

    def version(self):
        return {"version": "v0.31.1", "hash": "test"}

    def plugin_settings(self, plugin_id):
        return dict(self.settings)

    def save_plugin_settings(self, plugin_id, values):
        self.settings.update(values)
        return True

    def config_dir(self):
        return ""

    def stash_boxes(self):
        return list(self.boxes)

    def list_scene_scrapers(self):
        return list(self.scrapers)

    def find_scene(self, scene_id):
        if str(self.scene.get("id")) != str(scene_id):
            return None
        return self.scene

    def find_scenes_brief(self, scene_ids):
        wanted = {str(one) for one in (scene_ids or [])}
        return [self.scene] if str(self.scene.get("id")) in wanted else []

    def try_call(self, *args, **kwargs):
        return None

    # -- scraping

    def scrape_scene(self, source, scrape_input, selection, timeout=None):
        if source.get("stash_box_endpoint"):
            key = source["stash_box_endpoint"]
            if "query" in scrape_input:
                key += "?query"
        else:
            key = "%s@%s" % (source.get("scraper_id"),
                             (scrape_input.get("scene_input") or {}).get("url"))
        self.calls.append(("scrape_scene", key, timeout))
        return self._answer(key)

    def scrape_scene_url(self, url, selection, timeout=None):
        self.calls.append(("scrape_scene_url", url, timeout))
        return self._answer(url)

    def _answer(self, key):
        value = self.responses.get(key)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    # -- entity lookup and creation

    def find_performers_by_names(self, names):
        return self._lookup("performer", names)

    def find_tags_by_names(self, names):
        return self._lookup("tag", names)

    def find_studios_by_names(self, names):
        return self._lookup("studio", names)

    def find_groups_by_names(self, names):
        return self._lookup("group", names)

    def _lookup(self, kind, names):
        """What Stash answers: by name first, and only then by alias.

        The real client asks two questions and this stands in for both, because the
        difference between them is exactly what the alias handling is about - a name
        nothing is called can still be spoken for by something's alias.
        """
        known = self.entities.get(kind) or {}
        found = {}
        for name in (names or []):
            rows = list(known.get(name) or [])
            if not rows:
                rows = [row for value in known.values() for row in value
                        if name in _aliases(row)]
            found[name] = rows
        return found

    def create_performer(self, values):
        return self._create("performer", values)

    def create_tag(self, values):
        return self._create("tag", values)

    def create_studio(self, values):
        return self._create("studio", values)

    def create_group(self, values):
        return self._create("group", values)

    def _create(self, kind, values):
        self.created.append((kind, values))
        return {"id": "new-%s-%d" % (kind, len(self.created)), "name": values["name"]}

    # -- writes

    def scene_update(self, values):
        self.updates.append(values)
        return {"id": values.get("id"), "updated_at": "2026-01-01T00:00:00Z"}

    # -- jobs

    def run_plugin_task(self, plugin_id, task_name, args=None):
        self.calls.append(("run_plugin_task", task_name, args))
        return "job-1"

    def find_job(self, job_id):
        return {"id": job_id, "status": "RUNNING", "progress": 0.5}

    def stop_job(self, job_id):
        self.calls.append(("stop_job", job_id, None))
        return True


def scraped(**values):
    """A ScrapedScene payload with only the fields a test cares about."""
    payload = {}
    for key, value in values.items():
        if key == "performers":
            payload["performers"] = [one if isinstance(one, dict) else {"name": one}
                                     for one in value]
        elif key == "tags":
            payload["tags"] = [one if isinstance(one, dict) else {"name": one}
                               for one in value]
        elif key == "studio":
            payload["studio"] = value if isinstance(value, dict) else {"name": value}
        else:
            payload[key] = value
    return payload


def _aliases(row):
    """A record's aliases, in whichever of Stash's three shapes the row uses."""
    raw = row.get("aliases")
    if raw is None:
        raw = row.get("alias_list")
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(one).strip() for one in (raw or []) if str(one).strip()]
