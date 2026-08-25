"""The per-scraper statistics table, and uninstalling a scraper from it.

Uninstalling deletes files on the Stash host, so most of this is about the ways it must
refuse. The error-signature grouping is the other half: the numbers say a scraper is
failing, the signature says whether it is irrelevant, broken, or just slow.
"""

from __future__ import annotations

from conftest import FakeClient, scraped
from scrapediscovery import cache, engine as E, ops, settings as S
from scrapediscovery.db import repo as R
from test_engine import StubSchema


class PackageClient(FakeClient):
    """A Stash that knows about installed packages and records uninstalls."""

    def __init__(self, packages=None, **kwargs):
        FakeClient.__init__(self, **kwargs)
        self.packages = packages if packages is not None else [
            {"package_id": "SiteA", "name": "Site A", "version": "1",
             "sourceURL": "https://example.com/index.yml"},
            {"package_id": "Searcher", "name": "Searcher", "version": "2",
             "sourceURL": "https://example.com/index.yml"},
        ]
        self.uninstalled = []
        self.reloaded = 0

    def installed_scraper_packages(self):
        return self.packages

    def uninstall_scraper_package(self, package_id, source_url):
        self.uninstalled.append((package_id, source_url))
        return "job-uninstall-1"

    def reload_scrapers(self):
        self.reloaded += 1
        return True


def scanned(repo, scene, scrapers, responses):
    client = FakeClient(scene=scene, scrapers=scrapers, responses=responses)
    E.Engine(client, repo, S.parse({"excludedScrapers": ""}),
             schema=StubSchema()).scan(42)
    return client


def context(repo, client=None):
    return ops.Context(client or PackageClient(), repo, S.parse({}))


class TestErrorSignature:
    def test_the_same_fault_at_different_sites_is_one_signature(self):
        # Verbatim from one real run: three scrapers, one problem.
        messages = [
            'scraper YouPorn: failed to load URL "https://www.youporn.com/watch//":'
            " http error 404:Not Found",
            'scraper Xvideos: failed to load URL "https://www.xvideos.com/video./x":'
            " http error 404:Not Found",
            'scraper xhamster: failed to load URL "https://xhamster.com/videos/":'
            " http error 404:Not Found",
        ]
        signatures = {cache.error_signature(one) for one in messages}
        assert len(signatures) == 1
        assert "404" in signatures.pop().replace("#", "404")

    def test_different_faults_stay_apart(self):
        assert cache.error_signature("scraper A: scraper script error: exit status 69") \
            != cache.error_signature('scraper A: failed to load URL "x": http error 404')

    def test_the_scraper_name_is_not_part_of_the_fault(self):
        assert cache.error_signature("scraper Alpha: boom") == \
            cache.error_signature("scraper Beta: boom")

    def test_an_empty_message_still_has_a_signature(self):
        assert cache.error_signature("") == "(no message)"
        assert cache.error_signature(None) == "(no message)"

    def test_signatures_stay_readable(self):
        signature = cache.error_signature(
            'scraper X: failed to load URL "https://a.com/1": http error 500:Server Error')
        assert "failed to load url" in signature.lower()
        assert len(signature) <= 180


