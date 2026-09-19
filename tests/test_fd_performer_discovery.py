"""Fast/Full performer discovery: the acceptance scenarios from the specification.

Written against the fake Stash, exercising `performer_discovery.PerformerRunner` end to
end - no server, no network. Mirrors `test_fd_discovery.py`'s style for the scene engine.
"""

from __future__ import annotations

import json

import pytest
from fd_common import FakeStash, scraped_performer

from fastdiscovery import performer_discovery, settings as settings_module
from fastdiscovery.db import repo as R

PERFORMER = {"id": "42", "name": "Bonnie Alex", "urls": [], "alias_list": [],
             "tags": [], "stash_ids": []}


def perf_config(fast_scrapers=(), **overrides):
    values = {"performerFastScrapers": json.dumps(list(fast_scrapers))}
    values.update(overrides)
    return settings_module.parse(values)


def runner(client, repo, config):
    return performer_discovery.PerformerRunner(client, repo, config)


class TestNameResults:
    """Specification section 3 / test A & B: every result kept, not just the first."""

    def test_a_single_result_becomes_one_column(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia"])
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["results"] == 1
        results = fd_repo.results_of(summary["run_id"])
        assert len(results) == 1

    def test_five_results_from_one_scraper_become_five_columns(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": [
                scraped_performer(name="Bonnie Alex"),
                scraped_performer(name="Bonnie Alexis"),
                scraped_performer(name="Bonnie Alexandra"),
                scraped_performer(name="Bonnie A."),
                scraped_performer(name="Bonnie X"),
            ],
        })
        config = perf_config(["Babepedia"], maxResultsPerSource=10)
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["results"] == 5
        names = sorted(r["raw"]["name"] for r in fd_repo.results_of(summary["run_id"]))
        assert names == ["Bonnie A.", "Bonnie Alex", "Bonnie Alexandra", "Bonnie Alexis",
                         "Bonnie X"]

        # Each column's header must say *which* Babepedia result it is - not "#2",
        # "#3" - or five columns from one scraper are indistinguishable at a glance.
        from fastdiscovery import merge
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        column_names = sorted(c["name"] for c in review["columns"] if c["id"] != "current")
        assert column_names == [
            "Babepedia — Bonnie A.", "Babepedia — Bonnie Alex",
            "Babepedia — Bonnie Alexandra", "Babepedia — Bonnie Alexis",
            "Babepedia — Bonnie X",
        ]

    def test_a_removed_fast_scraper_is_skipped_silently(self, fd_repo):
        # requirement 23: a scraper picked in settings but no longer installed must
        # not fail the run.
        client = FakeStash(performer=PERFORMER, performer_scrapers=[
            {"id": "Babepedia", "name": "Babepedia",
             "performer": {"urls": [], "supported_scrapes": ["NAME"]}}])
        config = perf_config(["Babepedia", "GoneScraper"])
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["status"] in (R.NO_RESULTS, R.READY_FOR_REVIEW)
        asked = [call[1] for call in client.calls if call[0] == "scrape_single_performer"]
        assert not any("GoneScraper" in one for one in asked)


