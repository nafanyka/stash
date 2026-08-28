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
        titles = next(row for row in first["rows"] if row["field"] == "title")
        scraped_title = next(value["id"] for value in titles["values"]
                             if value["display"] == "One")

        ops.dispatch(ctx, "review.save", {"run_id": first["run"]["id"],
                                          "selection": {"title": scraped_title}})
        again = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        assert again["selection"]["title"] == scraped_title

    def test_a_saved_choice_that_no_longer_exists_falls_back(self, fd_repo, fd_config,
                                                             fd_scene):
        # Not an error: the option can be gone because its source was struck out. A
        # scalar left pointing at nothing would silently write nothing, so the row
        # returns to its default.
        ctx, _client = discovered(fd_repo, fd_config, fd_scene,
                                  {STASHDB: scraped(title="One")})
        run = fd_repo.latest_run(295)
        ops.dispatch(ctx, "review.save", {"run_id": run["id"],
                                          "selection": {"title": "v_gone"}})
        again = ops.dispatch(ctx, "review.get", {"scene_id": "295"})
        titles = next(row for row in again["rows"] if row["field"] == "title")
        assert again["selection"]["title"] == titles["default"]

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


class TestRejectingAColumn:
    """A result that got the wrong scene is wrong about every field at once."""

    def setup_run(self, fd_repo, fd_config, fd_scene):
        ctx, client = context(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="Right", date="2024-01-17",
                             performers=["Only StashDB Knows Her"]),
            "https://theporndb.net/graphql": scraped(title="Wrong Scene Entirely",
                                                     date="1999-01-01"),
        })
        discovery.Runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.latest_run(295)
        review = ops.dispatch(ctx, "review.get", {"run_id": run["id"]})
        wrong = [column for column in review["columns"]
                 if column["name"] == "ThePornDB"][0]
        return ctx, run, wrong

    def test_rejecting_drops_everything_that_column_said(self, fd_repo, fd_config,
                                                         fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        before = ops.dispatch(ctx, "review.get", {"run_id": run["id"]})
        titles = next(row for row in before["rows"] if row["field"] == "title")
        assert "Wrong Scene Entirely" in {value["display"] for value in titles["values"]}

        after = ops.dispatch(ctx, "review.reject_column",
                             {"run_id": run["id"], "column_id": wrong["id"]})
        titles = next(row for row in after["rows"] if row["field"] == "title")
        assert "Wrong Scene Entirely" not in {value["display"]
                                              for value in titles["values"]}
        # The date it was the only source for goes with it, so the row is gone too.
        dates = [row for row in after["rows"] if row["field"] == "date"]
        assert {value["display"] for value in dates[0]["values"]} == {"2024-01-17"}

    def test_the_column_stays_so_the_choice_can_be_taken_back(self, fd_repo, fd_config,
                                                              fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        after = ops.dispatch(ctx, "review.reject_column",
                             {"run_id": run["id"], "column_id": wrong["id"]})
        column = [one for one in after["columns"] if one["id"] == wrong["id"]][0]
        assert column["rejected"] is True
        assert after["summary"]["rejected_columns"] == 1
        # ...and every cell of it is empty, whatever the row's kind.
        for row in after["rows"]:
            cell = row["cells"][column["id"]]
            assert cell is None or cell == []

    def test_unrejecting_brings_it_back(self, fd_repo, fd_config, fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        ops.dispatch(ctx, "review.reject_column",
                     {"run_id": run["id"], "column_id": wrong["id"]})
        back = ops.dispatch(ctx, "review.reject_column",
                            {"run_id": run["id"], "column_id": wrong["id"],
                             "rejected": False})
        titles = next(row for row in back["rows"] if row["field"] == "title")
        assert "Wrong Scene Entirely" in {value["display"] for value in titles["values"]}
        assert back["rejected_columns"] == []

    def test_the_decision_survives_a_reload(self, fd_repo, fd_config, fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        ops.dispatch(ctx, "review.reject_column",
                     {"run_id": run["id"], "column_id": wrong["id"]})
        again = ops.dispatch(ctx, "review.get", {"run_id": run["id"]})
        assert again["rejected_columns"] == [wrong["id"]]

    def test_a_tick_on_a_value_only_that_column_had_does_not_survive(
            self, fd_repo, fd_config, fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        before = ops.dispatch(ctx, "review.get", {"run_id": run["id"]})
        titles = next(row for row in before["rows"] if row["field"] == "title")
        doomed = next(value["id"] for value in titles["values"]
                      if value["display"] == "Wrong Scene Entirely")
        ops.dispatch(ctx, "review.save", {"run_id": run["id"],
                                          "selection": {"title": doomed}})

        after = ops.dispatch(ctx, "review.reject_column",
                             {"run_id": run["id"], "column_id": wrong["id"]})
        titles = next(row for row in after["rows"] if row["field"] == "title")
        assert after["selection"]["title"] == titles["default"]
        assert after["selection"]["title"] != doomed

    def test_apply_uses_the_same_set_the_review_was_shown_with(self, fd_repo, fd_config,
                                                               fd_scene):
        ctx, run, wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        after = ops.dispatch(ctx, "review.reject_column",
                             {"run_id": run["id"], "column_id": wrong["id"]})
        titles = next(row for row in after["rows"] if row["field"] == "title")
        right = next(value["id"] for value in titles["values"]
                     if value["display"] == "Right")
        preview = ops.dispatch(ctx, "apply.preview",
                               {"run_id": run["id"], "selection": {"title": right}})
        assert preview["problems"] == []
        assert [change["display"] for change in preview["changes"]] == ["Right"]

    def test_a_column_that_is_not_this_run_s_is_refused(self, fd_repo, fd_config,
                                                        fd_scene):
        ctx, run, _wrong = self.setup_run(fd_repo, fd_config, fd_scene)
        result = ops.dispatch(ctx, "review.reject_column",
                              {"run_id": run["id"], "column_id": "s999_0"})
        assert result["ok"] is False


class TestRejectingOneOfSeveralResults:
    """The case rejection exists for: one source, several answers, one of them wrong.

    A stash-box name search returns a list, and each answer is its own column - so the
    first can be the scene and the second a different film. Rejecting has to be able to
    strike out the second without touching the first.
    """

    def prepared(self, fd_repo, fd_scene):
        config = settings.parse({"stashboxNameSearch": True})
        ctx, client = context(fd_repo, config, fd_scene, {
            STASHDB + "?query": [
                scraped(title="The Right Scene", date="2024-01-17"),
                scraped(title="A Different Film", date="1999-05-05"),
            ],
        })
        discovery.Runner(client, fd_repo, config).run(295)
        run = fd_repo.latest_run(295)
        review = ops.dispatch(ctx, "review.get", {"run_id": run["id"]})
        return ctx, run, review

    def test_one_source_can_produce_several_columns(self, fd_repo, fd_scene):
        _ctx, _run, review = self.prepared(fd_repo, fd_scene)
        names = [column["name"] for column in review["columns"]]
        assert "StashDB (search)" in names
        assert "StashDB (search) #2" in names
        # Both columns, one source.
        columns = [column for column in review["columns"]
                   if str(column["name"]).startswith("StashDB (search)")]
        assert len({column["source_id"] for column in columns}) == 1

    def test_rejecting_the_second_leaves_the_first_alone(self, fd_repo, fd_scene):
        ctx, run, review = self.prepared(fd_repo, fd_scene)
        second = [column for column in review["columns"]
                  if column["name"] == "StashDB (search) #2"][0]

        after = ops.dispatch(ctx, "review.reject_column",
                             {"run_id": run["id"], "column_id": second["id"]})

        titles = next(row for row in after["rows"] if row["field"] == "title")
        shown = {value["display"] for value in titles["values"]}
        assert "The Right Scene" in shown
        assert "A Different Film" not in shown

        first = [column for column in after["columns"]
                 if column["name"] == "StashDB (search)"][0]
        assert first["rejected"] is False
        assert titles["cells"][first["id"]] is not None
        assert titles["cells"][second["id"]] is None

        # The date only the wrong answer had goes with it.
        dates = next(row for row in after["rows"] if row["field"] == "date")
        assert {value["display"] for value in dates["values"]} == {"2024-01-17"}
