"""The scraper shim's entry point, and the recursion guard it makes necessary.

The shim is registered as a scene fragment scraper, which means the discovery engine
would otherwise find it in `listScrapers` and invoke it - and its answer to being
invoked is to start a discovery scan. That has to be impossible, not merely
discouraged.
"""

from __future__ import annotations

from conftest import FakeClient, scraped
from scrapediscovery import engine as E, ops, registry as G, settings as S
from scrapediscovery.db import repo as R
from test_engine import StubSchema


def context(repo, client=None, raw_config=None):
    return ops.Context(client or FakeClient(), repo, S.parse(raw_config or {}))


class TestRecursionGuard:
    def test_the_shim_can_never_be_invoked_by_a_scan(self):
        # Not a default: the shim starts scans, so a scan that invoked it would start a
        # scan. Stash gives a scraper no way to know it is running inside one - the
        # nested run is a fresh process - so this is the only reliable guard.
        for raw in ({}, {"excludedScrapers": ""},
                    {"scraperOverrides": {"ScrapeDiscovery": {"priority": "high"}}}):
            config = S.parse(raw)
            assert config.is_enabled("ScrapeDiscovery", "ScrapeDiscovery") is False

    def test_the_guard_is_case_insensitive(self):
        assert S.parse({}).is_enabled("scrapediscovery") is False
        assert S.parse({}).is_enabled("SCRAPEDISCOVERY") is False

    def test_it_never_appears_in_a_plan(self, scene):
        from scrapediscovery import normalize as N

        rows = [
            {"id": "ScrapeDiscovery", "name": "ScrapeDiscovery",
             "scene": {"urls": [], "supported_scrapes": ["FRAGMENT"]}},
            {"id": "Real", "name": "Real",
             "scene": {"urls": [], "supported_scrapes": ["FRAGMENT"]}},
        ]
        registry = G.Registry(G.from_list_scrapers(rows), S.parse({"excludedScrapers": ""}))
        planned = {(one.get("scraper") or {}).get("id")
                   for one in registry.plan(N.scene_snapshot(scene), S.DEEP)}
        assert "ScrapeDiscovery" not in planned
        assert "Real" in planned

    def test_a_scan_does_not_call_it(self, repo, scene):
        rows = [{"id": "ScrapeDiscovery", "name": "ScrapeDiscovery",
                 "scene": {"urls": [], "supported_scrapes": ["FRAGMENT"]}}]
        client = FakeClient(scene=scene, scrapers=rows, responses={
            "ScrapeDiscovery": scraped(title="should never be asked")})
        engine = E.Engine(client, repo, S.parse({"excludedScrapers": ""}),
                          schema=StubSchema())
        engine.scan(42)
        assert [one for one in client.calls if one[1] == "ScrapeDiscovery"] == []


def _scan(repo, scene, scrapers, responses):
    client = FakeClient(scene=scene, scrapers=scrapers, responses=responses)
    engine = E.Engine(client, repo, S.parse({"excludedScrapers": ""}),
                      schema=StubSchema())
    engine.scan(42)
    return client


