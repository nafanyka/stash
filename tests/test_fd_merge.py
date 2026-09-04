"""The review matrix: one value per answer, entities resolved, empty rows absent."""

from __future__ import annotations

from fd_common import FakeStash, scraped

from fastdiscovery import discovery, merge

STASHDB = "https://stashdb.org/graphql"
TPDB = "https://theporndb.net/graphql"
URL_A = "https://sitea.com/scene/1"


def review_of(fd_repo, config, scene, responses, client=None, use_client=False):
    stash = client or FakeStash(scene=scene, responses=responses)
    summary = discovery.Runner(stash, fd_repo, config).run(295)
    run = fd_repo.run(summary["run_id"])
    return merge.build(fd_repo, run, scene, client=stash if use_client else None)


def row(review, field):
    for entry in review["rows"]:
        if entry["field"] == field:
            return entry
    return None


class TestEmptyRows:
    """Specification section 50."""

    def test_a_field_empty_everywhere_has_no_row(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene,
                           {STASHDB: scraped(title="Test")})
        assert row(review, "director") is None

    def test_a_field_only_a_scraper_has_gets_a_row(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene,
                           {STASHDB: scraped(director="Some Director")})
        assert row(review, "director") is not None

    def test_a_field_only_the_scene_has_gets_a_row(self, fd_repo, fd_config, fd_scene):
        # It is what Apply would keep, so it belongs in the picture.
        fd_scene["director"] = "Local Director"
        review = review_of(fd_repo, fd_config, fd_scene, {STASHDB: scraped(title="x")})
        director = row(review, "director")
        assert [value["display"] for value in director["values"]] == ["Local Director"]
        assert director["default"] == director["values"][0]["id"]


class TestProvenance:
    """Specification section 18: one logical value, many source references."""

    def test_the_same_answer_from_four_sources_is_one_value(self, fd_repo, fd_config,
                                                            fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(date="2024-01-17"),
            TPDB: scraped(date="January 17, 2024"),
            URL_A: scraped(date="2024-01-17T00:00:00Z"),
        })
        dates = row(review, "date")
        assert len(dates["values"]) == 1
        assert len(dates["values"][0]["sources"]) == 3

    def test_values_that_differ_stay_separate(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(date="2024-01-17"),
            TPDB: scraped(date="2024-01-18"),
        })
        assert len(row(review, "date")["values"]) == 2

    def test_every_column_maps_to_the_value_it_gave(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="One"), TPDB: scraped(title="Two")})
        titles = row(review, "title")
        by_id = {value["id"]: value["display"] for value in titles["values"]}
        rendered = {column["name"]: by_id.get(titles["cells"][column["id"]])
                    for column in review["columns"]}
        assert rendered["Current"] == "Old title"
        assert rendered["StashDB"] == "One"
        assert rendered["ThePornDB"] == "Two"
        # Site A answered with nothing, so it has no column at all - an empty column
        # would be a source claiming to have said something.
        assert "Site A" not in rendered


class TestDefaults:
    """Specification section 35: conservative, never a silent improvement."""

    def test_the_scene_value_is_what_is_selected(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {STASHDB: scraped(title="New")})
        titles = row(review, "title")
        chosen = [value for value in titles["values"]
                  if value["id"] == titles["default"]][0]
        assert chosen["display"] == "Old title"

    def test_an_empty_field_with_one_answer_selects_it(self, fd_repo, fd_config,
                                                       fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {STASHDB: scraped(code="ABC")})
        codes = row(review, "code")
        assert codes["default"] == codes["values"][0]["id"]

    def test_an_empty_field_with_two_answers_selects_neither(self, fd_repo, fd_config,
                                                             fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(code="ABC"), TPDB: scraped(code="XYZ")})
        assert row(review, "code")["default"] is None

    def test_every_url_is_selected_by_default(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(urls=["https://siteb.com/v/2"])})
        urls = row(review, "urls")
        assert sorted(urls["default"]) == sorted(value["id"]
                                                 for value in urls["values"])

    def test_a_discovered_stash_id_is_not_adopted_by_default(self, fd_repo, fd_config,
                                                             fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="x", remote_site_id="abc-123")})
        stash_ids = row(review, "stash_ids")
        assert [value["display"] for value in stash_ids["values"]] == [
            "abc-123 @ stashdb.org"]
        assert stash_ids["default"] == []


