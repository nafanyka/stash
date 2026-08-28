"""The UI's API: what the page can ask for, and what it is protected from."""

from __future__ import annotations

from fd_common import FakeStash, scraped

from fastdiscovery import discovery, ops, settings
from fastdiscovery.db import repo as R

STASHDB = "https://stashdb.org/graphql"


def context(fd_repo, fd_config, scene, responses=None, entities=None):
    client = FakeStash(scene=scene, responses=responses or {}, entities=entities or {})
    return ops.Context(client, fd_repo, fd_config), client


def discovered(fd_repo, fd_config, scene, responses):
    ctx, client = context(fd_repo, fd_config, scene, responses)
    discovery.Runner(client, fd_repo, fd_config).run(295)
    return ctx, client


class TestDispatch:
    def test_an_unknown_operation_is_an_answer_not_a_crash(self, fd_repo, fd_config,
                                                            fd_scene):
        ctx, _client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "no.such.op", {})
        assert result["ok"] is False
        assert "operations" in result

    def test_an_operation_that_raises_answers_with_the_message(self, fd_repo, fd_config,
                                                               fd_scene):
        ctx, _client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "scene.status", {})
        assert result["ok"] is False
        assert "scene_id" in result["error"]


class TestStartingARun:
    def test_starting_queues_a_task_rather_than_scraping_here(self, fd_repo, fd_config,
                                                               fd_scene):
        ctx, client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "run.start", {"scene_id": "295"})
        assert result["job_id"] == "job-1"
        queued = [call for call in client.calls if call[0] == "run_plugin_task"]
        assert queued[0][1] == ops.TASK_DISCOVER
        assert queued[0][2]["scene_ids"] == "295"

    def test_a_run_waiting_for_review_needs_confirmation(self, fd_repo, fd_config,
                                                          fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        blocked = ops.dispatch(ctx, "run.start", {"scene_id": "295"})
        assert blocked["ok"] is False
        assert blocked["needs_confirmation"] is True

        allowed = ops.dispatch(ctx, "run.start", {"scene_id": "295", "replace": True})
        assert allowed["ok"] is True

    def test_several_scenes_go_into_one_job(self, fd_repo, fd_config, fd_scene):
        ctx, client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "run.start", {"scene_ids": [1, 2, 3]})
        assert result["queued"] == 3
        queued = [call for call in client.calls if call[0] == "run_plugin_task"][0]
        assert queued[2]["scene_ids"] == "1,2,3"


class TestReview:
    def test_the_review_carries_the_matrix_and_a_starting_selection(
            self, fd_repo, fd_config, fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One", performers=["New One"])})
        review = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        assert review["ok"] is True
        assert review["columns"][0]["id"] == "current"
        assert review["selection"] == review["default_selection"]
        assert review["summary"]["new_entities"] == 1

    def test_a_saved_selection_survives_a_reload(self, fd_repo, fd_config, fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        first = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        ops.dispatch(ctx, "review.save", {"run_id": first["run"]["id"],
                                          "selection": {"title": "v1"}})
        again = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        assert again["selection"] == {"title": "v1"}

    def test_a_decided_run_says_so_instead_of_showing_an_empty_table(
            self, fd_repo, fd_config, fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        run = fd_repo.latest_run(295)
        ops.dispatch(ctx, "run.reject", {"run_id": run["id"]})
        review = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        assert review["ok"] is False
        assert "deleted" in review["error"]

    def test_an_image_is_served_only_when_asked_for(self, fd_repo, fd_config, fd_scene):
        import base64
        uri = "data:image/png;base64," + base64.b64encode(b"png bytes").decode()
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(image=uri)})
        review = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        image_row = next(row for row in review["rows"] if row["field"] == "image")
        blob = next(entry for entry in image_row["values"] if entry["kind"] == "blob")
        # The matrix carries a reference, not the bytes.
        assert "data" not in blob
        served = ops.dispatch(ctx, "review.image", {"sha256": blob["sha256"]})
        assert served["data_uri"] == uri


class TestSceneStatus:
    def test_a_scene_with_no_run_says_so(self, fd_repo, fd_config, fd_scene):
        ctx, _client = context(fd_repo, fd_config, fd_scene)
        status = ops.dispatch(ctx, "scene.status", {"scene_id": "295"})
        assert status["run"] is None

    def test_a_finished_run_is_reported_with_its_counts(self, fd_repo, fd_config,
                                                        fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        status = ops.dispatch(ctx, "scene.status", {"scene_id": "295"})
        assert status["run"]["status"] == R.READY_FOR_REVIEW
        assert status["run"]["reviewable"] is True
        assert status["run"]["result_count"] == 1


class TestListing:
    def test_the_page_lists_runs_with_their_scenes(self, fd_repo, fd_config, fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        listing = ops.dispatch(ctx, "run.list", {"tab": "ready"})
        assert listing["total"] == 1
        assert listing["runs"][0]["scene"]["title"] == "Old title"

    def test_tabs_filter_by_status(self, fd_repo, fd_config, fd_scene):
        ctx, _client = discovered(fd_repo, fd_config, fd_scene, {})
        assert ops.dispatch(ctx, "run.list", {"tab": "ready"})["total"] == 0
        assert ops.dispatch(ctx, "run.list", {"tab": "empty"})["total"] == 1


class TestSettings:
    def test_settings_come_back_with_defaults_and_descriptions(self, fd_repo, fd_config,
                                                               fd_scene):
        ctx, _client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "settings.get", {})
        names = {entry["name"] for entry in result["spec"]}
        assert names == set(settings.SPEC)
        assert all(entry["description"] for entry in result["spec"])

    def test_the_detected_boxes_are_informational_only(self, fd_repo, fd_config,
                                                       fd_scene):
        ctx, _client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "settings.get", {})
        assert [box["name"] for box in result["stash_boxes"]] == ["StashDB", "ThePornDB"]
        # No API key anywhere near the settings payload.
        assert all(set(box) == {"name", "endpoint"} for box in result["stash_boxes"])

    def test_saving_an_unknown_setting_is_refused(self, fd_repo, fd_config, fd_scene):
        ctx, client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "settings.set", {"values": {"nonsense": 1}})
        assert result["ok"] is False
        assert client.settings == {}

    def test_saving_clamps_and_reports(self, fd_repo, fd_config, fd_scene):
        ctx, client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "settings.set", {"values": {"maxDepth": 99}})
        assert client.settings["maxDepth"] == 10
        assert result["problems"]


class TestMaintenance:
    def test_a_run_whose_process_died_is_swept(self, fd_repo, fd_config, fd_scene):
        run_id = fd_repo.start_run(295, "manual", {}, {})
        fd_repo.connection.execute(
            "UPDATE runs SET heartbeat_at = ?, started_at = ? WHERE id = ?",
            (R.ago(60 * 60 * 48), R.ago(60 * 60 * 48), run_id))
        fd_repo.connection.commit()

        ctx, _client = context(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "maintenance.run", {})
        assert result["stale_runs_failed"] == 1
        assert fd_repo.run(run_id)["status"] == R.FAILED