class TestStats:
    def test_every_kind_of_outcome_is_counted(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {
            "Example": scraped(title="A", date="2024-01-01"),
            "SiteA": [],
            "Searcher": RuntimeError("scraper script error: exit status 69"),
            "Filename": TimeoutError("timed out after 30s"),
        })
        answer = ops.dispatch(context(repo), "scrapers.stats", {})
        rows = {row["id"]: row for row in answer["scrapers"]}

        assert rows["Example"]["matches"] == 1
        assert rows["SiteA"]["no_matches"] == 1
        assert rows["Searcher"]["errors"] == 1
        assert rows["Filename"]["timeouts"] == 1
        assert answer["totals"]["attempts"] >= 4

    def test_untried_scrapers_can_be_included_or_hidden(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {"Example": scraped(title="A")})
        with_untried = ops.dispatch(context(repo), "scrapers.stats",
                                    {"includeUntried": True})
        without = ops.dispatch(context(repo), "scrapers.stats",
                               {"includeUntried": False})
        assert with_untried["total"] >= without["total"]
        assert all(row["attempts"] for row in without["scrapers"])

    def test_a_cache_hit_is_not_evidence_about_a_scraper(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers,
                            responses={"Example": scraped(title="A")})
        engine = E.Engine(client, repo, S.parse({"excludedScrapers": ""}),
                          schema=StubSchema())
        engine.scan(42)
        engine.scan(42)  # entirely from cache

        rows = {row["id"]: row for row in
                ops.dispatch(context(repo), "scrapers.stats", {})["scrapers"]}
        # Example is asked twice in one scan - once for the URL already on the scene,
        # once as a fragment - and the second scan adds nothing at all.
        assert rows["Example"]["attempts"] == 2

    def test_the_match_rate_and_waste_are_derived(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {"Searcher": scraped(title="A")})
        rows = {row["id"]: row for row in
                ops.dispatch(context(repo), "scrapers.stats", {})["scrapers"]}
        assert rows["Searcher"]["match_rate"] == 1.0
        assert rows["SiteA"]["match_rate"] == 0.0
        # Nothing found, so all of its time counts as wasted.
        assert rows["SiteA"]["waste"] >= 0

    def test_the_rate_counts_attempts_not_scrapers(self, repo, scene, scrapers):
        # Example is asked twice in one scan - for the scene's URL and as a fragment -
        # and only the fragment answers, so half of what it was asked came to nothing.
        scanned(repo, scene, scrapers, {"Example": scraped(title="A")})
        rows = {row["id"]: row for row in
                ops.dispatch(context(repo), "scrapers.stats", {})["scrapers"]}
        assert rows["Example"]["attempts"] == 2
        assert rows["Example"]["matches"] == 1
        assert rows["Example"]["match_rate"] == 0.5

    def test_the_typical_failure_is_reported_with_its_kind(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {
            "SiteA": RuntimeError(
                'scraper SiteA: failed to load URL "https://sitea.com/x":'
                " http error 404:Not Found"),
        })
        rows = {row["id"]: row for row in
                ops.dispatch(context(repo), "scrapers.stats", {})["scrapers"]}
        top = rows["SiteA"]["top_error"]
        assert top["count"] == 1
        assert top["kind"] == cache.PERMANENT
        assert "404" in top["signature"].replace("#", "404")
        # The verbatim message is kept for the tooltip, alongside the shape.
        assert "sitea.com" in top["example"]
        assert rows["SiteA"]["last_error"]["message"]

    def test_sorting_is_whitelisted(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {"Example": scraped(title="A")})
        for sort in ("attempts", "matches", "waste", "name"):
            answer = ops.dispatch(context(repo), "scrapers.stats", {"sort": sort})
            assert answer["ok"] is True
        injected = ops.dispatch(context(repo), "scrapers.stats",
                                {"sort": "id; DROP TABLE scrapers"})
        assert injected["ok"] is True
        assert injected["total"] > 0

    def test_rows_with_no_value_for_the_sorted_column_go_last(self, repo, scene,
                                                             scrapers):
        # A scraper that has never run has no average time, which is not the same as
        # being instantaneous.
        scanned(repo, scene, scrapers, {"Example": scraped(title="A")})
        rows = ops.dispatch(context(repo), "scrapers.stats",
                            {"sort": "avg_ms", "direction": "asc",
                             "includeUntried": True})["scrapers"]
        seen_none = False
        for row in rows:
            if row["avg_ms"] is None:
                seen_none = True
            elif seen_none:
                raise AssertionError("a scraper with a time sorted after one without")

    def test_uninstallability_is_reported_per_row(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {"Example": scraped(title="A")})
        rows = {row["id"]: row for row in
                ops.dispatch(context(repo), "scrapers.stats", {})["scrapers"]}
        assert rows["SiteA"]["uninstallable"] is True
        # No package: Stash can only remove what it installed.
        assert rows["Example"]["uninstallable"] is False
        assert rows["Example"]["uninstall_blocked"]