class TestEntityDedup:
    """Specification section 48."""

    def test_one_performer_three_sources(self, fd_repo, fd_config, fd_scene):
        # The scene has Angela White as local performer 9. StashDB reports her with a
        # stash id and her profile URL, ThePornDB with the same local id Stash matched,
        # a URL scraper with nothing but that same profile URL. All one entity, by
        # three different rules (requirement 13).
        profile = "https://stashdb.org/performers/xyz"
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=[{"name": "Angela White", "stored_id": "9",
                                          "remote_site_id": "xyz",
                                          "urls": [profile]}]),
            TPDB: scraped(performers=[{"name": "Angela White", "stored_id": "9"}]),
            URL_A: scraped(performers=[{"name": "Angela  White", "urls": [profile]}]),
        })
        performers = row(review, "performers")
        angela = [entity for entity in performers["values"]
                  if entity["name"] == "Angela White"]
        assert len(angela) == 1
        assert angela[0]["existing"] is True
        assert angela[0]["stored_id"] == "9"
        assert len(angela[0]["sources"]) >= 3

    def test_a_shared_stash_id_merges_two_mentions(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=[{"name": "Jane Doe",
                                          "remote_site_id": "p-1"}]),
            TPDB: scraped(performers=[{"name": "Jane D.", "remote_site_id": "p-1"}]),
        })
        # Different endpoints, so the stash ids do not match; the names do not either.
        names = {entity["name"] for entity in row(review, "performers")["values"]}
        assert {"Jane Doe", "Jane D."} <= names

    def test_a_name_alone_never_merges_with_an_identified_entity(
            self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=[{"name": "Angela White", "stored_id": "9"}]),
            TPDB: scraped(performers=[{"name": "Angela White"}]),
        })
        performers = row(review, "performers")
        angela = [entity for entity in performers["values"]
                  if entity["canon"] == "angela white"]
        # Two rows, and the unidentified one says what it might be, rather than being
        # silently fused with the local record.
        assert len(angela) == 2
        candidate = [entity for entity in angela if not entity["existing"]][0]
        assert candidate["possible_match"]["stored_id"] == "9"

    def test_two_nameless_candidates_with_the_same_name_are_one(self, fd_repo,
                                                                fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=["Some New Person"]),
            TPDB: scraped(performers=["some new person"]),
        })
        candidates = [entity for entity in row(review, "performers")["values"]
                      if not entity["existing"]]
        assert len(candidates) == 1
        assert len(candidates[0]["sources"]) == 2


class TestUnknownEntities:
    """Specification section 49: candidates are shown, never created."""

    def test_an_unknown_performer_is_a_candidate_and_is_not_selected(
            self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=["Some New Person"])})
        performers = row(review, "performers")
        candidate = [entity for entity in performers["values"]
                     if entity["name"] == "Some New Person"][0]
        assert candidate["existing"] is False
        assert candidate["id"] not in performers["default"]

    def test_an_existing_performer_is_selected(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(performers=[{"name": "Someone Known", "stored_id": "42"}])})
        performers = row(review, "performers")
        known = [entity for entity in performers["values"]
                 if entity["stored_id"] == "42"][0]
        assert known["id"] in performers["default"]

    def test_a_tag_stash_already_has_is_linked_by_name(self, fd_repo, fd_config,
                                                       fd_scene):
        # Stash sets stored_id itself when it can; a URL scraper returning a bare name
        # gets no such help, so the review asks Stash by exact name (requirement 14).
        client = FakeStash(scene=fd_scene,
                           responses={STASHDB: scraped(tags=["Existing Tag"])},
                           entities={"tag": {"Existing Tag": [{"id": "3",
                                                               "name": "Existing Tag"}]}})
        review = review_of(fd_repo, fd_config, fd_scene, None, client=client,
                           use_client=True)
        tags = row(review, "tags")
        existing = [entity for entity in tags["values"]
                    if entity["name"] == "Existing Tag"]
        assert len(existing) == 1
        assert existing[0]["existing"] is True

    def test_an_ambiguous_name_stays_a_candidate(self, fd_repo, fd_config, fd_scene):
        client = FakeStash(
            scene=fd_scene, responses={STASHDB: scraped(performers=["Alex"])},
            entities={"performer": {"Alex": [{"id": "1", "name": "Alex"},
                                             {"id": "2", "name": "Alex"}]}})
        review = review_of(fd_repo, fd_config, fd_scene, None, client=client,
                           use_client=True)
        alex = [entity for entity in row(review, "performers")["values"]
                if entity["name"] == "Alex"][0]
        assert alex["existing"] is False
        assert len(alex["ambiguous_matches"]) == 2


