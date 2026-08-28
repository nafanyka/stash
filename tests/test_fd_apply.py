"""Apply: one write, only what was ticked, and nothing created behind the user's back."""

from __future__ import annotations

import base64

import pytest
from fd_common import FakeStash, scraped

from fastdiscovery import apply as apply_module, discovery, merge
from fastdiscovery.db import repo as R

STASHDB = "https://stashdb.org/graphql"
TPDB = "https://theporndb.net/graphql"
URL_A = "https://sitea.com/scene/1"


def prepared(fd_repo, config, scene, responses, entities=None):
    """Run discovery, then hand back (client, run, review) ready to apply."""
    client = FakeStash(scene=scene, responses=responses, entities=entities or {})
    summary = discovery.Runner(client, fd_repo, config).run(295)
    run = fd_repo.run(summary["run_id"])
    review = merge.build(fd_repo, run, scene, client=client)
    return client, run, review


def row(review, field):
    return next(entry for entry in review["rows"] if entry["field"] == field)


def value_id(review, field, display):
    for value in row(review, field)["values"]:
        if value.get("display") == display or value.get("name") == display:
            return value["id"]
    raise AssertionError("no %r in %s" % (display, field))


class TestNothingHappensByDefault:
    def test_the_default_selection_changes_nothing(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="A better title", performers=["Nobody Known"])})
        selection = merge.default_selection(review)
        result = apply_module.commit(fd_repo, client, run, fd_scene, selection)
        assert result["applied"] is False
        assert client.updates == []
        assert client.created == []

    def test_a_field_absent_from_the_selection_is_left_alone(self, fd_repo, fd_config,
                                                             fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="New", code="ABC")})
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"title": value_id(review, "title", "New")})
        assert sorted(client.updates[0]) == ["id", "title"]


class TestScalars:
    def test_choosing_a_scraper_value_writes_exactly_that_field(self, fd_repo,
                                                                fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="Title 1", date="January 17, 2024")})
        apply_module.commit(fd_repo, client, run, fd_scene, {
            "title": value_id(review, "title", "Title 1"),
            "date": value_id(review, "date", "2024-01-17")})
        update = client.updates[0]
        assert update["title"] == "Title 1"
        # The date is written in Stash's format, not the scraper's spelling of it.
        assert update["date"] == "2024-01-17"
        assert update["id"] == "295"

    def test_an_unknown_option_is_refused_before_anything_is_written(
            self, fd_repo, fd_config, fd_scene):
        client, run, _review = prepared(fd_repo, fd_config, fd_scene,
                                        {STASHDB: scraped(title="X")})
        with pytest.raises(apply_module.ApplyError):
            apply_module.commit(fd_repo, client, run, fd_scene, {"title": "v99"})
        assert client.updates == []


