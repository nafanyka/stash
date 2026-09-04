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
        # tag_ids carries the FastDiscovery marker, which every apply adds. `code` was
        # scraped and not selected, so it is not in the write at all.
        assert sorted(client.updates[0]) == ["id", "tag_ids", "title"]


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
            entities={"tag": {"Known Tag": [{"id": "77", "name": "Known Tag"}],
                              # The marker already exists here, so this test stays
                              # about the scraped tag and nothing else.
                              "FastDiscovery": [{"id": "9", "name": "FastDiscovery"}]}})
        tags = row(review, "tags")
        apply_module.commit(fd_repo, client, run, fd_scene,
                            {"tags": [entity["id"] for entity in tags["values"]]})
        assert client.created == []
        assert sorted(client.updates[0]["tag_ids"]) == ["3", "77", "9"]


class TestTheMarkerTag:
    """Every apply leaves the scene tagged FastDiscovery, and nothing else does.

    The point of the tag is to tell a scene FastDiscovery has written to apart from one
    it has never touched, so it is written by exactly the operation that writes.
    """

    def test_an_applied_scene_comes_out_tagged(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})
        result = apply_module.commit(fd_repo, client, run, fd_scene,
                                     {"title": value_id(review, "title", "New")})
        assert client.created == [("tag", {"name": "FastDiscovery"})]
        assert result["marker"]["created"] is True
        # The tag the scene already had is still there: marking adds one, removes none.
        assert client.updates[0]["tag_ids"] == ["3", "new-tag-1"]

    def test_a_marker_stash_already_has_is_linked_not_created(self, fd_repo, fd_config,
                                                              fd_scene):
        client, run, review = prepared(
            fd_repo, fd_config, fd_scene, {STASHDB: scraped(title="New")},
            entities={"tag": {"FastDiscovery": [{"id": "42",
                                                 "name": "FastDiscovery"}]}})
        result = apply_module.commit(fd_repo, client, run, fd_scene,
                                     {"title": value_id(review, "title", "New")})
        assert client.created == []
        assert result["marker"] == {"id": "42", "name": "FastDiscovery",
                                    "created": False}
        assert client.updates[0]["tag_ids"] == ["3", "42"]

    def test_a_scene_that_already_carries_it_is_left_alone(self, fd_repo, fd_config,
                                                           fd_scene):
        scene = dict(fd_scene, tags=[{"id": "3", "name": "Existing Tag"},
                                     {"id": "42", "name": "FastDiscovery"}])
        client, run, review = prepared(fd_repo, fd_config, scene,
                                       {STASHDB: scraped(title="New")})
        result = apply_module.commit(fd_repo, client, run, scene,
                                     {"title": value_id(review, "title", "New")})
        # Nothing to add, and tags were not being written, so tag_ids stays out of the
        # update entirely rather than being rewritten with itself.
        assert "tag_ids" not in client.updates[0]
        assert result["marker"] is None
        assert client.created == []

    def test_it_joins_the_tags_that_were_ticked(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(tags=["Known Tag"])},
                                       entities={"tag": {"Known Tag": [
                                           {"id": "77", "name": "Known Tag"}]}})
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]
        apply_module.commit(fd_repo, client, run, fd_scene, {"tags": ticked})
        assert client.updates[0]["tag_ids"] == ["3", "77", "new-tag-1"]

    def test_unticking_a_tag_still_removes_it(self, fd_repo, fd_config, fd_scene):
        """The marker is added to the reviewed set, it does not preserve it."""
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})
        apply_module.commit(fd_repo, client, run, fd_scene, {"tags": []})
        assert client.updates[0]["tag_ids"] == ["new-tag-1"]

    def test_nothing_selected_means_no_tag_either(self, fd_repo, fd_config, fd_scene):
        client, run, review = prepared(fd_repo, fd_config, fd_scene,
                                       {STASHDB: scraped(title="New")})
        result = apply_module.commit(fd_repo, client, run, fd_scene,
                                     merge.default_selection(review))
        assert result["applied"] is False
        assert client.updates == []
        assert client.created == []

    def test_a_rejected_run_leaves_no_mark(self, fd_repo, fd_config, fd_scene):
        client, run, _review = prepared(fd_repo, fd_config, fd_scene,
                                        {STASHDB: scraped(title="New")})
        apply_module.reject(fd_repo, run)
        assert client.updates == []
        assert client.created == []


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