class TestNameSearchHitsGetTheirOwnFullProfile:
    """A name search's own results are only ever `{name, url}` - Babepedia's real
    shape, and most non-stash-box scrapers' - never a full profile. Fast's depth-0
    follow-up (see `_expand_urls`) is what turns each candidate's own `url` into a
    second, fuller column, exactly the two-step dance Stash's own scrape-by-name
    modal does when a person clicks a candidate."""

    def test_each_of_several_candidates_gets_scraped_by_its_own_url(self, fd_repo):
        url_a = "https://www.babepedia.com/babe/Kloe_Love"
        url_b = "https://www.babepedia.com/babe/Kloey_Love"
        client = FakeStash(performer=PERFORMER, boxes=[], responses={
            "pname:Babepedia:Bonnie Alex": [
                {"name": "Kloe Love (Kloe Love)", "url": url_a},
                {"name": "Kloey Love", "url": url_b},
            ],
            "purl:" + url_a: scraped_performer(name="Kloe Love",
                                                images=["https://img/a.jpg"]),
            "purl:" + url_b: scraped_performer(name="Kloey Love",
                                               images=["https://img/b.jpg"]),
        })
        config = perf_config(["Babepedia"])
        summary = runner(client, fd_repo, config).run_fast(42)

        scraped_urls = sorted(call[1] for call in client.calls
                              if call[0] == "scrape_performer_url")
        assert scraped_urls == sorted([url_a, url_b])

        from fastdiscovery import merge
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        columns = [c for c in review["columns"] if c["id"] != "current"]
        # Two bare search hits (no url of their own to speak of - `url` on a column
        # is the *scraped* url, not the search query) plus, once the depth-0
        # follow-up ran, one fuller column per hit, each a child of the hit that
        # led to it, not a replacement for it.
        hits = [c for c in columns if not c["url"]]
        full = {c["url"]: c for c in columns if c["url"]}
        assert len(hits) == 2
        assert set(full) == {url_a, url_b}
        by_id = {c["id"]: c for c in columns}
        assert by_id[full[url_a]["parent"]] in hits
        assert by_id[full[url_b]["parent"]] in hits
        image_row = [r for r in review["rows"] if r["kind"] == "image"][0]
        assert len(image_row["values"]) == 2

    def test_a_tight_maxUrlsPerRun_does_not_swallow_a_candidates_own_url(self, fd_repo):
        # requirement: a name-search hit's own identifying url is the guaranteed
        # second half of a result this run already committed to, never optional
        # discovery competing for the same budget as an arbitrary link mentioned
        # inside some other source's content - so it must survive even when that
        # budget is already spent by something else entirely.
        url_a = "https://www.babepedia.com/babe/Kloe_Love"
        url_b = "https://www.babepedia.com/babe/Kloey_Love"
        STASHDB = "https://stashdb.org/graphql"
        client = FakeStash(performer=PERFORMER, boxes=[
            {"name": "StashDB", "endpoint": STASHDB}], responses={
            "pbox:%s:Bonnie Alex" % STASHDB: scraped_performer(
                name="Bonnie Alex",
                urls=["https://x.example/1", "https://x.example/2",
                     "https://x.example/3"]),
            "pname:Babepedia:Bonnie Alex": [
                {"name": "Kloe Love (Kloe Love)", "url": url_a},
                {"name": "Kloey Love", "url": url_b},
            ],
            "purl:" + url_a: scraped_performer(name="Kloe Love"),
            "purl:" + url_b: scraped_performer(name="Kloey Love"),
        })
        config = perf_config(["Babepedia"], maxUrlsPerRun=1)
        summary = runner(client, fd_repo, config).run_fast(42)

        scraped_urls = sorted(call[1] for call in client.calls
                              if call[0] == "scrape_performer_url")
        assert scraped_urls == sorted([url_a, url_b])