class TestEntities:
    def test_an_unselected_candidate_is_never_created(self, fd_repo, fd_config,
                                                      fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=["Some New Person"])})
        performers = row(review, "performers")
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"performers": performers["default"]})
        assert client.created == []

    def test_a_selected_candidate_is_created_and_linked(self, fd_repo, fd_config,
                                                        fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=[{"name": "Some New Person",
                                          "remote_site_id": "p-9",
                                          "urls": ["https://stashdb.org/performers/p-9"]}])})
        performers = row(review, "performers")
        wanted = performers["default"] + [value_id(review, "performers",
                                                   "Some New Person")]
        apply_module.commit(fd_repo, client, run, fd_scene, {"performers": wanted})

        kind, values = client.created[0]
        assert kind == "performer"
        assert values["name"] == "Some New Person"
        # Created with the identity the box gave it, so the new record is not a bare
        # name that the next scrape fails to match again.
        assert values["stash_ids"] == [{"endpoint": STASHDB, "stash_id": "p-9"}]
        assert "new-performer-1" in client.updates[0]["performer_ids"]
        assert "9" in client.updates[0]["performer_ids"]

    def test_the_same_candidate_under_two_fields_is_created_once(self, fd_repo,
                                                                 fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=["Twice Over"]),
            TPDB: scraped(performers=["Twice Over"])})
        performers = row(review, "performers")
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"performers": [entity["id"]
                                            for entity in performers["values"]]})
        assert len([one for one in client.created if one[0] == "performer"]) == 1

    def test_unticking_an_existing_performer_removes_it(self, fd_repo, fd_config,
                                                        fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="x")})
        apply_module.commit(fd_repo, client, run, fd_scene, {"performers": []})
        assert client.updates[0]["performer_ids"] == []

    def test_a_selected_studio_candidate_is_created(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(studio="Brand New Studio")})
        apply_module.commit(fd_repo, client, run, fd_scene, {
            "studio": value_id(review, "studio", "Brand New Studio")})
        assert client.created[0][0] == "studio"
        assert client.updates[0]["studio_id"] == "new-studio-1"

    def test_a_tag_stash_already_has_is_linked_rather_than_created(
            self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(
            fd_repo, fd_config, fd_scene, {STASHDB: scraped(tags=["Known Tag"])},
            entities={"tag": {"Known Tag": [{"id": "77", "name": "Known Tag"}]}})
        tags = row(review, "tags")
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"tags": [entity["id"] for entity in tags["values"]]})
        assert client.created == []
        assert sorted(client.updates[0]["tag_ids"]) == ["3", "77"]


class TestListsAndImages:
    def test_urls_are_written_as_the_ticked_set(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(urls=["https://siteb.com/v/2"])})
        urls = row(review, "urls")
        apply_module.commit(fd_repo, client, run, fd_scene, {"urls": urls["default"]})
        assert sorted(client.updates[0]["urls"]) == sorted(
            [URL_A, "https://siteb.com/v/2"])

    def test_an_image_url_is_handed_to_stash_untouched(self, fd_repo, fd_config,
                                                       fd_scene):
        cover = "https://cdn.example.com/cover.jpg"
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(image=cover)})
        chosen = [entry for entry in row(review, "image")["values"]
                  if entry["kind"] == "url"][0]
        apply_module.commit(fd_repo, client, run, fd_scene, {"image": chosen["id"]})
        # Stash fetches the URL itself; FastDiscovery never downloads a cover.
        assert client.updates[0]["cover_image"] == cover

    def test_a_base64_image_is_rebuilt_from_the_blob(self, fd_repo, fd_config,
                                                     fd_scene):
        raw = b"pretend this is a jpeg"
        uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(image=uri)})
        chosen = [entry for entry in row(review, "image")["values"]
                  if entry["kind"] == "blob"][0]
        apply_module.commit(fd_repo, client, run, fd_scene, {"image": chosen["id"]})
        assert client.updates[0]["cover_image"] == uri

    def test_a_stash_id_is_written_with_its_endpoint(self, fd_repo, fd_config,
                                                     fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="x", remote_site_id="scene-1")})
        stash_ids = row(review, "stash_ids")
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"stash_ids": [value["id"] for value in stash_ids["values"]]})
        assert client.updates[0]["stash_ids"] == [
            {"endpoint": STASHDB, "stash_id": "scene-1"}]