class TestUninstall:
    def _ready(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers, {"SiteA": []})
        return PackageClient(scene=scene, scrapers=scrapers)

    def test_it_refuses_without_a_matching_confirmation(self, repo, scene, scrapers):
        client = self._ready(repo, scene, scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scrapers.uninstall", {"scraper_id": "SiteA"})
        assert answer["ok"] is False
        assert client.uninstalled == []

        wrong = ops.dispatch(ops.Context(client, repo, S.parse({})),
                             "scrapers.uninstall",
                             {"scraper_id": "SiteA", "confirm": "yes"})
        assert wrong["ok"] is False
        assert client.uninstalled == []

    def test_a_matching_confirmation_removes_the_package(self, repo, scene, scrapers):
        client = self._ready(repo, scene, scrapers)
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scrapers.uninstall",
                              {"scraper_id": "SiteA", "confirm": "SiteA"})
        assert answer["ok"] is True
        assert answer["job_id"] == "job-uninstall-1"
        assert client.uninstalled == [("SiteA", "https://example.com/index.yml")]

    def test_history_survives_an_uninstall(self, repo, scene, scrapers):
        client = self._ready(repo, scene, scrapers)
        before = len(repo.attempts_of_scene(42))
        ops.dispatch(ops.Context(client, repo, S.parse({})), "scrapers.uninstall",
                     {"scraper_id": "SiteA", "confirm": "SiteA"})
        # The record of what was tried is the point of keeping a database.
        assert len(repo.attempts_of_scene(42)) == before
        assert "SiteA" not in repo.known_scrapers()

    def test_it_refuses_to_uninstall_its_own_entry_point(self, repo, scene, scrapers):
        client = PackageClient(scene=scene, scrapers=scrapers, packages=[
            {"package_id": "ScrapeDiscovery", "name": "ScrapeDiscovery", "version": "1",
             "sourceURL": "https://example.com/index.yml"}])
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scrapers.uninstall",
                              {"scraper_id": "ScrapeDiscovery",
                               "confirm": "ScrapeDiscovery"})
        assert answer["ok"] is False
        assert client.uninstalled == []

    def test_it_refuses_what_stash_did_not_install(self, repo, scene, scrapers):
        client = PackageClient(scene=scene, scrapers=scrapers, packages=[])
        answer = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scrapers.uninstall",
                              {"scraper_id": "SiteA", "confirm": "SiteA"})
        assert answer["ok"] is False
        assert "source" in answer["error"]
        assert client.uninstalled == []

    def test_a_scraper_id_is_required(self, repo):
        assert ops.dispatch(context(repo), "scrapers.uninstall", {})["ok"] is False

    def test_reloading_is_a_separate_step(self, repo, scene, scrapers):
        client = self._ready(repo, scene, scrapers)
        ops.dispatch(ops.Context(client, repo, S.parse({})), "scrapers.uninstall",
                     {"scraper_id": "SiteA", "confirm": "SiteA"})
        # Stash removes the files in a job, so the scraper stays loaded until asked.
        assert client.reloaded == 0
        ops.dispatch(ops.Context(client, repo, S.parse({})), "scrapers.reload", {})
        assert client.reloaded == 1


class TestOrchestratorExclusion:
    def test_scrapeall_is_excluded_by_default(self):
        # It probes every source itself, so invoking it from a scan multiplies the work
        # by a couple of hundred. Observed live: slowest attempt of the run, timed out.
        assert S.parse({}).is_enabled("ScrapeAll", "ScrapeAll") is False

    def test_but_it_is_only_a_default(self, ):
        # Unlike the recursion guard, this is a judgement call the user may overrule.
        assert S.parse({"excludedScrapers": ""}).is_enabled("ScrapeAll") is True