class TestStashBoxes:
    """Every configured stash-box is asked unconditionally, before the picked
    scrapers, on both Fast and Full - never a setting to opt into, exactly the way
    scene discovery always asks every box (requirement: stash-boxes take priority).
    """

    STASHDB = "https://stashdb.org/graphql"
    TPDB = "https://theporndb.net/graphql"

    def test_every_configured_box_is_asked_with_no_scrapers_picked_at_all(
            self, fd_repo):
        client = FakeStash(performer=PERFORMER, boxes=[
            {"name": "StashDB", "endpoint": self.STASHDB},
            {"name": "ThePornDB", "endpoint": self.TPDB}], responses={
            "pbox:%s:Bonnie Alex" % self.STASHDB: scraped_performer(name="Bonnie Alex"),
            "pbox:%s:Bonnie Alex" % self.TPDB: scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config([])   # no Fast scrapers picked in settings at all
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["results"] == 2
        asked = sorted(c[1] for c in client.calls if c[0] == "scrape_single_performer")
        assert asked == sorted(["pbox:%s:Bonnie Alex" % self.STASHDB,
                                "pbox:%s:Bonnie Alex" % self.TPDB])

    def test_boxes_and_picked_scrapers_both_run_in_one_fast_pass(self, fd_repo):
        client = FakeStash(performer=PERFORMER, boxes=[
            {"name": "StashDB", "endpoint": self.STASHDB}], responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
            "pbox:%s:Bonnie Alex" % self.STASHDB: scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia"])
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["results"] == 2

    def test_boxes_run_even_when_nothing_is_installed_or_picked(self, fd_repo):
        # No performer-name scrapers at all, just a configured box - discovery must
        # still try it rather than doing nothing (boxes are never optional).
        client = FakeStash(performer=PERFORMER, performer_scrapers=[], boxes=[
            {"name": "StashDB", "endpoint": self.STASHDB}], responses={
            "pbox:%s:Bonnie Alex" % self.STASHDB: scraped_performer(name="Bonnie Alex"),
        })
        summary = runner(client, fd_repo, perf_config([])).run_fast(42)
        assert summary["results"] == 1

    def test_full_does_not_re_query_a_box_fast_already_used_but_picks_up_a_new_one(
            self, fd_repo):
        client = FakeStash(performer=PERFORMER, boxes=[
            {"name": "StashDB", "endpoint": self.STASHDB}], responses={
            "pbox:%s:Bonnie Alex" % self.STASHDB: scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config([])
        run = runner(client, fd_repo, config)
        fast_summary = run.run_fast(42)
        asked_after_fast = [c[1] for c in client.calls if c[0] == "scrape_single_performer"]
        assert asked_after_fast == ["pbox:%s:Bonnie Alex" % self.STASHDB]

        # A second box gets added to Stash's configuration between Fast and Full.
        client.boxes = [{"name": "StashDB", "endpoint": self.STASHDB},
                        {"name": "ThePornDB", "endpoint": self.TPDB}]
        client.responses["pbox:%s:Bonnie Alex" % self.TPDB] = scraped_performer(
            name="Bonnie Alex")
        run.run_full(fast_summary["run_id"])
        asked_after_full = [c[1] for c in client.calls if c[0] == "scrape_single_performer"]
        assert asked_after_full.count("pbox:%s:Bonnie Alex" % self.STASHDB) == 1
        assert "pbox:%s:Bonnie Alex" % self.TPDB in asked_after_full


class TestFastThenFull:
    """Specification section 26 / test G: Full never repeats a Fast scraper."""

    def test_full_only_runs_scrapers_fast_did_not_use(self, fd_repo):
        # No stash-boxes here: this test is about the scraper picklist specifically,
        # and the fake's default boxes would add two more (harmless, but noise for
        # an exact-list assertion) - see TestStashBoxes for box behaviour.
        client = FakeStash(performer=PERFORMER, boxes=[], responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
            "pname:IAFD:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
            "pname:StashDB:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia"])
        run = runner(client, fd_repo, config)
        fast_summary = run.run_fast(42)
        assert fast_summary["status"] == R.READY_FOR_REVIEW

        asked_after_fast = [c[1] for c in client.calls if c[0] == "scrape_single_performer"]
        assert asked_after_fast == ["pname:Babepedia:Bonnie Alex"]

        full_summary = run.run_full(fast_summary["run_id"])
        asked_after_full = [c[1] for c in client.calls if c[0] == "scrape_single_performer"]
        # Babepedia (Fast) is not asked again; IAFD and StashDB (Full) are.
        assert asked_after_full.count("pname:Babepedia:Bonnie Alex") == 1
        assert "pname:IAFD:Bonnie Alex" in asked_after_full
        assert "pname:StashDB:Bonnie Alex" in asked_after_full
        assert full_summary["results"] == 3   # Fast's 1 plus Full's 2, not just Full's

        run_row = fd_repo.run(fast_summary["run_id"])
        assert run_row["mode"] == "FULL"

    def test_full_keeps_existing_fast_results(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
            "pname:IAFD:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia"])
        run = runner(client, fd_repo, config)
        summary = run.run_fast(42)
        before = fd_repo.results_of(summary["run_id"])
        assert len(before) == 1

        run.run_full(summary["run_id"])
        after = fd_repo.results_of(summary["run_id"])
        assert len(after) == 2   # Fast's result is still there, Full's is added


class TestFullFailureKeepsFastReviewable:
    """Full runs on top of an already-reviewable result; if Full itself blows up
    (not one scraper failing - that never raises), Fast's results must stay
    reviewable rather than the whole run coming out FAILED."""

    def test_full_crashing_does_not_destroy_fasts_reviewable_result(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia"])
        fast_summary = runner(client, fd_repo, config).run_fast(42)
        assert fast_summary["status"] == R.READY_FOR_REVIEW

        broken = runner(client, fd_repo, config)
        broken._expand_urls = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            broken.run_full(fast_summary["run_id"])

        after = fd_repo.run(fast_summary["run_id"])
        assert after["status"] in (R.READY_FOR_REVIEW, R.READY_WITH_ERRORS)
        assert after["reviewable"] is True
        assert after["mode"] == "FAST"   # Full never completed, so it never became FULL
        assert fd_repo.results_of(fast_summary["run_id"])   # Fast's result is intact


class TestOneFailureDoesNotStopTheRun:
    """Specification section 19 / test P."""

    def test_one_scraper_erroring_does_not_stop_the_others(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
            "pname:IAFD:Bonnie Alex": RuntimeError("timed out"),
            "pname:StashDB:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config(["Babepedia", "IAFD", "StashDB"])
        summary = runner(client, fd_repo, config).run_fast(42)
        assert summary["status"] == R.READY_WITH_ERRORS
        assert summary["results"] == 2
        assert summary["errors"] == 1


class TestUrlRecursion:
    """Specification section 4 / tests H & I: cycles stop, maxDepth is respected.

    URL expansion is Full's job: Fast only seeds the frontier so the button stays
    quick (requirement: no URL parsing in Fast). Each test runs Fast then tops the
    same run up with Full before checking what the frontier actually did.
    """

    def test_a_cycle_terminates(self, fd_repo):
        url_a = "https://pshared.com/a"
        url_b = "https://perfa.com/b"
        client = FakeStash(performer=dict(PERFORMER, urls=[url_a]), responses={
            "purl:" + url_a: scraped_performer(name="Bonnie Alex", urls=[url_b]),
            "purl:" + url_b: scraped_performer(name="Bonnie Alex", urls=[url_a]),
        })
        config = perf_config([], performerMaxUrlDepth=5)
        one = runner(client, fd_repo, config)
        summary = one.run_full(one.run_fast(42)["run_id"])
        # Finishes at all - a real cycle would hang or grow without bound otherwise.
        assert summary["status"] in (R.READY_FOR_REVIEW, R.READY_WITH_ERRORS)
        urls = fd_repo.urls_of(summary["run_id"])
        keys = {u["norm_key"] for u in urls}
        assert len(keys) == 2   # both URLs recorded once each, not endlessly re-added

    def test_max_depth_is_respected(self, fd_repo):
        chain = ["https://perfa.com/%d" % i for i in range(6)]
        responses = {}
        client = FakeStash(performer=dict(PERFORMER, urls=[chain[0]]))
        for i in range(len(chain) - 1):
            responses["purl:" + chain[i]] = scraped_performer(
                name="Bonnie Alex", urls=[chain[i + 1]])
        client.responses = responses
        config = perf_config([], performerMaxUrlDepth=2)
        one = runner(client, fd_repo, config)
        summary = one.run_full(one.run_fast(42)["run_id"])
        urls = fd_repo.urls_of(summary["run_id"])
        assert max(u["depth"] for u in urls) <= 2 + 1
        assert any(u["state"] == R.U_SKIPPED_DEPTH for u in urls)

    def test_fast_completes_depth_zero_but_defers_deeper_urls_to_full(self, fd_repo):
        # Depth 0 is each candidate's own profile URL - for most non-stash-box
        # scrapers, where the actual images live, since the name search itself
        # usually returns just a name and this link (requirement: Fast still
        # completes what it found, it just does not chase further).
        url_a = "https://perfa.com/a"
        url_b = "https://perfa.com/b"
        client = FakeStash(performer=dict(PERFORMER, urls=[url_a]), responses={
            "purl:" + url_a: scraped_performer(name="Bonnie Alex", urls=[url_b]),
            "purl:" + url_b: scraped_performer(name="Bonnie Alex"),
        })
        config = perf_config([])
        summary = runner(client, fd_repo, config).run_fast(42)
        scraped = [call[1] for call in client.calls if call[0] == "scrape_performer_url"]
        assert scraped == [url_a]

        by_url = {u["url"]: u for u in fd_repo.urls_of(summary["run_id"])}
        assert by_url[url_a]["state"] == R.U_SCRAPED
        # Not U_SKIPPED_DEPTH either: still PENDING, so Full's own pass - not Fast's
        # clamp - is what actually decides whether it goes further.
        assert by_url[url_b]["state"] == R.U_PENDING

    def test_full_afterwards_picks_up_the_depth_fast_deferred(self, fd_repo):
        url_a = "https://perfa.com/a"
        url_b = "https://perfa.com/b"
        client = FakeStash(performer=dict(PERFORMER, urls=[url_a]), responses={
            "purl:" + url_a: scraped_performer(name="Bonnie Alex", urls=[url_b]),
            "purl:" + url_b: scraped_performer(name="Bonnie Alex X")}, boxes=[])
        config = perf_config([])
        one = runner(client, fd_repo, config)
        fast_summary = one.run_fast(42)
        one.run_full(fast_summary["run_id"])
        scraped = [call[1] for call in client.calls if call[0] == "scrape_performer_url"]
        assert scraped == [url_a, url_b]
        by_url = {u["url"]: u for u in fd_repo.urls_of(fast_summary["run_id"])}
        assert by_url[url_b]["state"] == R.U_SCRAPED


class TestSharedUrlProvenance:
    """Specification section 21 / tests C, D, E."""

    def test_two_scrapers_returning_the_same_url_is_one_candidate_two_sources(
            self, fd_repo):
        shared = "https://perfa.com/shared"
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex",
                                                              urls=[shared]),
            "pname:IAFD:Bonnie Alex": scraped_performer(name="Bonnie Alex",
                                                        urls=[shared]),
        })
        config = perf_config(["Babepedia", "IAFD"], recursiveUrlDiscovery=False)
        summary = runner(client, fd_repo, config).run_fast(42)

        from fastdiscovery import merge
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        url_row = [r for r in review["rows"] if r["field"] == "urls"][0]
        candidate = [v for v in url_row["values"] if v["raw"] == shared][0]
        assert len(candidate["sources"]) == 2   # both columns, one candidate


class TestImagesFollowRejection:
    """Specification section 12 / test F: rejecting a source drops its images too."""

    def test_rejecting_a_source_removes_its_images_restore_brings_them_back(
            self, fd_repo):
        images = ["https://perfa.com/img/%d.jpg" % i for i in range(20)]
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex",
                                                             images=images),
        })
        config = perf_config(["Babepedia"])
        summary = runner(client, fd_repo, config).run_fast(42)

        from fastdiscovery import merge
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        image_row = [r for r in review["rows"] if r["kind"] == "image"][0]
        assert len(image_row["values"]) == len(images)

        column_id = [c["id"] for c in review["columns"] if c["id"] != "current"][0]
        rejected_review = merge.build_performer(fd_repo, run, PERFORMER,
                                                rejected=[column_id])
        rejected_image_row = [r for r in rejected_review["rows"]
                              if r["kind"] == "image"]
        assert rejected_image_row == []   # every candidate came from the rejected source

        restored_review = merge.build_performer(fd_repo, run, PERFORMER, rejected=[])
        restored_row = [r for r in restored_review["rows"] if r["kind"] == "image"][0]
        assert len(restored_row["values"]) == len(images)
