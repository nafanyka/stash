"""The persistence layer: migrations, deduplication, the inbox index, retention.

The inbox queries are the ones with a performance contract attached - a library can be
tens of thousands of scenes, so filtering, sorting and paging all have to happen in
SQL and never open a raw result.
"""

from __future__ import annotations

import sqlite3

from scrapediscovery.db import migrations, repo as R


class TestMigrations:
    def test_a_fresh_database_is_at_the_current_version(self, repo):
        assert repo.schema_version() == migrations.SCHEMA_VERSION

    def test_migrating_twice_is_a_no_op(self, repo):
        before, after = migrations.migrate(repo.db)
        assert (before, after) == (migrations.SCHEMA_VERSION, migrations.SCHEMA_VERSION)

    def test_a_newer_database_is_refused_rather_than_broken(self, repo):
        repo.db.execute("PRAGMA user_version = 999")
        try:
            migrations.migrate(repo.db)
        except RuntimeError as exc:
            assert "newer" in str(exc)
        else:
            raise AssertionError("a future schema should not be silently accepted")

    def test_foreign_keys_are_enforced(self, repo):
        try:
            repo.db.execute(
                "INSERT INTO results(attempt_id, ordinal, raw_json, raw_fingerprint)"
                " VALUES(9999, 0, '{}', 'x')")
            repo.db.commit()
        except sqlite3.IntegrityError:
            return
        raise AssertionError("a result pointing at no attempt should not be storable")

    def test_deleting_a_scan_takes_its_attempts_and_results(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        attempt = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K")
        repo.add_result(attempt, 0, {"a": 1}, "fp")
        repo._write("DELETE FROM scans WHERE id = ?", (scan,))
        assert repo.counts()["attempts"] == 0
        assert repo.counts()["results"] == 0


class TestScraperRegistry:
    def test_new_and_changed_scrapers_are_reported(self, repo):
        first = repo.sync_scrapers([{"id": "A", "name": "A", "kinds": ["URL"],
                                     "url_patterns": ["a.com"], "fingerprint": "f1"}])
        assert first == {"total": 1, "added": ["A"], "changed": []}

        second = repo.sync_scrapers([
            {"id": "A", "name": "A", "kinds": ["URL"], "url_patterns": ["a.com"],
             "fingerprint": "f2"},
            {"id": "B", "name": "B", "kinds": ["NAME"], "url_patterns": [],
             "fingerprint": "g1"},
        ])
        assert second["added"] == ["B"]
        assert second["changed"] == ["A"]

    def test_first_seen_survives_a_change(self, repo):
        repo.sync_scrapers([{"id": "A", "fingerprint": "f1"}])
        original = repo.known_scrapers()["A"]["first_seen"]
        repo.sync_scrapers([{"id": "A", "fingerprint": "f2"}])
        assert repo.known_scrapers()["A"]["first_seen"] == original

    def test_what_a_scene_has_been_tried_with(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        for scraper_id, fingerprint in (("A", "f1"), ("B", "g1")):
            attempt = repo.begin_attempt(
                scan, 1, "FRAGMENT_SCENE", "", "K" + scraper_id,
                scraper={"id": scraper_id, "name": scraper_id, "fingerprint": fingerprint})
            repo.finish_attempt(attempt, "NO_MATCH", 10)
        assert repo.scrapers_tried(1) == {"A": "f1", "B": "g1"}
        assert repo.scrapers_tried(2) == {}


class TestDeduplication:
    def test_a_url_cannot_enter_one_scan_twice(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        first = repo.add_url(scan, 1, "https://x.com/a", "https://x.com/a", "x.com",
                             "x.com/a", 0, ["A"])
        again = repo.add_url(scan, 1, "https://x.com/a?utm_source=y", "https://x.com/a",
                            "x.com", "x.com/a", 1, ["A"])
        assert first is not None
        assert again is None  # the loop guard

    def test_the_same_url_may_appear_in_a_later_scan(self, repo):
        one = repo.start_scan(1, "manual", "normal", {}, {})
        two = repo.start_scan(1, "manual", "normal", {}, {})
        assert repo.add_url(one, 1, "u", "u", "h", "k", 0, []) is not None
        assert repo.add_url(two, 1, "u", "u", "h", "k", 0, []) is not None

    def test_an_image_is_stored_once_however_many_scrapers_return_it(self, repo):
        for _ in range(4):
            repo.put_blob("hash", "image/jpeg", b"bytes")
        assert repo.counts()["blobs"] == 1
        assert repo.blob("hash")["data"] == b"bytes"

    def test_an_identical_result_keeps_its_own_fingerprint(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        one = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K1")
        two = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K2")
        repo.add_result(one, 0, {"title": "x"}, "same-fp")
        repo.add_result(two, 0, {"title": "x"}, "same-fp")
        rows = repo.results_of_scene(1)
        # Two sources agreeing is the signal; the fingerprint is what recognises it.
        assert len(rows) == 2
        assert len({row["raw_fingerprint"] for row in rows}) == 1


class TestCachedAttempts:
    def test_a_cached_copy_points_at_the_attempt_that_ran(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        original = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K",
                                      scraper={"id": "S", "name": "S", "fingerprint": "f"})
        repo.finish_attempt(original, "MATCH", 50, 1)
        repo.add_result(original, 0, {"title": "x"}, "fp")

        later = repo.start_scan(1, "retry", "normal", {}, {})
        source = repo.attempts_of_scan(scan)[0]
        copy = repo.record_cached_attempt(later, 1, source)

        row = [one for one in repo.attempts_of_scan(later) if one["id"] == copy][0]
        assert row["from_cache"] == 1
        assert row["cached_from"] == original
        # The payload is not duplicated; the reference finds it.
        assert len(repo.results_via(row)) == 1
        assert repo.counts()["results"] == 1

    def test_a_cache_hit_on_a_cache_hit_does_not_chain(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        original = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K")
        repo.finish_attempt(original, "MATCH", 50, 1)
        repo.add_result(original, 0, {"a": 1}, "fp")

        second = repo.start_scan(1, "retry", "normal", {}, {})
        first_copy = repo.record_cached_attempt(second, 1, repo.attempts_of_scan(scan)[0])
        third = repo.start_scan(1, "retry", "normal", {}, {})
        source = [one for one in repo.attempts_of_scan(second) if one["id"] == first_copy][0]
        second_copy = repo.record_cached_attempt(third, 1, source)

        row = [one for one in repo.attempts_of_scan(third) if one["id"] == second_copy][0]
        assert row["cached_from"] == original
        assert len(repo.results_via(row)) == 1


class TestSceneState:
    def _scan(self, repo, scene_id, statuses, candidates=0, scan_status="COMPLETED"):
        scan = repo.start_scan(scene_id, "manual", "normal", {}, {})
        for index, status in enumerate(statuses):
            attempt = repo.begin_attempt(scan, scene_id, "FRAGMENT_SCENE", "",
                                         "K%d" % index)
            repo.finish_attempt(attempt, status, 10, 1 if status == "MATCH" else 0)
            if status == "MATCH":
                repo.add_result(attempt, 0, {"title": "t"}, "fp%d" % index)
        for index in range(candidates):
            repo.add_candidate(scene_id, scan, "id%d" % index, 50 + index, "possible",
                               {}, {}, 1, 1, 1, 1)
        repo.finish_scan(scan, scan_status)
        return repo.refresh_scene_state(scene_id)

    def test_never_scanned(self, repo):
        assert repo.refresh_scene_state(1)["status"] == R.UNSCANNED

    def test_matches_without_candidates_read_as_results_not_nothing(self, repo):
        # Otherwise a scene with five matches would say "nothing found" until the
        # candidate stage runs, which is exactly backwards.
        state = self._scan(repo, 1, ["MATCH", "NO_MATCH"])
        assert state["status"] == R.RESULTS

    def test_candidates_win(self, repo):
        assert self._scan(repo, 1, ["MATCH"], candidates=2)["status"] == R.CANDIDATES

    def test_nothing_found(self, repo):
        assert self._scan(repo, 1, ["NO_MATCH", "NO_MATCH"])["status"] == R.NO_RESULTS

    def test_everything_failing_is_not_the_same_as_nothing_found(self, repo):
        assert self._scan(repo, 1, ["ERROR", "TIMEOUT"])["status"] == R.FAILED

    def test_a_running_scan_shows_as_scanning(self, repo):
        repo.start_scan(1, "manual", "normal", {}, {})
        assert repo.refresh_scene_state(1)["status"] == R.SCANNING

    def test_applying_metadata_wins_over_everything(self, repo):
        self._scan(repo, 1, ["MATCH"], candidates=1)
        repo.record_application(1, None, "manual", {}, {}, {}, {})
        assert repo.refresh_scene_state(1)["status"] == R.APPLIED

    def test_counters_are_recomputed_from_what_is_stored(self, repo):
        state = self._scan(repo, 1, ["MATCH", "ERROR", "NO_MATCH"], candidates=2)
        assert state["attempt_count"] == 3
        assert state["error_count"] == 1
        assert state["candidate_count"] == 2
        assert state["best_confidence"] == 51

    def test_scene_details_are_carried_for_the_inbox(self, repo):
        repo.refresh_scene_state(1, {"display_title": "T", "path": "/p.mp4",
                                     "studio": {"name": "S"}})
        row = repo.scene_state(1)
        assert (row["title"], row["path"], row["studio_name"]) == ("T", "/p.mp4", "S")


class TestStaleScans:
    def test_a_scan_whose_process_died_is_swept(self, repo):
        # `stopJob` kills the process, so a scan can never write its own final status.
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        repo.db.execute("UPDATE scans SET heartbeat_at = ? WHERE id = ?",
                        (R.ago(R.STALE_SCAN_SECONDS + 60), scan))
        repo.db.commit()
        assert repo.sweep_stale_scans() == 1
        assert repo.scan(scan)["status"] == R.SCAN_CANCELLED
        assert repo.scene_state(1)["status"] != R.SCANNING

    def test_a_scan_that_is_still_reporting_is_left_alone(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        repo.heartbeat(scan, {"done": 3})
        assert repo.sweep_stale_scans() == 0
        assert repo.scan(scan)["status"] == R.SCAN_RUNNING

    def test_completed_attempts_survive_the_sweep(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        attempt = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K")
        repo.finish_attempt(attempt, "MATCH", 10, 1)
        repo.add_result(attempt, 0, {"a": 1}, "fp")
        repo.db.execute("UPDATE scans SET heartbeat_at = ? WHERE id = ?",
                        (R.ago(R.STALE_SCAN_SECONDS + 60), scan))
        repo.db.commit()
        repo.sweep_stale_scans()
        assert len(repo.results_of_scene(1)) == 1


class TestInbox:
    def _populate(self, repo):
        rows = [
            (1, R.CANDIDATES, 3, 92.0, "Alpha", "Studio One", 0),
            (2, R.CANDIDATES, 1, 64.0, "Beta", "Studio Two", 2),
            (3, R.NO_RESULTS, 0, None, "Gamma", "Studio One", 0),
            (4, R.FAILED, 0, None, "Delta", "", 5),
            (5, R.APPLIED, 2, 97.0, "Epsilon", "Studio Two", 0),
        ]
        for scene_id, status, candidates, best, title, studio, errors in rows:
            repo.set_scene_status(
                scene_id, status, candidate_count=candidates, best_confidence=best,
                title=title, studio_name=studio, error_count=errors,
                path="/media/%s.mp4" % title.lower(),
                last_scanned_at=R.ago(scene_id * 3600))

    def test_filtering_by_status(self, repo):
        self._populate(repo)
        page = repo.inbox(status=R.CANDIDATES)
        assert page["total"] == 2
        assert {row["scene_id"] for row in page["items"]} == {1, 2}

    def test_several_statuses_at_once(self, repo):
        self._populate(repo)
        assert repo.inbox(status="FAILED,NO_RESULTS")["total"] == 2

    def test_searching_matches_title_path_or_id(self, repo):
        self._populate(repo)
        assert repo.inbox(query="Alph")["total"] == 1
        assert repo.inbox(query="delta.mp4")["total"] == 1
        assert repo.inbox(query="3")["total"] == 1

    def test_filtering_by_confidence_and_studio_and_errors(self, repo):
        self._populate(repo)
        assert repo.inbox(min_confidence=90)["total"] == 2
        assert repo.inbox(studio="Studio One")["total"] == 2
        assert repo.inbox(has_errors=True)["total"] == 2
        assert repo.inbox(has_errors=False)["total"] == 3

    def test_sorting_uses_a_whitelist_not_the_callers_string(self, repo):
        self._populate(repo)
        best = repo.inbox(sort="confidence", direction="desc")["items"]
        assert [row["scene_id"] for row in best][:2] == [5, 1]

        # An unknown sort falls back rather than reaching SQL.
        injected = repo.inbox(sort="scene_id; DROP TABLE scans")
        assert injected["total"] == 5
        assert repo.counts()["scene_state"] == 5

    def test_paging(self, repo):
        self._populate(repo)
        first = repo.inbox(sort="scene_id", direction="asc", page=1, per_page=2)
        second = repo.inbox(sort="scene_id", direction="asc", page=2, per_page=2)
        assert [row["scene_id"] for row in first["items"]] == [1, 2]
        assert [row["scene_id"] for row in second["items"]] == [3, 4]
        assert first["total"] == 5

    def test_per_page_is_capped(self, repo):
        self._populate(repo)
        assert repo.inbox(per_page=100000)["per_page"] == 200

    def test_filtering_by_scraper_uses_the_attempt_history(self, repo):
        self._populate(repo)
        scan = repo.start_scan(3, "manual", "normal", {}, {})
        attempt = repo.begin_attempt(scan, 3, "FRAGMENT_SCENE", "", "K",
                                     scraper={"id": "OnlyHere", "name": "Only Here"})
        repo.finish_attempt(attempt, "NO_MATCH", 10)
        assert [row["scene_id"] for row in repo.inbox(scraper="OnlyHere")["items"]] == [3]
        assert repo.inbox(scraper="NeverUsed")["total"] == 0

    def test_status_counts_and_selection_by_status(self, repo):
        self._populate(repo)
        assert repo.status_counts()[R.CANDIDATES] == 2
        assert sorted(repo.scenes_by_status([R.NO_RESULTS, R.FAILED])) == [3, 4]


class TestRetention:
    def test_pruning_removes_old_scans_and_their_payloads(self, repo):
        old = repo.start_scan(1, "manual", "normal", {}, {})
        attempt = repo.begin_attempt(old, 1, "FRAGMENT_SCENE", "", "K")
        repo.finish_attempt(attempt, "MATCH", 10, 1)
        repo.add_result(attempt, 0, {"a": 1}, "fp")
        repo.finish_scan(old, "COMPLETED")
        repo.db.execute("UPDATE scans SET finished_at = ? WHERE id = ?",
                        (R.ago(400 * 86400), old))
        repo.db.commit()

        recent = repo.start_scan(1, "manual", "normal", {}, {})
        repo.finish_scan(recent, "COMPLETED")

        assert repo.prune_history(180)["scans"] == 1
        assert repo.counts()["results"] == 0
        assert repo.counts()["scans"] == 1

    def test_pruning_is_off_when_retention_is_zero(self, repo):
        repo.start_scan(1, "manual", "normal", {}, {})
        assert repo.prune_history(0)["scans"] == 0

    def test_unreferenced_images_are_collected(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        attempt = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K")
        repo.put_blob("kept", "image/jpeg", b"a")
        repo.put_blob("orphan", "image/jpeg", b"b")
        repo.add_result(attempt, 0, {"a": 1}, "fp", image_sha256="kept")
        assert repo.prune_orphan_blobs() == 1
        assert repo.blob("kept") is not None
        assert repo.blob("orphan") is None

    def test_vacuum_runs_and_leaves_the_database_usable(self, repo):
        repo.start_scan(1, "manual", "normal", {}, {})
        repo.vacuum()
        assert repo.counts()["scans"] == 1


class TestStatistics:
    def test_stats_are_derived_from_history_and_exclude_cache_hits(self, repo):
        scan = repo.start_scan(1, "manual", "normal", {}, {})
        for status in ("MATCH", "MATCH", "NO_MATCH", "ERROR", "TIMEOUT"):
            attempt = repo.begin_attempt(scan, 1, "FRAGMENT_SCENE", "", "K" + status,
                                         scraper={"id": "S", "name": "S"})
            repo.finish_attempt(attempt, status, 200)
        # A cache hit is not evidence about the scraper, so it must not skew the stats.
        source = repo.attempts_of_scan(scan)[0]
        repo.record_cached_attempt(scan, 1, source)

        stats = {row["scraper_id"]: row for row in repo.scraper_stats()}["S"]
        assert stats["attempts"] == 5
        assert stats["matches"] == 2
        assert stats["errors"] == 1
        assert stats["timeouts"] == 1
        assert stats["avg_ms"] == 200
