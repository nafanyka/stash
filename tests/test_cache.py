"""Attempt caching: TTLs, invalidation, and how an error is remembered.

The error classification matters more than it looks. A single scene thrown at every
installed fragment scraper produced 101 errors out of 191 attempts on a live library,
and nearly all of them were scrapers failing at a scene they have nothing to do with.
Cached for a day, as a transient error would be, they would be re-attempted on every
scan forever.
"""

from __future__ import annotations

from scrapediscovery import cache, settings as S
from scrapediscovery.db import repo as R


class TestErrorClassification:
    def test_the_failures_a_real_scan_produces_are_permanent(self):
        for message in (
            'scraper YouPorn: failed to load URL "https://www.youporn.com/watch//":'
            " http error 404:Not Found",
            "scraper xEmpire: scraper script error: exit status 69",
            "Internal system error. Error <runtime error: invalid memory address or"
            " nil pointer dereference>",
            "cannot use scraper Foo to scrape by name",
            "http error 403:Forbidden",
        ):
            assert cache.classify_error(message) == cache.PERMANENT, message

    def test_momentary_failures_are_transient(self):
        for message in ("timed out after 30s", "connection reset by peer",
                        "http error 503:Service Unavailable", "http error 429",
                        "dial tcp: lookup x.com: no such host", "unexpected EOF",
                        "context canceled"):
            assert cache.classify_error(message) == cache.TRANSIENT, message

    def test_an_unrecognised_error_is_assumed_transient(self):
        # Misclassifying costs one retry; the other way costs a permanently missed
        # match, so the unknown case errs towards trying again.
        assert cache.classify_error("something nobody has seen before") == cache.TRANSIENT
        assert cache.classify_error("") == cache.TRANSIENT
        assert cache.classify_error(None) == cache.TRANSIENT

    def test_a_transient_marker_wins_over_a_permanent_one(self):
        # A timeout while fetching a 404 page is still a timeout.
        assert cache.classify_error("http error 404 after timeout") == cache.TRANSIENT


class TestPolicy:
    def test_ttls_come_from_the_configuration(self):
        policy = cache.Policy(S.parse({}))
        assert policy.days_for("MATCH") == 90
        assert policy.days_for("NO_MATCH") == 30
        assert policy.days_for("TIMEOUT") == 3
        assert policy.days_for("ERROR", cache.TRANSIENT) == 1

    def test_a_permanent_error_is_remembered_as_long_as_a_no_match(self):
        policy = cache.Policy(S.parse({}))
        assert policy.days_for("ERROR", cache.PERMANENT) == policy.days_for("NO_MATCH")

    def test_an_unknown_status_is_never_reused(self):
        assert cache.Policy(S.parse({})).days_for("RUNNING") == 0
        assert cache.Policy(S.parse({})).days_for("SKIPPED") == 0

    def test_a_per_scraper_override_applies(self):
        config = S.parse({"scraperOverrides": {"S": {"cacheDays": 5}}})
        assert cache.Policy(config, "S").days_for("MATCH") == 5
        assert cache.Policy(config, "Other").days_for("MATCH") == 90


def _finished(repo, scene_id, key, status, when, fingerprint="fp", error_kind=None,
              results=0):
    """An attempt that finished at a chosen time, for testing expiry."""
    scan_id = repo.start_scan(scene_id, "manual", "normal", {}, {})
    attempt_id = repo.begin_attempt(scan_id, scene_id, "FRAGMENT_SCENE", "", key,
                                    scraper={"id": "S", "name": "S",
                                             "fingerprint": fingerprint})
    repo.finish_attempt(attempt_id, status, 100, results, error_kind=error_kind)
    repo.db.execute("UPDATE attempts SET finished_at = ? WHERE id = ?", (when, attempt_id))
    repo.db.commit()
    return attempt_id


class TestLookup:
    def test_a_fresh_attempt_is_reused(self, repo):
        _finished(repo, 1, "K", "NO_MATCH", R.now())
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable()) is not None

    def test_an_expired_attempt_is_not(self, repo):
        _finished(repo, 1, "K", "NO_MATCH", R.ago(31 * 86400))
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable()) is None

    def test_a_permanent_error_survives_past_the_error_ttl(self, repo):
        # Two days old: beyond ttlErrorDays (1), inside ttlNoMatchDays (30).
        _finished(repo, 1, "K", "ERROR", R.ago(2 * 86400), error_kind=cache.PERMANENT)
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable()) is not None

    def test_a_transient_error_does_not(self, repo):
        _finished(repo, 1, "K", "ERROR", R.ago(2 * 86400), error_kind=cache.TRANSIENT)
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable()) is None

    def test_a_changed_scraper_invalidates_its_cache(self, repo):
        # This is also what makes "scan with newly installed scrapers" work: an updated
        # scraper is one that has not been tried in its current form.
        _finished(repo, 1, "K", "NO_MATCH", R.now(), fingerprint="old")
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "new", policy.as_callable()) is None
        assert repo.find_cached_attempt(1, "K", "old", policy.as_callable()) is not None

    def test_the_cache_is_per_scene(self, repo):
        _finished(repo, 1, "K", "MATCH", R.now())
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(2, "K", "fp", policy.as_callable()) is None

    def test_the_most_recent_answer_wins(self, repo):
        _finished(repo, 1, "K", "NO_MATCH", R.ago(3600))
        _finished(repo, 1, "K", "MATCH", R.now())
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable())["status"] == "MATCH"

    def test_an_unfinished_attempt_is_never_a_cache_hit(self, repo):
        scan_id = repo.start_scan(1, "manual", "normal", {}, {})
        repo.begin_attempt(scan_id, 1, "FRAGMENT_SCENE", "", "K",
                           scraper={"id": "S", "fingerprint": "fp"})
        policy = cache.Policy(S.parse({}), "S")
        assert repo.find_cached_attempt(1, "K", "fp", policy.as_callable()) is None


class TestClearing:
    def test_only_empty_expired_attempts_are_cleared(self, repo):
        _finished(repo, 1, "empty", "NO_MATCH", R.ago(40 * 86400))
        kept = _finished(repo, 1, "held", "MATCH", R.ago(400 * 86400), results=1)
        with_results = _finished(repo, 1, "odd", "ERROR", R.ago(400 * 86400),
                                 error_kind=cache.PERMANENT, results=2)

        removed = repo.clear_expired_cache(cache.Policy(S.parse({})).as_callable())
        assert removed == 1
        surviving = {row["id"] for row in repo.attempts_of_scene(1)}
        # A MATCH is never touched by cache clearing, and neither is anything that
        # owns results - those are what candidates get rebuilt from.
        assert kept in surviving
        assert with_results in surviving

    def test_a_permanent_error_is_not_cleared_on_the_error_ttl(self, repo):
        _finished(repo, 1, "perm", "ERROR", R.ago(3 * 86400), error_kind=cache.PERMANENT)
        assert repo.clear_expired_cache(cache.Policy(S.parse({})).as_callable()) == 0

    def test_a_transient_error_is(self, repo):
        _finished(repo, 1, "temp", "ERROR", R.ago(3 * 86400), error_kind=cache.TRANSIENT)
        assert repo.clear_expired_cache(cache.Policy(S.parse({})).as_callable()) == 1

    def test_an_error_with_no_recorded_kind_is_treated_as_transient(self, repo):
        # Rows written before classification existed still have to behave sanely.
        _finished(repo, 1, "old", "ERROR", R.ago(3 * 86400), error_kind=None)
        assert repo.clear_expired_cache(cache.Policy(S.parse({})).as_callable()) == 1
