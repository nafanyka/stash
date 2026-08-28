"""The run: every box, every URL, recursively, and nothing written to the scene.

These are the acceptance tests from the specification, written against the fake Stash
so they exercise the real engine end to end without a server.
"""

from __future__ import annotations

import pytest
from fd_common import FakeStash, scraped

from fastdiscovery import discovery, merge
from fastdiscovery.db import repo as R

URL_A = "https://sitea.com/scene/1"
URL_B = "https://siteb.com/v/2"
URL_C = "https://sitec.com/x/3"
URL_D = "https://pornhub.com/view_video.php?viewkey=9"

STASHDB = "https://stashdb.org/graphql"
TPDB = "https://theporndb.net/graphql"


def runner(client, repo, config):
    return discovery.Runner(client, repo, config)


def column_names(repo, run_id):
    return [source["name"] for source in repo.sources_of(run_id)]


class TestEveryBoxRuns:
    def test_every_configured_box_is_asked_even_after_one_matches(
            self, fd_repo, fd_config, fd_scene):
        # The whole point: Stash's own Identify returns at the first source that
        # answers. FastDiscovery must not.
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="Title 1"),
            TPDB: scraped(title="Title 2"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)

        asked = [call[1] for call in client.calls if call[0] == "scrape_scene"]
        assert STASHDB in asked and TPDB in asked
        assert summary["results"] == 2

    def test_a_box_is_asked_by_fingerprint_not_by_a_query_we_built(
            self, fd_repo, fd_config, fd_scene):
        # FastDiscovery must never do its own fingerprint or filename matching: the
        # scene id goes to Stash, and Stash does the lookup (requirement 3).
        seen = {}
        client = FakeStash(scene=fd_scene)
        original = client.scrape_scene

        def record(source, scrape_input, selection, timeout=None):
            if source.get("stash_box_endpoint"):
                seen[source["stash_box_endpoint"]] = scrape_input
            return original(source, scrape_input, selection, timeout)

        client.scrape_scene = record
        runner(client, fd_repo, fd_config).run(295)
        assert seen[STASHDB] == {"scene_id": "295"}
        assert seen[TPDB] == {"scene_id": "295"}

    def test_boxes_are_discovered_from_stash_not_hardcoded(
            self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, boxes=[
            {"name": "SomeFutureBox", "endpoint": "https://example.com/graphql"}])
        runner(client, fd_repo, fd_config).run(295)
        assert any(call[1] == "https://example.com/graphql" for call in client.calls)

    def test_a_name_search_is_off_unless_asked_for(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene)
        runner(client, fd_repo, fd_config).run(295)
        assert not any(str(call[1]).endswith("?query") for call in client.calls)


class TestAcceptanceBasic:
    """Specification section 47."""

    def test_every_source_becomes_its_own_column(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="Title 1", urls=[URL_B]),
            TPDB: scraped(title="Title 2", urls=[URL_C]),
            URL_A: scraped(title="Title 3"),
            URL_B: scraped(title="Title 4", urls=[URL_D]),
            URL_D: scraped(date="2024-01-01"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, fd_scene)

        names = [column["name"] for column in review["columns"]]
        assert names[0] == "Current"
        assert set(names) == {"Current", "StashDB", "ThePornDB", "Site A", "Site B",
                              "Pornhub"}

        titles = _row(review, "title")
        assert [value["display"] for value in titles["values"]] == [
            "Old title", "Title 1", "Title 2", "Title 3", "Title 4"]

        dates = _row(review, "date")
        assert [value["display"] for value in dates["values"]] == ["2024-01-01"]

        urls = _row(review, "urls")
        assert {value["raw"] for value in urls["values"]} == {URL_A, URL_B, URL_C, URL_D}

    def test_the_scene_is_not_touched(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={STASHDB: scraped(title="X")})
        runner(client, fd_repo, fd_config).run(295)
        assert client.updates == []


