"""URL normalisation, the field model, routing, settings - the pure parts."""

from __future__ import annotations

import pytest
from fd_common import SCRAPERS

from fastdiscovery import fields, registry, settings, urls


class TestUrlNormalisation:
    @pytest.mark.parametrize("left, right", [
        ("https://Example.com/scene/1", "https://example.com/scene/1"),
        ("http://example.com/scene/1", "https://example.com/scene/1"),
        ("https://www.example.com/scene/1", "https://example.com/scene/1"),
        ("https://example.com/scene/1/", "https://example.com/scene/1"),
        ("https://example.com/scene/1#anchor", "https://example.com/scene/1"),
        ("https://example.com/scene/1?utm_source=x", "https://example.com/scene/1"),
        ("https://example.com:443/scene/1", "https://example.com/scene/1"),
        ("https://example.com/scene//1", "https://example.com/scene/1"),
    ])
    def test_these_are_the_same_page(self, left, right):
        assert urls.normalize(left)["key"] == urls.normalize(right)["key"]

    @pytest.mark.parametrize("left, right", [
        # Path case is preserved: plenty of sites route on a case-sensitive slug.
        ("https://example.com/Scene/A", "https://example.com/scene/a"),
        # A meaningful query parameter is the page on a lot of sites.
        ("https://example.com/view?v=1", "https://example.com/view?v=2"),
        ("https://example.com/view?v=1", "https://example.com/view"),
        ("https://a.example.com/x", "https://b.example.com/x"),
    ])
    def test_these_are_not(self, left, right):
        assert urls.normalize(left)["key"] != urls.normalize(right)["key"]

    def test_the_original_spelling_is_never_lost(self):
        original = "https://WWW.Example.com/Scene/1/?utm_source=x"
        assert urls.normalize(original)["url"] == original

    @pytest.mark.parametrize("value", [
        "javascript:alert(1)", "data:text/html,<script>", "file:///etc/passwd",
        "ftp://example.com/x", "", None, "not a url",
    ])
    def test_anything_that_is_not_http_is_refused(self, value):
        assert urls.normalize(value) is None
        assert urls.is_safe(value) is False


class TestUrlsInResults:
    def test_scene_urls_and_urls_in_the_details_text_are_leads(self):
        found = urls.from_result({
            "url": "https://a.com/1",
            "urls": ["https://b.com/2"],
            "details": "originally from https://c.com/3, see also https://d.com/4.",
        })
        assert [entry["url"] for entry in found] == [
            "https://b.com/2", "https://a.com/1", "https://c.com/3", "https://d.com/4"]
        assert {entry["role"] for entry in found} == {urls.ROLE_SCENE}

    def test_a_performers_homepage_is_related_not_a_scene_page(self):
        found = urls.from_result({
            "performers": [{"name": "X", "url": "https://performer.example/x"}],
            "studio": {"name": "S", "urls": ["https://studio.example"]},
        })
        assert {entry["role"] for entry in found} == {urls.ROLE_RELATED}

    def test_the_field_a_url_came_from_is_recorded(self):
        found = urls.from_result({"details": "see https://c.com/3"})
        assert found[0]["source"] == "details"


class TestFieldModel:
    def test_every_writable_field_names_a_scene_update_field(self):
        for field in fields.FIELDS:
            if field.writable:
                assert field.update_key

    def test_a_field_a_future_stash_adds_still_gets_reviewed(self):
        extra = fields.extra_fields(["title", "date", "somethingNew"])
        assert [field.name for field in extra] == ["somethingNew"]
        # Shown, but never written: we do not know what it means.
        assert extra[0].writable is False

    def test_deprecated_spellings_do_not_become_rows_of_their_own(self):
        assert fields.extra_fields(["url", "movies", "file"]) == []

    @pytest.mark.parametrize("left, right", [
        ("2024-01-17", "January 17, 2024"),
        ("2024-01-17", "2024-01-17T12:00:00Z"),
        ("2024-01-17", "17/01/2024"),
    ])
    def test_dates_spelled_differently_compare_equal(self, left, right):
        field = fields.BY_NAME["date"]
        assert fields.scalar_key(field, left) == fields.scalar_key(field, right)

    def test_titles_compare_without_punctuation_or_case(self):
        field = fields.BY_NAME["title"]
        assert fields.scalar_key(field, "The Title!") == fields.scalar_key(
            field, "the title")

    def test_duration_is_not_a_row_at_all(self):
        # It comes from the scene's own file and can never be written, so a row for it
        # would be one nobody can act on.
        assert "duration" not in fields.BY_NAME
        assert fields.extra_fields(["duration"]) == []

    def test_a_filename_becomes_something_searchable(self):
        assert fields.title_from_filename(
            "Some.Scene.Name.1080p.x264-RARBG.mp4") == "Some Scene Name"


