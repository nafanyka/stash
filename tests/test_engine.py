"""The discovery engine, against a faked Stash.

The contract being tested is the one the whole design rests on: a scan acquires and
stores, one scraper's failure never ends it, and nothing is ever written back to the
scene.
"""

from __future__ import annotations

import base64

from conftest import FakeClient, scraped
from scrapediscovery import cache, engine as E, registry as G, settings as S, stash
from scrapediscovery.db import migrations, repo as R


class StubSchema:
    """A fixed selection: what the server supports is not what these tests are about."""

    selection = "title date urls image studio { name } performers { name } tags { name }"


def make(repo, client, raw_config=None):
    config = S.parse(raw_config or {})
    engine = E.Engine(client, repo, config, schema=StubSchema())
    return engine


IMAGE = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffjpeg").decode()


class TestOutcomes:
    def test_each_kind_of_answer_is_recorded_as_itself(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Example Scene", date="2024-01-20"),
            "SiteA": [],                                     # nothing found
            "Searcher": RuntimeError("http error 404:Not Found"),
            "Filename": TimeoutError("timed out after 30s"),
            "https://example.com/scene/1": scraped(title="Example Scene"),
        })
        engine = make(repo, client, {"excludedScrapers": ""})
        summary = engine.scan(42)

        statuses = {}
        for attempt in repo.attempts_of_scan(summary["scan_id"]):
            key = attempt["scraper_id"] or "auto"
            statuses.setdefault(key, set()).add(attempt["status"])

        assert "MATCH" in statuses["Example"]
        assert statuses["SiteA"] == {"NO_MATCH"}
        assert statuses["Searcher"] == {"ERROR"}
        assert statuses["Filename"] == {"TIMEOUT"}
        assert summary["matches"] >= 1

    def test_one_broken_scraper_does_not_end_the_scan(self, repo, scene, scrapers):
        # A scraper on the live instance panics Stash's resolver; the scan has to
        # survive that and still report the scrapers that worked.
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "SiteA": RuntimeError(
                "Internal system error. Error <runtime error: invalid memory address"
                " or nil pointer dereference>"),
            "Example": scraped(title="Example Scene"),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        assert summary["errors"] >= 1
        assert summary["matches"] >= 1
        assert repo.scan(summary["scan_id"])["status"] == R.SCAN_WARNINGS

    def test_a_permanent_error_is_classified_when_stored(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "SiteA": RuntimeError("scraper SiteA: scraper script error: exit status 69"),
            "Searcher": RuntimeError("connection reset by peer"),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        kinds = {attempt["scraper_id"]: attempt["error_kind"]
                 for attempt in repo.attempts_of_scan(summary["scan_id"])
                 if attempt["status"] == "ERROR"}
        assert kinds["SiteA"] == cache.PERMANENT
        assert kinds["Searcher"] == cache.TRANSIENT

    def test_an_all_null_payload_is_a_no_match(self, repo, scene, scrapers):
        # Storing it would put an empty candidate in front of the user.
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": {"title": None, "date": None, "performers": [], "urls": []},
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        example = [one for one in repo.attempts_of_scan(summary["scan_id"])
                   if one["scraper_id"] == "Example"
                   and one["method"] == G.M_FRAGMENT_SCENE][0]
        assert example["status"] == "NO_MATCH"
        assert repo.result_count_for_scene(42) == 0

    def test_a_missing_scene_is_an_error_not_an_empty_scan(self, repo, scrapers):
        client = FakeClient(scene={"id": "1"}, scrapers=scrapers)
        try:
            make(repo, client).scan(999)
        except ValueError as exc:
            assert "999" in str(exc)
        else:
            raise AssertionError("scanning a scene that does not exist should fail")


class TestStorage:
    def test_raw_and_normalised_forms_are_both_kept(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="  Example Scene  ", date="January 20, 2024",
                               performers=["Alice", "Bob"]),
        })
        make(repo, client, {"excludedScrapers": ""}).scan(42)
        result = repo.results_of_scene(42, include_raw=True)[0]

        # Raw keeps what the scraper said, warts and all; normalised is what compares.
        assert result["raw"]["title"] == "  Example Scene  "
        assert result["normalized"]["title"] == "Example Scene"
        assert result["normalized"]["date"] == "2024-01-20"
        assert result["raw_fingerprint"]
        assert result["norm_version"] == migrations.NORM_VERSION

    def test_a_name_search_stores_every_returned_result(self, repo, scene, scrapers):
        # A name search answers with a list - a dozen near-misses is normal - and each
        # entry is a separate potential candidate.
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Searcher": [scraped(title="First"), scraped(title="Second"),
                         scraped(title="Third")],
        })
        engine = make(repo, client, {"excludedScrapers": "",
                                     "normalIncludesNameScrapers": True})
        engine.scan(42)
        titles = {one["normalized"]["title"] for one in repo.results_of_scene(42)}
        assert {"First", "Second", "Third"} <= titles

    def test_images_are_externalised_and_shared(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", image=IMAGE),
            "SiteA": scraped(title="B", image=IMAGE),
        })
        make(repo, client, {"excludedScrapers": ""}).scan(42)

        assert repo.counts()["blobs"] == 1  # the same cover from two scrapers
        for result in repo.results_of_scene(42, include_raw=True):
            if result["image_sha256"]:
                assert isinstance(result["raw"]["image"], dict)
                assert result["raw"]["image"]["$blob"] == result["image_sha256"]

    def test_the_scene_snapshot_is_kept_with_the_scan(self, repo, scene, scrapers):
        # What was known at the time is what a score can be explained against later.
        summary = make(repo, FakeClient(scene=scene, scrapers=scrapers)).scan(42)
        stored = repo.scan(summary["scan_id"])["scene_snapshot"]
        assert stored["title"] == "Example Scene"
        assert stored["duration"] == 1800