class TestScraperEntry:
    def test_nothing_discovered_yet_queues_a_scan(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})), "scraper.entry",
                              {"scene_id": 42})
        assert answer["action"] == ops.QUEUED
        assert answer["scene"] is None
        queued = [one for one in client.calls if one[0] == "run_plugin_task"][0]
        assert queued[2]["scene_ids"] == "42"
        assert queued[2]["trigger"] == "scraper"

    def test_queuing_can_be_declined(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})), "scraper.entry",
                              {"scene_id": 42, "queue": False})
        assert answer["action"] == ops.NO_CONSENSUS
        assert [one for one in client.calls if one[0] == "run_plugin_task"] == []

    def test_a_running_scan_is_not_disturbed(self, repo, scene, scrapers):
        repo.start_scan(42, "manual", "normal", {}, {})
        client = FakeClient(scene=scene, scrapers=scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})), "scraper.entry",
                              {"scene_id": 42})
        assert answer["action"] == ops.RUNNING
        assert [one for one in client.calls if one[0] == "run_plugin_task"] == []

    def test_corroborated_results_come_back_as_a_scene(self, repo, scene, scrapers):
        # Two independent sites agreeing, which is the weakest thing that qualifies.
        _scan(repo, scene, scrapers, {
            "Example": scraped(title="The Real Title", date="2024-02-02",
                               urls=["https://example.com/scene/1"],
                               performers=["Alice"]),
            "SiteA": scraped(title="The Real Title", date="2024-02-02",
                             urls=["https://sitea.com/scene/5"], performers=["Bob"]),
        })
        client = FakeClient(scene=scene, scrapers=scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})), "scraper.entry",
                              {"scene_id": 42})
        assert answer["action"] == ops.RETURNED
        assert answer["scene"]["title"] == "The Real Title"
        assert answer["consensus"]["independent_sources"] >= 2
        # Reading is not writing: the shim must not have touched the scene.
        assert client.updates == []

    def test_uncorroborated_results_come_back_as_nothing(self, repo, scene, scrapers):
        _scan(repo, scene, scrapers, {
            "Searcher": scraped(title="A Guess", date="2001-01-01",
                                urls=["https://searcher.com/9"]),
        })
        client = FakeClient(scene=scene, scrapers=scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})), "scraper.entry",
                              {"scene_id": 42})
        assert answer["action"] == ops.NO_CONSENSUS
        assert answer["scene"] is None
        assert answer["results"] >= 1
        assert "review" in answer["message"]

    def test_a_scene_id_is_required(self, repo):
        assert ops.dispatch(context(repo), "scraper.entry", {})["ok"] is False

    def test_the_reason_is_reported_so_it_can_be_logged(self, repo, scene, scrapers):
        # The site's own scraper answering the URL the scene already carried.
        _scan(repo, scene, scrapers, {
            "https://example.com/scene/1": scraped(
                title="X", date="2024-02-02", urls=["https://example.com/scene/1"]),
        })
        answer = ops.dispatch(context(repo, FakeClient(scene=scene, scrapers=scrapers)),
                              "scraper.entry", {"scene_id": 42})
        assert answer["action"] == ops.RETURNED
        assert answer["consensus"]["reason"]
        assert answer["consensus"]["sources"]

    def test_the_cover_is_offered_when_one_was_stored(self, repo, scene, scrapers):
        import base64
        image = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffjpg").decode()
        _scan(repo, scene, scrapers, {
            "https://example.com/scene/1": scraped(
                title="X", date="2024-02-02", image=image,
                urls=["https://example.com/scene/1"]),
        })
        answer = ops.dispatch(context(repo, FakeClient(scene=scene, scrapers=scrapers)),
                              "scraper.entry", {"scene_id": 42})
        assert answer["scene"]["image"].startswith("data:image/jpeg;base64,")

        without = ops.dispatch(context(repo, FakeClient(scene=scene, scrapers=scrapers)),
                               "scraper.entry", {"scene_id": 42, "includeImage": False})
        assert "image" not in without["scene"]


class TestConsensusOp:
    def test_the_ui_can_read_the_same_answer(self, repo, scene, scrapers):
        _scan(repo, scene, scrapers, {
            "https://example.com/scene/1": scraped(
                title="X", date="2024-02-02", urls=["https://example.com/scene/1"]),
        })
        answer = ops.dispatch(context(repo, FakeClient(scene=scene, scrapers=scrapers)),
                              "consensus.get", {"scene_id": 42})
        assert answer["consensus"]["reason"]
        assert answer["preview"]["title"] == "X"

    def test_an_unscanned_scene_answers_cleanly(self, repo):
        answer = ops.dispatch(context(repo), "consensus.get", {"scene_id": 999})
        assert answer["consensus"] is None
        assert answer["results"] == 0
