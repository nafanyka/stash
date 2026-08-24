"""The operation layer the UI talks to.

`runPluginOperation` is the only channel between the page and the database, so these
tests care about two things: that a failure comes back as a message the page can show
rather than an exception, and that list operations stay cheap.
"""

from __future__ import annotations

from conftest import FakeClient, scraped
from scrapediscovery import engine as E, ops, settings as S
from scrapediscovery.db import repo as R
from test_engine import StubSchema


def context(repo, client=None, raw_config=None):
    return ops.Context(client or FakeClient(), repo, S.parse(raw_config or {}))


def scanned(repo, scene, scrapers, responses=None):
    client = FakeClient(scene=scene, scrapers=scrapers,
                        responses=responses or {"Example": scraped(
                            title="Example Scene", date="2024-01-20",
                            urls=["https://sitea.com/scene/9"])})
    engine = E.Engine(client, repo, S.parse({"excludedScrapers": ""}),
                      schema=StubSchema())
    summary = engine.scan(42)
    return client, summary


class TestDispatch:
    def test_an_unknown_operation_lists_the_real_ones(self, repo):
        result = ops.dispatch(context(repo), "nope", {})
        assert result["ok"] is False
        assert "ping" in result["operations"]

    def test_a_failing_operation_returns_a_message_not_an_exception(self, repo):
        # The page shows this text in place; an exception would surface as a bare
        # GraphQL error with nowhere useful to put it.
        result = ops.dispatch(context(repo), "image.get", {"sha256": "missing"})
        assert result["ok"] is False
        assert "missing"[:8] in result["error"]

    def test_ping_reports_the_versions_that_matter(self, repo):
        result = ops.dispatch(context(repo), "ping", {})
        assert result["ok"] is True
        assert result["schema_version"] == result["expected_schema_version"]
        assert result["database"].endswith(".sqlite")


class TestInbox:
    def test_tabs_map_to_status_groups(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers)
        page = ops.dispatch(context(repo), "inbox.list", {"tab": "results"})
        assert [row["scene_id"] for row in page["items"]] == [42]
        assert page["tabs"]["results"] == 1
        assert ops.dispatch(context(repo), "inbox.list", {"tab": "candidates"})["total"] == 0

    def test_the_all_tab_ignores_status(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers)
        assert ops.dispatch(context(repo), "inbox.list", {"tab": "all"})["total"] == 1

    def test_a_running_scan_is_reported_so_the_page_can_poll(self, repo):
        repo.start_scan(7, "manual", "normal", {}, {})
        repo.heartbeat(7 and repo.running_scans()[0]["id"], {"done": 2, "planned": 10})
        page = ops.dispatch(context(repo), "inbox.list", {"tab": "all"})
        assert page["running"][0]["progress"]["done"] == 2

    def test_a_dead_scan_is_swept_when_the_inbox_is_read(self, repo):
        scan = repo.start_scan(7, "manual", "normal", {}, {})
        repo.db.execute("UPDATE scans SET heartbeat_at = ? WHERE id = ?",
                        (R.ago(R.STALE_SCAN_SECONDS + 60), scan))
        repo.db.commit()
        ops.dispatch(context(repo), "inbox.list", {"tab": "all"})
        assert repo.scan(scan)["status"] == R.SCAN_CANCELLED


class TestSceneViews:
    def test_the_summary_is_small_enough_for_a_tab_badge(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers)
        summary = ops.dispatch(context(repo), "scene.summary", {"scene_id": 42})
        assert summary["status"] == R.RESULTS
        assert summary["result_count"] >= 1
        assert "results" not in summary  # no payload, just counts

    def test_an_unscanned_scene_answers_rather_than_failing(self, repo):
        summary = ops.dispatch(context(repo), "scene.summary", {"scene_id": 12345})
        assert summary["status"] == R.UNSCANNED
        assert summary["candidate_count"] == 0

    def test_the_detail_view_carries_no_raw_payloads(self, repo, scene, scrapers):
        client, _summary = scanned(repo, scene, scrapers)
        detail = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scene.detail", {"scene_id": 42})
        assert detail["results"]
        for result in detail["results"]:
            # Raw results are the biggest thing in the database; the page asks for one
            # at a time through result.raw instead.
            assert "raw" not in result
            assert "normalized" in result

    def test_the_detail_view_includes_the_discovery_graph(self, repo, scene, scrapers):
        client, _summary = scanned(repo, scene, scrapers)
        detail = ops.dispatch(ops.Context(client, repo, S.parse({})),
                              "scene.detail", {"scene_id": 42})
        assert detail["graph"]["nodes"]
        assert any(edge["from"] == "scene" for edge in detail["graph"]["edges"])

    def test_a_deleted_scene_does_not_break_its_history(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers)
        # The scene is gone from Stash but the discovery record is still worth reading.
        detail = ops.dispatch(context(repo, FakeClient(scene={"id": "999"})),
                              "scene.detail", {"scene_id": 42})
        assert detail["ok"] is True
        assert detail["scene"] is None
        assert detail["attempts"]

    def test_a_raw_result_can_be_fetched_on_request(self, repo, scene, scrapers):
        scanned(repo, scene, scrapers)
        result_id = repo.results_of_scene(42)[0]["id"]
        fetched = ops.dispatch(context(repo), "result.raw", {"result_id": result_id})
        assert fetched["result"]["raw"]["title"] == "Example Scene"