class TestNoWrites:
    def test_a_scan_never_touches_the_scene(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Different Title", date="1999-01-01",
                               performers=["Someone Else"], tags=["New Tag"],
                               urls=["https://elsewhere.com/x/1"]),
        })
        make(repo, client, {"excludedScrapers": ""}).scan(42)
        # The whole point of the design: discovery acquires, it does not apply.
        assert client.updates == []


class TestAttribution:
    def test_a_url_with_one_handler_is_attributed(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "https://example.com/scene/1": scraped(title="From the URL"),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        url_attempt = [one for one in repo.attempts_of_scan(summary["scan_id"])
                       if one["method"] == G.M_URL][0]
        assert url_attempt["scraper_id"] == "Example"
        assert url_attempt["attribution"] == G.CERTAIN

    def test_a_url_several_scrapers_match_is_marked_uncertain(self, repo, scene):
        shared = [
            {"id": "One", "name": "One",
             "scene": {"urls": ["example.com/"], "supported_scrapes": ["URL"]}},
            {"id": "Two", "name": "Two",
             "scene": {"urls": ["example.com/scene/"], "supported_scrapes": ["URL"]}},
        ]
        client = FakeClient(scene=scene, scrapers=shared, responses={
            "https://example.com/scene/1": scraped(title="Ambiguous"),
        })
        summary = make(repo, client).scan(42)
        url_attempt = [one for one in repo.attempts_of_scan(summary["scan_id"])
                       if one["method"] == G.M_URL][0]
        # Stash chose the handler and does not report which; claiming one would lie.
        assert url_attempt["scraper_id"] is None
        assert url_attempt["attribution"] == G.AMBIGUOUS
        assert sorted(url_attempt["input_json"] and
                      __import__("json").loads(url_attempt["input_json"])
                      ["possible_handlers"]) == ["One", "Two"]


class TestUrlDiscovery:
    def test_urls_in_results_are_recorded_with_their_handlers(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://sitea.com/scene/99",
                                                 "https://nowhere.invalid/x"]),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        urls = {one["norm_key"]: one for one in repo.urls_of_scan(summary["scan_id"])}

        assert urls["sitea.com/scene/99"]["handlers"] == ["SiteA"]
        assert urls["sitea.com/scene/99"]["state"] == "PENDING"
        # Recorded even with nothing to follow it: that is still discovery information.
        assert urls["nowhere.invalid/x"]["state"] == "NO_HANDLER"

    def test_a_url_the_scene_already_has_is_not_re_added(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://example.com/scene/1"]),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        keys = [one["norm_key"] for one in repo.urls_of_scan(summary["scan_id"])]
        assert "example.com/scene/1" not in keys

    def test_the_same_url_from_two_scrapers_is_stored_once(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://sitea.com/scene/7"]),
            "SiteA": scraped(title="B", urls=["https://www.sitea.com/scene/7/?utm_source=x"]),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        keys = [one["norm_key"] for one in repo.urls_of_scan(summary["scan_id"])]
        assert keys.count("sitea.com/scene/7") == 1

    def test_related_urls_are_left_alone_by_default(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": {"title": "A", "performers": [
                {"name": "Alice", "url": "https://sitea.com/scene/model"}]},
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        assert repo.urls_of_scan(summary["scan_id"]) == []

    def test_related_urls_can_be_turned_on(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": {"title": "A", "performers": [
                {"name": "Alice", "url": "https://sitea.com/scene/model"}]},
        })
        engine = make(repo, client, {"excludedScrapers": "", "expandRelatedUrls": True})
        summary = engine.scan(42)
        assert [one["norm_key"] for one in repo.urls_of_scan(summary["scan_id"])] == \
            ["sitea.com/scene/model"]