class TestStudio:
    def test_a_studio_is_a_single_choice(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(studio="Studio One"), TPDB: scraped(studio="Studio Two")})
        studio = row(review, "studio")
        assert studio["kind"] == "entity"
        assert len(studio["values"]) == 2
        # Neither exists locally and the scene has none, so nothing is preselected.
        assert studio["default"] is None


class TestImages:
    """Specification section 53."""

    def test_identical_images_are_one_candidate_with_two_sources(self, fd_repo,
                                                                 fd_config, fd_scene):
        same = "https://cdn.example.com/cover.jpg"
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(image="https://cdn.example.com/a.jpg"),
            TPDB: scraped(image=same),
            URL_A: scraped(image=same + "?utm_source=x"),
        })
        images = row(review, "image")
        assert len(images["values"]) == 3        # the scene's own cover plus two
        shared = [entry for entry in images["values"]
                  if entry.get("url", "").startswith(same)][0]
        assert len(shared["sources"]) == 2

    def test_a_base64_image_is_stored_once_and_referenced(self, fd_repo, fd_config,
                                                          fd_scene):
        import base64
        payload = base64.b64encode(b"not really a jpeg").decode()
        uri = "data:image/jpeg;base64," + payload
        client = FakeStash(scene=fd_scene, responses={
            STASHDB: scraped(image=uri), TPDB: scraped(image=uri)})
        summary = discovery.Runner(client, fd_repo, fd_config).run(295)
        assert fd_repo.counts()["images"] == 1

        review = merge.build(fd_repo, fd_repo.run(summary["run_id"]), fd_scene)
        blobs = [entry for entry in row(review, "image")["values"]
                 if entry["kind"] == "blob"]
        assert len(blobs) == 1
        assert len(blobs[0]["sources"]) == 2

    def test_the_current_cover_is_the_default(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(image="https://cdn.example.com/a.jpg")})
        images = row(review, "image")
        chosen = [entry for entry in images["values"]
                  if entry["id"] == images["default"]][0]
        assert chosen["kind"] == "scene"


class TestSummary:
    def test_the_summary_counts_what_the_scene_page_shows(self, fd_repo, fd_config,
                                                          fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="One", performers=["Nobody Known"]),
            TPDB: RuntimeError("HTTP 500"),
        })
        summary = merge.summarise(review)
        assert summary["failed_sources"] == 1
        assert summary["new_entities"] >= 1
        assert summary["columns"] >= 2