class TestImages:
    def test_a_stored_image_comes_back_as_a_data_uri(self, repo):
        repo.put_blob("abc", "image/png", b"\x89PNG")
        result = ops.dispatch(context(repo), "image.get", {"sha256": "abc"})
        assert result["data_uri"].startswith("data:image/png;base64,")


class TestScanControl:
    def test_starting_a_scan_queues_a_task_rather_than_doing_the_work(self, repo, scene,
                                                                     scrapers):
        # The operation blocks the browser's request, so a scan belongs in the job
        # queue where it gets a progress bar and a stop button.
        client = FakeClient(scene=scene, scrapers=scrapers)
        result = ops.dispatch(ops.Context(client, repo, S.parse({})), "scan.start",
                              {"scene_id": 42, "mode": "deep"})
        assert result["job_id"] == "job-1"
        queued = [one for one in client.calls if one[0] == "run_plugin_task"][0]
        assert queued[1] == ops.TASK_DISCOVER_SCENES
        assert queued[2]["mode"] == "deep"
        assert repo.scene_state(42)["status"] == R.SCANNING

    def test_a_scan_needs_a_scene(self, repo):
        assert ops.dispatch(context(repo), "scan.start", {})["ok"] is False

    def test_an_unknown_mode_is_refused(self, repo, scene, scrapers):
        result = ops.dispatch(context(repo, FakeClient(scene=scene, scrapers=scrapers)),
                              "scan.start", {"scene_id": 42, "mode": "sideways"})
        assert result["ok"] is False

    def test_several_scenes_can_be_queued_at_once(self, repo, scene, scrapers):
        client = FakeClient(scene=scene, scrapers=scrapers)
        result = ops.dispatch(ops.Context(client, repo, S.parse({})), "scan.start",
                              {"scene_ids": [1, 2, 3]})
        assert result["scene_ids"] == ["1", "2", "3"]


class TestSettingsOps:
    def test_the_spec_is_reported_with_current_values(self, repo):
        result = ops.dispatch(context(repo), "settings.get", {})
        keys = {entry["key"] for entry in result["settings"]}
        assert "maxConcurrency" in keys
        entry = [one for one in result["settings"] if one["key"] == "maxConcurrency"][0]
        assert entry["type"] == "NUMBER"
        assert entry["value"] == 3
        assert entry["description"]

    def test_saving_writes_through_to_stash(self, repo):
        saved = {}

        class Client(FakeClient):
            def save_plugin_settings(self, plugin_id, values):
                saved.update(values)
                return values

        result = ops.dispatch(context(repo, Client()), "settings.set",
                              {"values": {"maxDepth": 4, "debugLogging": True,
                                          "scoreWeights": {"duration": 5}}})
        assert result["ok"] is True
        assert saved["maxDepth"] == 4
        assert saved["debugLogging"] is True
        # JSON settings are stored as JSON text, the way Stash's own UI writes them.
        assert saved["scoreWeights"] == '{"duration": 5}'

    def test_unknown_keys_are_rejected_not_written(self, repo):
        result = ops.dispatch(context(repo), "settings.set",
                              {"values": {"nonsense": 1}})
        assert result["rejected"] == ["nonsense"]
        assert result["saved"] == []

    def test_a_non_object_payload_is_refused(self, repo):
        assert ops.dispatch(context(repo), "settings.set",
                            {"values": "not an object"})["ok"] is False


class TestDiagnostics:
    def test_diagnostics_reports_counts_and_scraper_statistics(self, repo, scene,
                                                              scrapers):
        scanned(repo, scene, scrapers)
        info = ops.dispatch(context(repo), "diagnostics.info", {})
        assert info["counts"]["attempts"] > 0
        assert info["database_bytes"] > 0
        assert info["versions"]["norm"] >= 1
        assert info["scraper_stats"]