class TestAfterwards:
    def test_a_successful_apply_deletes_the_payload_and_keeps_the_audit(
            self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"title": value_id(review, "title", "New")})

        after = fd_repo.run(run["id"])
        assert after["status"] == R.APPLIED
        assert after["purged"] is True
        counts = fd_repo.counts()
        assert counts["results"] == 0 and counts["sources"] == 0 and counts["urls"] == 0
        assert counts["images"] == 0
        # The audit says what happened, and holds no metadata.
        history = fd_repo.applications_for(295)
        assert history[0]["status"] == "APPLIED"
        assert history[0]["fields"] == ["title"]

    def test_a_failed_apply_keeps_everything_so_it_can_be_retried(
            self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})

        def explode(_values):
            raise RuntimeError("HTTP 500 from Stash")

        client.scene_update = explode
        with pytest.raises(apply_module.ApplyError):
            apply_module.commit(fd_repo, client, run, fd_scene,
                                {"title": value_id(review, "title", "New")})

        after = fd_repo.run(run["id"])
        assert after["status"] == R.FAILED_APPLY
        assert after["purged"] is False
        assert after["reviewable"] is True
        assert fd_repo.counts()["results"] > 0

    def test_reject_writes_nothing_and_drops_the_payload(self, fd_repo, fd_config,
                                                         fd_scene):
        client, run, _review = prepared(fd_repo, fd_config, fd_scene,
                                        {STASHDB: scraped(title="New")})
        apply_module.reject(fd_repo, run)
        assert client.updates == []
        after = fd_repo.run(run["id"])
        assert after["status"] == R.REJECTED
        assert after["purged"] is True
        assert fd_repo.counts()["results"] == 0

    def test_a_scene_edited_since_the_review_blocks_the_write(self, fd_repo, fd_config,
                                                              fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})
        with pytest.raises(apply_module.ApplyError):
            apply_module.commit(fd_repo, client, run, fd_scene,
                                {"title": value_id(review, "title", "New")},
                                expected_updated_at="2020-01-01T00:00:00Z")
        assert client.updates == []


class TestPreview:
    def test_preview_reports_the_changes_without_making_them(self, fd_repo, fd_config,
                                                             fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="New", performers=["Somebody New"])})
        selection = merge.default_selection(review)
        selection["title"] = value_id(review, "title", "New")
        selection["performers"] = [entity["id"] for entity
                                   in row(review, "performers")["values"]]

        plan = apply_module.preview(fd_repo, client, run, fd_scene, selection)
        assert [change["field"] for change in plan["changes"]] == ["title", "performers"]
        assert [create["entity"]["name"] for create in plan["creates"]] == ["Somebody New"]
        assert client.updates == [] and client.created == []


class TestSelectionSurvivesARebuild:
    """A review is built to be shown, and built again when Apply resolves it.

    Between the two, Stash's answer about an unmatched name can change, which reorders
    the row and can even fold two options into one. A selection made against the first
    build has to still mean the same thing against the second.
    """

    def test_an_option_id_does_not_depend_on_position(self, fd_repo, fd_config,
                                                      fd_scene):
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(title="One", performers=["Zoe Last", "Aaron First"])})
        summary = discovery.Runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])

        first = merge.build(fd_repo, run, fd_scene)
        second = merge.build(fd_repo, run, fd_scene)
        assert {entity["id"] for entity in row(first, "performers")["values"]} == \
            {entity["id"] for entity in row(second, "performers")["values"]}
        assert row(first, "title")["values"][0]["id"] == \
            row(second, "title")["values"][0]["id"]

    def test_a_tick_still_applies_after_two_options_turn_out_to_be_one(
            self, fd_repo, fd_config, fd_scene):
        # At review time Stash had not matched the scraped name, so it was a candidate
        # alongside the tag already on the scene. By apply time the name lookup matches
        # it, and the two become one option.
        blind = FakeStash(scene=fd_scene,
                          responses={STASHDB: scraped(tags=["Existing Tag"])})
        summary = discovery.Runner(blind, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])

        review = merge.build(fd_repo, run, fd_scene, client=blind)
        candidate = [entity for entity in row(review, "tags")["values"]
                     if not entity["existing"]][0]
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]

        seeing = FakeStash(
            scene=fd_scene, responses={STASHDB: scraped(tags=["Existing Tag"])},
            entities={"tag": {"Existing Tag": [{"id": "3", "name": "Existing Tag"}]}})
        result = apply_module.commit(fd_repo, seeing, run, fd_scene, {"tags": ticked})

        # One tag, the one the library already had, and nothing created.
        assert seeing.created == []
        assert result["applied"] is False or seeing.updates[0]["tag_ids"] == ["3"]
        assert candidate["id"] in ticked