class TestOnePerEndpoint:
    """A scene holds one stash id per box, so two for one endpoint are alternatives.

    Stash has a unique index on (scene_id, endpoint); sending two makes the write fail
    inside the database with a constraint violation the user cannot act on. It is caught
    here instead, before anything is created or written.
    """

    def prepared(self, fd_repo, fd_config, fd_scene):
        fd_scene["stash_ids"] = [{"endpoint": STASHDB, "stash_id": "old-one"}]
        return prepared(fd_repo, fd_config, fd_scene,
                        {STASHDB: scraped(title="T", remote_site_id="new-one")})

    def test_the_row_says_it_is_exclusive(self, fd_repo, fd_config, fd_scene):
        _client, _run, review = self.prepared(fd_repo, fd_config, fd_scene)
        assert row(review, "stash_ids")["exclusive_by"] == "endpoint"

    def test_two_ids_for_one_endpoint_are_refused_before_anything_is_written(
            self, fd_repo, fd_config, fd_scene):
        client, run, review = self.prepared(fd_repo, fd_config, fd_scene)
        both = [value["id"] for value in row(review, "stash_ids")["values"]]
        assert len(both) == 2

        with pytest.raises(apply_module.ApplyError) as failure:
            apply_module.commit(fd_repo, client, run, fd_scene, {"stash_ids": both})
        assert "one value per endpoint" in str(failure.value)
        assert client.updates == []

    def test_swapping_the_id_for_that_endpoint_works(self, fd_repo, fd_config,
                                                     fd_scene):
        client, run, review = self.prepared(fd_repo, fd_config, fd_scene)
        fresh = next(value["id"] for value in row(review, "stash_ids")["values"]
                     if value["stash_id"] == "new-one")
        apply_module.commit(fd_repo, client, run, fd_scene, {"stash_ids": [fresh]})
        assert client.updates[0]["stash_ids"] == [
            {"endpoint": STASHDB, "stash_id": "new-one"}]

    def test_ids_for_different_endpoints_still_coexist(self, fd_repo, fd_config,
                                                      fd_scene):
        fd_scene["stash_ids"] = [{"endpoint": STASHDB, "stash_id": "keep-me"}]
        client, run, review = prepared(
            fd_repo, fd_config, fd_scene,
            {TPDB: scraped(title="T", remote_site_id="tpdb-one")})
        both = [value["id"] for value in row(review, "stash_ids")["values"]]
        apply_module.commit(fd_repo, client, run, fd_scene, {"stash_ids": both})
        assert sorted(client.updates[0]["stash_ids"],
                      key=lambda one: one["endpoint"]) == [
            {"endpoint": STASHDB, "stash_id": "keep-me"},
            {"endpoint": TPDB, "stash_id": "tpdb-one"}]