class TestRouting:
    def test_a_url_is_matched_the_way_stash_matches_it(self, fd_config):
        reg = registry.Registry(registry.from_list_scrapers(SCRAPERS), [], fd_config)
        found = reg.handlers_for("https://www.pornhub.com/view_video.php?viewkey=1")
        assert [entry["id"] for entry in found] == ["Pornhub"]

    def test_a_url_nothing_matches_has_no_handler(self, fd_config):
        reg = registry.Registry(registry.from_list_scrapers(SCRAPERS), [], fd_config)
        assert reg.handlers_for("https://nowhere.example/x") == []

    def test_a_single_handler_url_is_one_certain_source(self, fd_config):
        reg = registry.Registry(registry.from_list_scrapers(SCRAPERS), [], fd_config)
        sources, unreachable = reg.url_sources(
            {"url": "https://sitea.com/1", "key": "sitea.com/1", "host": "sitea.com"}, 0)
        assert len(sources) == 1
        assert sources[0]["attribution"] == registry.CERTAIN
        assert sources[0]["scraper_id"] == "SiteA"
        assert unreachable == []

    def test_a_shared_url_gets_an_ambiguous_scrape_plus_an_aimed_one(self, fd_config):
        reg = registry.Registry(registry.from_list_scrapers(SCRAPERS), [], fd_config)
        sources, unreachable = reg.url_sources(
            {"url": "https://shared.com/7", "key": "shared.com/7",
             "host": "shared.com"}, 1)
        methods = {(source["method"], source["scraper_id"]) for source in sources}
        assert (registry.M_URL, None) in methods
        assert (registry.M_URL_FRAGMENT, "SharedOne") in methods
        # SharedTwo is URL-only, so no API can aim at it; it is reported, not dropped.
        assert [entry["scraper_id"] for entry in unreachable] == ["SharedTwo"]

    def test_aiming_can_be_switched_off(self):
        config = settings.parse({"aimAmbiguousUrls": False})
        reg = registry.Registry(registry.from_list_scrapers(SCRAPERS), [], config)
        sources, _unreachable = reg.url_sources(
            {"url": "https://shared.com/7", "key": "shared.com/7",
             "host": "shared.com"}, 0)
        assert [source["method"] for source in sources] == [registry.M_URL]

    def test_the_source_key_is_the_scraper_url_pair(self, fd_config):
        first = registry.source_key({"method": registry.M_URL, "scraper_id": "A",
                                     "url_key": "x.com/1"})
        same = registry.source_key({"method": registry.M_URL, "scraper_id": "A",
                                    "url_key": "x.com/1"})
        other_url = registry.source_key({"method": registry.M_URL, "scraper_id": "A",
                                         "url_key": "x.com/2"})
        other_scraper = registry.source_key({"method": registry.M_URL, "scraper_id": "B",
                                             "url_key": "x.com/1"})
        assert first == same
        assert len({first, other_url, other_scraper}) == 3

    def test_a_box_is_asked_by_endpoint(self, fd_config):
        reg = registry.Registry([], [{"name": "Box", "endpoint": "https://b/graphql"}],
                                fd_config)
        source = reg.box_sources()[0]
        assert registry.graphql_source(source) == {
            "stash_box_endpoint": "https://b/graphql"}
        assert registry.graphql_input(source, 7) == {"scene_id": "7"}


class TestSettings:
    def test_defaults_apply_when_stash_sends_nothing(self):
        config = settings.parse({})
        assert config["maxDepth"] == 3
        assert config["maxConcurrentScrapers"] == 3
        assert config["recursiveUrlDiscovery"] is True

    def test_out_of_range_numbers_are_clamped_and_reported(self):
        config = settings.parse({"maxConcurrentScrapers": 500})
        assert config["maxConcurrentScrapers"] == 16
        assert config.problems

    def test_nonsense_falls_back_rather_than_failing(self):
        assert settings.parse({"maxDepth": "banana"})["maxDepth"] == 3

    def test_the_orchestrators_can_never_be_invoked(self):
        config = settings.parse({})
        for name in ("FastDiscovery", "ScrapeDiscovery", "ScrapeAll", "scrape all"):
            assert config.may_invoke(name) is False
        assert config.may_invoke("Pornhub") is True

    def test_there_is_no_stash_box_setting(self):
        # The endpoints and their keys belong to Stash; a second copy is a second thing
        # to leak (requirement 2, 43).
        assert not [name for name in settings.SPEC if "stashbox" in name.lower()
                    and name != "stashboxNameSearch"]


class TestLogSafety:
    def test_a_credential_in_a_message_is_redacted(self):
        from fastdiscovery import logs
        assert "sekrit" not in logs.sanitise("ApiKey sekrit123")
        assert "sekrit" not in logs.sanitise("api_key: sekrit123")
        assert "sekrit" not in logs.sanitise("https://box/graphql?apikey=sekrit123")
        assert "Authorization" in logs.sanitise("Authorization: Bearer sekrit")


class TestProgressProtocol:
    """Stash parses a progress line with strconv.ParseFloat - nothing may precede it."""

    def test_a_progress_line_carries_the_number_and_nothing_else(self, capsys):
        from fastdiscovery import logs
        logs.progress(0.5)
        line = capsys.readouterr().err.rstrip("\n")
        assert line.startswith("\x01p\x02")
        assert float(line[3:]) == 0.5

    def test_every_other_level_is_still_labelled(self, capsys):
        from fastdiscovery import logs
        logs.info("hello")
        assert capsys.readouterr().err == "\x01i\x02[FastDiscovery] hello\n"