class TestCellShape:
    """A cell's shape has to match its row's, or the selection is malformed.

    This is the invariant that a single-choice studio row broke: it handed the UI a
    one-element list, the UI put that list into the selection where an option id
    belongs, and Apply could not resolve it - taking every other field down with it,
    because a selection is applied as a whole or not at all.
    """

    def test_single_choice_rows_map_a_column_to_one_option(self, fd_repo, fd_config,
                                                           fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(title="One", studio="Studio One",
                             image="https://cdn/a.jpg"),
            TPDB: scraped(title="Two", studio="Studio Two"),
        })
        for name in ("title", "studio", "image"):
            entry = row(review, name)
            ids = {value["id"] for value in entry["values"]}
            for column, cell in entry["cells"].items():
                assert cell is None or isinstance(cell, str), (name, column, cell)
                assert cell is None or cell in ids, (name, column, cell)
            assert entry["default"] is None or isinstance(entry["default"], str)

    def test_list_rows_map_a_column_to_a_list_of_options(self, fd_repo, fd_config,
                                                         fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(tags=["A", "B"], performers=["P"],
                             urls=["https://siteb.com/v/2"], remote_site_id="r1"),
        })
        for name in ("tags", "performers", "urls", "stash_ids"):
            entry = row(review, name)
            ids = {value["id"] for value in entry["values"]}
            for column, cell in entry["cells"].items():
                assert isinstance(cell, list), (name, column, cell)
                assert set(cell) <= ids, (name, column, cell)
            assert isinstance(entry["default"], list)

    def test_a_studio_can_be_chosen_and_applied(self, fd_repo, fd_config, fd_scene):
        # The end-to-end version of the same thing: pick the studio a column reported,
        # exactly as clicking that cell in the UI does, and apply it.
        from fastdiscovery import apply as apply_module
        client = FakeStash(scene=fd_scene,
                           responses={STASHDB: scraped(studio="Studio One")})
        summary = discovery.Runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        review = merge.build(fd_repo, run, fd_scene, client=client)

        studio_column = [column for column in review["columns"]
                         if column["name"] == "StashDB"][0]
        chosen = row(review, "studio")["cells"][studio_column["id"]]
        result = apply_module.commit(fd_repo, client, run, fd_scene,
                                     {"studio": chosen})
        assert result["applied"] is True
        assert client.created[0][0] == "studio"
        assert client.updates[0]["studio_id"] == "new-studio-1"


class TestUrlRow:
    """What ends up in the scene's URL list, and what only ends up in the graph."""

    def test_every_url_a_source_claims_as_the_scene_is_offered(self, fd_repo, fd_config,
                                                               fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(urls=["https://siteb.com/v/2"]),
            TPDB: scraped(url="https://sitec.com/x/3"),
        })
        assert {value["raw"] for value in row(review, "urls")["values"]} == {
            "https://sitea.com/scene/1", "https://siteb.com/v/2", "https://sitec.com/x/3"}

    def test_a_url_found_by_following_another_is_offered_too(self, fd_repo, fd_config,
                                                             fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(urls=["https://siteb.com/v/2"]),
            "https://siteb.com/v/2": scraped(urls=["https://sitec.com/x/3"]),
            "https://sitec.com/x/3": scraped(title="three"),
        })
        assert "https://sitec.com/x/3" in {value["raw"] for value
                                          in row(review, "urls")["values"]}

    def test_a_url_mentioned_in_prose_is_a_lead_not_a_scene_url(self, fd_repo,
                                                                fd_config, fd_scene):
        # It is still followed and still in the graph - it just is not a claim that the
        # scene lives there, so it is not offered as one, let alone ticked by default.
        review = review_of(fd_repo, fd_config, fd_scene, {
            STASHDB: scraped(details="mirrored at https://siteb.com/v/2"),
        })
        assert "https://siteb.com/v/2" not in {value["raw"] for value
                                              in row(review, "urls")["values"]}
        assert "https://siteb.com/v/2" in {entry["url"] for entry
                                           in review["urls_graph"]}