class TestNeverCreatesWhatAlreadyExists:
    """The review says "does not exist yet" as of when it was built.

    Between then and Apply, the same tag can have been created by another scene's
    review, by a second browser tab, or by hand. Creating blindly turns that into a
    unique-constraint error from Stash's database with a whole apply lost behind it, so
    every ticked candidate is looked up once more immediately before it is created.
    """

    def library_client(self, scene, tags, library):
        """A fake Stash whose tags are shared between scenes, like a real one's."""
        client = FakeStash(scene=scene, boxes=[{"name": "StashDB", "endpoint": STASHDB}],
                           scrapers=[], entities=library,
                           responses={STASHDB: scraped(title="T", tags=tags)})

        def create_tag(values):
            name = values["name"]
            if name in library["tag"]:
                raise RuntimeError("UNIQUE constraint failed: tags.name")
            record = {"id": "tag-%d" % (len(library["tag"]) + 1), "name": name}
            library["tag"][name] = [record]
            client.created.append(("tag", values))
            return record

        client.create_tag = create_tag
        return client

    def apply_all_tags(self, fd_repo, fd_config, scene, tags, library):
        client = self.library_client(scene, tags, library)
        summary = discovery.Runner(client, fd_repo, fd_config).run(int(scene["id"]))
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, scene, client=client)
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]
        return client, apply_module.commit(fd_repo, client, run, scene,
                                           {"tags": ticked})

    def test_a_tag_another_scene_just_created_is_linked_not_created_again(
            self, fd_repo, fd_config, fd_scene):
        library = {"tag": {}}
        first = dict(fd_scene, id="101", tags=[])
        second = dict(fd_scene, id="102", tags=[])

        client_a, result_a = self.apply_all_tags(fd_repo, fd_config, first,
                                                 ["Brand New Tag"], library)
        # The ticked tag, and the marker every apply adds - both new to this library.
        assert [values["name"] for _kind, values in client_a.created] == [
            "Brand New Tag", "FastDiscovery"]
        assert result_a["created"]["tag"][0]["name"] == "Brand New Tag"

        client_b, result_b = self.apply_all_tags(fd_repo, fd_config, second,
                                                 ["Brand New Tag"], library)
        assert client_b.created == []
        assert result_b["created"] == {}
        # Small enough to be inside the review's lookup budget, so the second review
        # already knew it existed and it never reached the create stage at all - the
        # scene simply links the id the first one made. The marker is linked too.
        assert client_b.updates[0]["tag_ids"] == ["tag-1", "tag-2"]

    def test_it_holds_past_the_reviews_lookup_budget(self, fd_repo, fd_config,
                                                     fd_scene):
        # The review only looks up so many names before it stops labelling, so a tag
        # this far down the list still reads as a candidate. Apply must not believe it.
        tags = ["Filler %03d" % index
                for index in range(merge.MAX_NAME_LOOKUPS + 5)] + ["Last One"]
        library = {"tag": {}}
        first = dict(fd_scene, id="201", tags=[])
        second = dict(fd_scene, id="202", tags=[])

        self.apply_all_tags(fd_repo, fd_config, first, tags, library)
        client_b, result_b = self.apply_all_tags(fd_repo, fd_config, second, tags,
                                                 library)
        assert client_b.created == []
        assert result_b["applied"] is True

    def test_losing_the_race_outright_is_recovered_from(self, fd_repo, fd_config,
                                                        fd_scene):
        library = {"tag": {}}
        scene = dict(fd_scene, id="301", tags=[])
        client = self.library_client(scene, ["Racy Tag"], library)
        summary = discovery.Runner(client, fd_repo, fd_config).run(301)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, scene, client=client)
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]

        def create_tag(values):
            # Somebody else got there first, between the lookup and this call.
            library["tag"][values["name"]] = [{"id": "tag-99", "name": values["name"]}]
            raise RuntimeError("UNIQUE constraint failed: tags.name")

        client.create_tag = create_tag
        result = apply_module.commit(fd_repo, client, run, scene, {"tags": ticked})
        assert result["applied"] is True
        assert result["linked"]["tag"][0]["id"] == "tag-99"
        assert client.updates[0]["tag_ids"] == ["tag-99"]

    def test_a_create_that_really_fails_still_fails(self, fd_repo, fd_config, fd_scene):
        library = {"tag": {}}
        scene = dict(fd_scene, id="401", tags=[])
        client = self.library_client(scene, ["Doomed Tag"], library)
        summary = discovery.Runner(client, fd_repo, fd_config).run(401)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, scene, client=client)
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]

        def create_tag(_values):
            raise RuntimeError("the server said no")

        client.create_tag = create_tag
        with pytest.raises(apply_module.ApplyError) as failure:
            apply_module.commit(fd_repo, client, run, scene, {"tags": ticked})
        assert "the server said no" in str(failure.value)
        assert client.updates == []


class TestAliasesOnApply:
    """A name the library owns as an alias is linked, never created.

    Stash refuses the create outright - "name 'X' is used as alias for 'Y'" - so the
    only two outcomes available are linking to the record that owns it and failing.
    """

    def test_a_ticked_candidate_owned_as_an_alias_is_linked(self, fd_repo, fd_config,
                                                            fd_scene):
        client, run, review = prepared(
            fd_repo, fd_config, fd_scene,
            {STASHDB: scraped(tags=["Couple Sex (Straight)"])},
            entities={"tag": {"Couple Sex": [{"id": "71", "name": "Couple Sex",
                                              "aliases": ["Couple Sex (Straight)"]}],
                              "FastDiscovery": [{"id": "9",
                                                 "name": "FastDiscovery"}]}})
        ticked = [entity["id"] for entity in row(review, "tags")["values"]]
        result = apply_module.commit(fd_repo, client, run, fd_scene, {"tags": ticked})
        assert client.created == []
        assert result["created"] == {}
        assert sorted(client.updates[0]["tag_ids"]) == ["3", "71", "9"]

    def test_the_review_already_calls_it_existing(self, fd_repo, fd_config, fd_scene):
        _client, _run, review = prepared(
            fd_repo, fd_config, fd_scene,
            {STASHDB: scraped(tags=["Couple Sex (Straight)"])},
            entities={"tag": {"Couple Sex": [{"id": "71", "name": "Couple Sex",
                                              "aliases": ["Couple Sex (Straight)"]}]}})
        entity = next(one for one in row(review, "tags")["values"]
                      if one["stored_id"] == "71")
        # Ticked by default, because it is a record the library already has - a
        # candidate would not be.
        assert entity["id"] in row(review, "tags")["default"]