class TestAcceptanceRecursion:
    """Specification section 51: A -> B -> C -> A must terminate."""

    def test_a_cycle_terminates_and_each_pair_runs_once(self, fd_repo, fd_config,
                                                        fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            URL_A: scraped(title="A", urls=[URL_B]),
            URL_B: scraped(title="B", urls=[URL_C]),
            URL_C: scraped(title="C", urls=[URL_A]),
        })
        summary = runner(client, fd_repo, fd_config).run(295)

        scraped_urls = [call[1] for call in client.calls
                        if call[0] == "scrape_scene_url"]
        assert sorted(scraped_urls) == sorted([URL_A, URL_B, URL_C])
        assert summary["results"] == 3

    def test_the_same_url_spelled_differently_is_one_url(self, fd_repo, fd_config,
                                                         fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            URL_A: scraped(title="A", urls=["http://www.sitea.com/scene/1/?utm_source=x",
                                            URL_B]),
            URL_B: scraped(title="B"),
        })
        runner(client, fd_repo, fd_config).run(295)
        scraped_urls = [call[1] for call in client.calls
                        if call[0] == "scrape_scene_url"]
        assert sorted(scraped_urls) == sorted([URL_A, URL_B])

    def test_depth_is_honoured(self, fd_repo, fd_scene):
        from fastdiscovery import settings
        config = settings.parse({"maxDepth": 1})
        client = FakeStash(scene=fd_scene, responses={
            URL_A: scraped(title="A", urls=[URL_B]),
            URL_B: scraped(title="B", urls=[URL_C]),
            URL_C: scraped(title="C"),
        })
        summary = runner(client, fd_repo, config).run(295)
        scraped_urls = [call[1] for call in client.calls
                        if call[0] == "scrape_scene_url"]
        assert sorted(scraped_urls) == sorted([URL_A, URL_B])

        skipped = [record for record in fd_repo.urls_of(summary["run_id"])
                   if record["state"] == R.U_SKIPPED_DEPTH]
        assert [record["url"] for record in skipped] == [URL_C]

    def test_recursion_can_be_switched_off(self, fd_repo, fd_scene):
        from fastdiscovery import settings
        config = settings.parse({"recursiveUrlDiscovery": False})
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(urls=[URL_B]),
            URL_A: scraped(title="A", urls=[URL_C]),
            URL_B: scraped(title="B"),
        })
        runner(client, fd_repo, config).run(295)
        scraped_urls = sorted(call[1] for call in client.calls
                              if call[0] == "scrape_scene_url")
        # Depth 0 is the scene's URLs plus whatever the boxes returned; URL_C came out
        # of a URL scraper and so is depth 1, which recursion off does not reach.
        assert scraped_urls == sorted([URL_A, URL_B])


class TestAcceptanceSameScraperManyUrls:
    """Specification section 52."""

    def test_two_urls_through_one_scraper_are_two_results(self, fd_repo, fd_config,
                                                          fd_scene):
        second = "https://pornhub.com/view_video.php?viewkey=222"
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(urls=[URL_D, second]),
            URL_D: scraped(title="From URL D"),
            second: scraped(title="From URL 222"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, fd_scene)

        pornhub = [column for column in review["columns"]
                   if column["name"] == "Pornhub"]
        assert len(pornhub) == 2
        assert {column["url"] for column in pornhub} == {URL_D, second}
        assert {value["display"] for value in _row(review, "title")["values"]} >= {
            "From URL D", "From URL 222"}


