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


class TestFastThenFull:
    """Specification section 26 / test G: Full never repeats a Fast scraper."""

    def test_full_only_runs_scrapers_fast_did_not_use(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
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
    """Specification section 4 / tests H & I: cycles stop, maxDepth is respected."""

    def test_a_cycle_terminates(self, fd_repo):
        url_a = "https://pshared.com/a"
        url_b = "https://perfa.com/b"
        client = FakeStash(performer=dict(PERFORMER, urls=[url_a]), responses={
            "purl:" + url_a: scraped_performer(name="Bonnie Alex", urls=[url_b]),
            "purl:" + url_b: scraped_performer(name="Bonnie Alex", urls=[url_a]),
        })
        config = perf_config([], performerMaxUrlDepth=5)
        summary = runner(client, fd_repo, config).run_fast(42)
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
        summary = runner(client, fd_repo, config).run_fast(42)
        urls = fd_repo.urls_of(summary["run_id"])
        assert max(u["depth"] for u in urls) <= 2 + 1
        assert any(u["state"] == R.U_SKIPPED_DEPTH for u in urls)


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