class TestExpansion:
    def test_a_discovered_url_is_followed_and_the_graph_recorded(self, repo, scene,
                                                                 scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://sitea.com/scene/2"]),
            "https://sitea.com/scene/2": scraped(title="Followed"),
        })
        engine = make(repo, client, {"excludedScrapers": ""})
        summary = engine.scan(42, expand_urls=True)

        followed = [one for one in repo.attempts_of_scan(summary["scan_id"])
                    if one["target"] == "https://sitea.com/scene/2"]
        assert len(followed) == 1
        assert followed[0]["depth"] == 1
        assert followed[0]["status"] == "MATCH"
        # The parent edge is what lets the UI say how a candidate was reached.
        assert followed[0]["parent_id"] is not None

    def test_expansion_stops_at_the_configured_depth(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://sitea.com/scene/d1"]),
            "https://sitea.com/scene/d1": scraped(title="D1",
                                                  urls=["https://siteb.com/v/d2"]),
            "https://siteb.com/v/d2": scraped(title="D2",
                                              urls=["https://sitea.com/scene/d3"]),
        })
        engine = make(repo, client, {"excludedScrapers": "", "maxDepth": 2})
        summary = engine.scan(42, expand_urls=True)

        depths = {one["depth"] for one in repo.attempts_of_scan(summary["scan_id"])
                  if one["method"] == G.M_URL and one["depth"] > 0}
        assert depths == {1, 2}
        states = {one["norm_key"]: one["state"]
                  for one in repo.urls_of_scan(summary["scan_id"])}
        assert states["sitea.com/scene/d3"] == "SKIPPED_DEPTH"

    def test_a_url_cycle_cannot_loop(self, repo, scene, scrapers):
        # Two pages pointing at each other is the obvious way to spin forever.
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="A", urls=["https://sitea.com/scene/loop"]),
            "https://sitea.com/scene/loop": scraped(
                title="Loop", urls=["https://siteb.com/v/loop"]),
            "https://siteb.com/v/loop": scraped(
                title="Back", urls=["https://sitea.com/scene/loop"]),
        })
        engine = make(repo, client, {"excludedScrapers": "", "maxDepth": 5})
        summary = engine.scan(42, expand_urls=True)

        targets = [one["target"] for one in repo.attempts_of_scan(summary["scan_id"])
                   if one["method"] == G.M_URL]
        assert targets.count("https://sitea.com/scene/loop") == 1
        assert targets.count("https://siteb.com/v/loop") == 1

    def test_the_url_limit_is_respected_and_reported(self, repo, scene, scrapers):
        many = ["https://sitea.com/scene/%d" % index for index in range(10)]
        responses = {"Example": scraped(title="A", urls=many)}
        for url in many:
            responses[url] = scraped(title="deep")
        client = FakeClient(scene=scene, scrapers=scrapers, responses=responses)

        engine = make(repo, client, {"excludedScrapers": "", "maxUrlsPerScan": 3})
        summary = engine.scan(42, expand_urls=True)

        followed = [one for one in repo.attempts_of_scan(summary["scan_id"])
                    if one["method"] == G.M_URL and one["depth"] == 1]
        assert len(followed) == 3
        # Nothing is dropped silently: the rest say why they were left.
        states = [one["state"] for one in repo.urls_of_scan(summary["scan_id"])]
        assert states.count("SKIPPED_LIMIT") == 7