class TestAliases:
    """A name the library owns as an alias is not a name the library is missing.

    Stash refuses to create a tag whose name is another tag's alias, so treating such a
    name as a candidate produces a review that promises something Apply cannot deliver.
    """

    def review_for(self, fd_repo, fd_config, fd_scene, tags, library, scene_tags=None):
        scene = dict(fd_scene, tags=scene_tags if scene_tags is not None
                     else fd_scene["tags"])
        client = FakeStash(scene=scene, responses={STASHDB: scraped(tags=tags)},
                           entities={"tag": library})
        summary = discovery.Runner(client, fd_repo, fd_config).run(295)
        run = fd_repo.run(summary["run_id"])
        return client, merge.build(fd_repo, run, scene, client=client)

    def test_a_scraped_name_owned_as_an_alias_resolves_to_its_owner(
            self, fd_repo, fd_config, fd_scene):
        library = {"Couple Sex": [{"id": "71", "name": "Couple Sex",
                                   "aliases": ["Couple Sex (Straight)"]}]}
        _client, review = self.review_for(fd_repo, fd_config, fd_scene,
                                          ["Couple Sex (Straight)"], library)
        entity = next(one for one in row(review, "tags")["values"]
                      if one["stored_id"] == "71")
        assert entity["existing"] is True
        assert entity["matched_by"] == "alias"
        # The library's own spelling is what goes on the scene, so the reviewer is not
        # shown one name and given a differently named record.
        assert entity["name"] == "Couple Sex"
        assert "Couple Sex (Straight)" in entity["aliases"]

    def test_the_owner_already_on_the_scene_stays_one_option(self, fd_repo, fd_config,
                                                             fd_scene):
        library = {"Couple Sex": [{"id": "71", "name": "Couple Sex",
                                   "aliases": ["Couple Sex (Straight)"]}]}
        _client, review = self.review_for(
            fd_repo, fd_config, fd_scene, ["Couple Sex (Straight)"], library,
            scene_tags=[{"id": "71", "name": "Couple Sex"}])
        matching = [one for one in row(review, "tags")["values"]
                    if one["stored_id"] == "71"]
        assert len(matching) == 1
        assert matching[0]["on_scene"] is True

    def test_a_name_nothing_owns_is_still_a_candidate(self, fd_repo, fd_config,
                                                      fd_scene):
        _client, review = self.review_for(fd_repo, fd_config, fd_scene,
                                          ["Genuinely New"], {})
        entity = next(one for one in row(review, "tags")["values"]
                      if one["name"] == "Genuinely New")
        assert entity["existing"] is False

    def test_two_records_answering_to_it_settle_nothing(self, fd_repo, fd_config,
                                                        fd_scene):
        """An ambiguous alias is a question only the user can answer."""
        library = {
            "Couple Sex": [{"id": "71", "name": "Couple Sex",
                            "aliases": ["Couple Sex (Straight)"]}],
            "Straight": [{"id": "72", "name": "Straight",
                          "aliases": ["Couple Sex (Straight)"]}],
        }
        _client, review = self.review_for(fd_repo, fd_config, fd_scene,
                                          ["Couple Sex (Straight)"], library)
        entity = next(one for one in row(review, "tags")["values"]
                      if one["canon"] == "couple sex straight")
        assert entity["existing"] is False
        assert len(entity["ambiguous_matches"]) == 2


class TestTheSceneHeader:
    def test_the_review_carries_the_full_file_path(self, fd_repo, fd_config, fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene,
                           {STASHDB: scraped(title="New")})
        assert review["scene"]["path"] == "/media/example_scene_1080p.mp4"
        assert review["scene"]["id"] == "295"


class TestTheCurrentCover:
    def test_the_scene_cover_is_offered_as_an_option(self, fd_repo, fd_config,
                                                     fd_scene):
        review = review_of(fd_repo, fd_config, fd_scene,
                           {STASHDB: scraped(image="https://cdn/a.jpg")})
        images = row(review, "image")
        current = [one for one in images["values"] if one["is_current"]]
        assert len(current) == 1
        assert images["cells"]["current"] == current[0]["id"]
        assert images["default"] == current[0]["id"]

    def test_its_address_is_left_for_the_browser_to_resolve(self, fd_repo, fd_config,
                                                            fd_scene):
        """Stash builds paths.screenshot from the host *this plugin* connected to.

        Handing that to a browser somewhere else gives it an address only the server can
        reach, and the cover silently fails to load. The path is the part that travels.
        """
        review = review_of(fd_repo, fd_config, fd_scene,
                           {STASHDB: scraped(title="New")})
        current = next(one for one in row(review, "image")["values"]
                       if one["is_current"])
        assert current["url"] == "/scene/295/screenshot"