class TestAcceptanceFailure:
    """Specification section 54: one source failing is not the run failing."""

    def test_a_failed_source_is_visible_and_the_run_is_still_reviewable(
            self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="Good"),
            TPDB: TimeoutError("timed out after 30s"),
            URL_A: scraped(title="Also good"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        assert summary["status"] == R.READY_WITH_ERRORS

        by_name = {source["name"]: source
                   for source in fd_repo.sources_of(summary["run_id"])}
        assert by_name["StashDB"]["status"] == R.S_OK
        assert by_name["ThePornDB"]["status"] == R.S_TIMEOUT
        assert "timed out" in by_name["ThePornDB"]["error"]
        assert by_name["Site A"]["status"] == R.S_OK

    def test_an_erroring_source_does_not_stop_the_others(self, fd_repo, fd_config,
                                                         fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: RuntimeError("HTTP 500 from the box"),
            TPDB: scraped(title="Still here"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        assert summary["errors"] == 1
        assert summary["results"] == 1

    def test_nothing_anywhere_is_a_finished_run_not_a_failure(self, fd_repo, fd_config,
                                                              fd_scene):
        client = FakeStash(scene=fd_scene, responses={})
        summary = runner(client, fd_repo, fd_config).run(295)
        assert summary["status"] == R.NO_RESULTS

    def test_a_missing_scene_is_reported_not_swallowed(self, fd_repo, fd_config):
        client = FakeStash(scene={"id": "1"})
        with pytest.raises(discovery.SceneMissing):
            runner(client, fd_repo, fd_config).run(295)


class TestAmbiguousUrls:
    """Stash cannot aim a URL scrape; the run says so instead of pretending."""

    def test_a_shared_url_is_scraped_once_and_aimed_at_the_fragment_scraper(
            self, fd_repo, fd_config, fd_scene):
        shared = "https://shared.com/scene/7"
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(urls=[shared]),
            shared: scraped(title="whoever answered"),
            "SharedOne@" + shared: scraped(title="Shared One says"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        by_name = {source["name"]: source
                   for source in fd_repo.sources_of(summary["run_id"])}

        assert by_name["auto (Shared One, Shared Two)"]["attribution"] == "AMBIGUOUS"
        assert by_name["Shared One"]["status"] == R.S_OK
        # Shared Two is URL-only, so no public Stash API can reach it for this URL.
        assert by_name["Shared Two"]["status"] == R.S_UNREACHABLE
        assert "cannot aim" in by_name["Shared Two"]["error"]

    def test_a_single_handler_url_is_attributed_with_certainty(
            self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={URL_A: scraped(title="A")})
        summary = runner(client, fd_repo, fd_config).run(295)
        source = [one for one in fd_repo.sources_of(summary["run_id"])
                  if one["url"] == URL_A][0]
        assert source["attribution"] == "CERTAIN"
        assert source["scraper_id"] == "SiteA"


class TestGuards:
    def test_a_url_no_scraper_matches_is_recorded_not_scraped(
            self, fd_repo, fd_config, fd_scene):
        unknown = "https://nobody-scrapes-this.example/scene/1"
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(urls=[unknown]),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        record = [one for one in fd_repo.urls_of(summary["run_id"])
                  if one["url"] == unknown][0]
        assert record["state"] == R.U_NO_HANDLER
        assert not any(call[1] == unknown for call in client.calls)

    def test_orchestrator_scrapers_are_never_invoked(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, scrapers=[
            {"id": "FastDiscovery", "name": "FastDiscovery",
             "scene": {"urls": ["sitea.com"], "supported_scrapes": ["URL", "FRAGMENT"]}},
        ], responses={URL_A: scraped(title="should never happen")})
        summary = runner(client, fd_repo, fd_config).run(295)
        assert not any(call[0] == "scrape_scene_url" for call in client.calls)
        record = [one for one in fd_repo.urls_of(summary["run_id"])
                  if one["url"] == URL_A][0]
        assert record["state"] == R.U_NO_HANDLER

    def test_an_all_null_result_is_a_no_match_not_an_empty_column(
            self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: {"title": None, "date": None, "urls": []},
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        assert summary["results"] == 0
        by_name = {source["name"]: source
                   for source in fd_repo.sources_of(summary["run_id"])}
        assert by_name["StashDB"]["status"] == R.S_NO_RESULT

    def test_a_rescan_replaces_the_run_waiting_for_review(self, fd_repo, fd_config,
                                                          fd_scene):
        client = FakeStash(scene=fd_scene, responses={STASHDB: scraped(title="One")})
        first = runner(client, fd_repo, fd_config).run(295)
        second = runner(client, fd_repo, fd_config).run(295)
        assert fd_repo.run(first["run_id"]) is None
        assert fd_repo.run(second["run_id"])["status"] == R.READY_FOR_REVIEW


class TestCounts:
    def test_the_counts_include_sources_that_were_never_dispatched(
            self, fd_repo, fd_config, fd_scene):
        # A scraper no API can reach is a source the review shows, so it has to be a
        # source the counts admit to as well.
        shared = "https://shared.com/scene/7"
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(urls=[shared]),
            shared: scraped(title="whoever answered"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        statuses = [source["status"]
                    for source in fd_repo.sources_of(summary["run_id"])]
        assert run["source_count"] == len(statuses)
        assert run["ok_source_count"] == statuses.count(R.S_OK)
        assert run["error_count"] == statuses.count(R.S_UNREACHABLE)


class TestProvenance:
    def test_a_url_records_which_result_found_it(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="One", urls=[URL_B]),
            URL_B: scraped(title="Two"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, fd_scene)
        found = {entry["url"]: entry for entry in review["urls_graph"]}
        assert found[URL_A]["found_by"] == "scene"
        assert found[URL_B]["found_by"] == "StashDB"
        assert found[URL_B]["depth"] == 0

    def test_a_result_knows_the_source_it_came_from(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="One", urls=[URL_B]),
            URL_B: scraped(title="Two"),
        })
        summary = runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, fd_scene)
        site_b = [column for column in review["columns"]
                  if column["name"] == "Site B"][0]
        stashdb = [column for column in review["columns"]
                   if column["name"] == "StashDB"][0]
        assert site_b["parent"] == stashdb["id"]
        assert site_b["depth"] == 0


def _row(review, field):
    for row in review["rows"]:
        if row["field"] == field:
            return row
    raise AssertionError("no %s row: %s" % (field, [one["field"]
                                                    for one in review["rows"]]))