class TestLimits:
    def test_the_attempt_ceiling_truncates_the_plan(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        engine = make(repo, client, {"excludedScrapers": "", "maxAttemptsPerScan": 2})
        summary = engine.scan(42)
        assert summary["planned"] == 2
        assert len(repo.attempts_of_scan(summary["scan_id"])) == 2

    def test_an_exhausted_time_budget_records_what_was_skipped(self, repo, scene,
                                                               scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        engine = make(repo, client, {"excludedScrapers": "", "sceneTimeBudget": 0.001})
        summary = engine.scan(42)

        attempts = repo.attempts_of_scan(summary["scan_id"])
        skipped = [one for one in attempts if one["status"] == "SKIPPED"]
        assert skipped
        assert all(one["error"] for one in skipped)
        assert repo.scan(summary["scan_id"])["stop_reason"]


class TestCaching:
    def test_a_second_scan_reuses_the_first(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Example Scene"),
        })
        engine = make(repo, client, {"excludedScrapers": ""})
        first = engine.scan(42)
        calls_after_first = len(client.calls)

        second = engine.scan(42)
        assert second["cached"] == first["planned"]
        assert second["done"] == 0
        assert len(client.calls) == calls_after_first  # no external work at all

    def test_the_cached_copy_still_finds_the_results(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Example Scene"),
        })
        engine = make(repo, client, {"excludedScrapers": ""})
        engine.scan(42)
        second = engine.scan(42)

        cached = [one for one in repo.attempts_of_scan(second["scan_id"])
                  if one["from_cache"] and one["status"] == "MATCH"]
        assert cached
        assert repo.results_via(cached[0])
        # And the payload was not duplicated.
        assert repo.counts()["results"] == 1

    def test_ignoring_the_cache_re_runs_everything(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Example Scene"),
        })
        engine = make(repo, client, {"excludedScrapers": ""})
        engine.scan(42)
        before = len(client.calls)
        again = engine.scan(42, ignore_cache=True)
        assert again["cached"] == 0
        assert len(client.calls) > before


class TestBookkeeping:
    def test_the_registry_is_recorded_so_new_scrapers_can_be_told_apart(self, repo, scene,
                                                                       scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        make(repo, client, {"excludedScrapers": ""}).scan(42)
        known = repo.known_scrapers()
        assert set(known) == {one["id"] for one in scrapers}
        assert repo.scrapers_tried(42)

    def test_the_scene_state_reflects_the_scan(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers, responses={
            "Example": scraped(title="Example Scene"),
        })
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        state = repo.scene_state(42)
        assert state["status"] == R.RESULTS
        assert state["title"] == "Example Scene"
        assert state["attempt_count"] == summary["planned"]

    def test_progress_is_reported_and_stored(self, repo, scene, scrapers):
        seen = []
        client = FakeClient(scene=scene, scrapers=scrapers)
        engine = make(repo, client, {"excludedScrapers": "", "maxConcurrency": 1})
        summary = engine.scan(42, progress_hook=lambda state: seen.append(state.fraction()))
        stored = repo.scan(summary["scan_id"])["progress"]
        assert stored["planned"] == summary["planned"]
        assert all(0.0 <= one <= 1.0 for one in seen)

    def test_a_scan_records_why_it_stopped_or_that_it_finished(self, repo, scene,
                                                               scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        summary = make(repo, client, {"excludedScrapers": ""}).scan(42)
        row = repo.scan(summary["scan_id"])
        assert row["status"] in (R.SCAN_COMPLETED, R.SCAN_WARNINGS)
        assert row["finished_at"]


class TestConcurrency:
    def test_results_are_identical_whatever_the_concurrency(self, repo, scene, scrapers,
                                                            tmp_path):
        from scrapediscovery.db import repo as repo_module

        def run(concurrency, path):
            store = repo_module.Repo.open(str(path))
            client = FakeClient(scene=scene, scrapers=scrapers, responses={
                "Example": scraped(title="A", urls=["https://sitea.com/scene/2"]),
                "SiteA": scraped(title="B"),
                "Searcher": RuntimeError("http error 404"),
            })
            engine = make(store, client, {"excludedScrapers": "",
                                          "maxConcurrency": concurrency})
            summary = engine.scan(42)
            statuses = sorted(
                (one["scraper_id"] or "auto", one["status"])
                for one in store.attempts_of_scan(summary["scan_id"]))
            store.close()
            return statuses

        assert run(1, tmp_path / "one.sqlite") == run(4, tmp_path / "four.sqlite")
