"""The scraper registry: URL routing, planning and scraper identity.

The URL rule is the one that matters most. It is copied from Stash
(`pkg/scraper/definition.go`, `strings.Contains(url, pattern)`) rather than
approximated, because a looser rule claims scrapers Stash would not use and a stricter
one misses ones it would.
"""

from __future__ import annotations

from scrapediscovery import normalize as N, registry as G, settings as S


def build(scrapers, raw_config=None, boxes=None):
    config = S.parse(raw_config or {})
    return G.Registry(G.from_list_scrapers(scrapers), config, boxes or [])


class TestCapabilities:
    def test_rows_become_records_with_a_fingerprint(self, scrapers):
        records = G.from_list_scrapers(scrapers)
        by_id = {one["id"]: one for one in records}
        assert by_id["SiteA"]["kinds"] == ["FRAGMENT", "URL"]
        assert by_id["SiteA"]["url_patterns"] == ["sitea.com/scene/"]
        assert len(by_id["SiteA"]["fingerprint"]) == 32

    def test_rows_without_an_id_are_dropped(self):
        assert G.from_list_scrapers([{"id": "", "scene": {}}]) == []

    def test_capability_views_respect_exclusion(self, scrapers):
        registry = build(scrapers, {"excludedScrapers": "Filename"})
        assert "Filename" not in [one["id"] for one in registry.with_kind("FRAGMENT")]


class TestScraperIdentity:
    def test_the_fingerprint_changes_when_anything_observable_changes(self):
        base = {"id": "A", "name": "A", "kinds": ["URL"], "url_patterns": ["a.com"]}
        original = G.fingerprint_of(base)
        assert G.fingerprint_of(dict(base, name="A2")) != original
        assert G.fingerprint_of(dict(base, kinds=["URL", "NAME"])) != original
        assert G.fingerprint_of(dict(base, url_patterns=["a.com", "b.com"])) != original

    def test_the_fingerprint_is_stable_across_pattern_order(self):
        left = G.fingerprint_of({"id": "A", "url_patterns": ["a.com", "b.com"]})
        right = G.fingerprint_of({"id": "A", "url_patterns": ["b.com", "a.com"]})
        assert left == right

    def test_fields_cannot_be_confused_with_each_other(self):
        # A joined string would let "ab" + "" hash the same as "a" + "b".
        assert G.fingerprint_of({"id": "ab", "name": ""}) != \
            G.fingerprint_of({"id": "a", "name": "b"})


class TestUrlRouting:
    def test_a_pattern_matches_anywhere_in_the_url(self, scrapers):
        registry = build(scrapers)
        found = registry.handlers_for_url("https://www.sitea.com/scene/12345?utm=x")
        assert [one["id"] for one in found] == ["SiteA"]

    def test_matching_is_case_insensitive(self, scrapers):
        registry = build(scrapers)
        assert registry.handlers_for_url("https://SiteA.COM/Scene/1")

    def test_a_url_nothing_matches_returns_nothing(self, scrapers):
        assert build(scrapers).handlers_for_url("https://nowhere.invalid/x") == []

    def test_several_handlers_are_ordered_by_priority(self):
        rows = [
            {"id": "Low", "name": "Low", "scene": {"urls": ["shared.com/"],
                                                   "supported_scrapes": ["URL"]}},
            {"id": "High", "name": "High", "scene": {"urls": ["shared.com/"],
                                                     "supported_scrapes": ["URL"]}},
        ]
        registry = build(rows, {"scraperOverrides": {"High": {"priority": "high"}}})
        assert [one["id"] for one in registry.handlers_for_url("https://shared.com/x")] == \
            ["High", "Low"]

    def test_a_disabled_scraper_is_not_a_handler(self, scrapers):
        registry = build(scrapers, {"scraperOverrides": {"SiteA": {"priority": "disabled"}}})
        assert registry.handlers_for_url("https://sitea.com/scene/1") == []

    def test_hosts_are_extracted_for_domain_routing(self, scrapers):
        registry = build(scrapers)
        siteA = registry.by_id["SiteA"]
        assert registry.hosts_of(siteA) == {"sitea.com"}
        assert [one["id"] for one in registry.routed_for_hosts({"example.com"})] == ["Example"]


class TestAttribution:
    def test_one_handler_means_the_source_is_known(self, scrapers):
        registry = build(scrapers)
        work = registry.url_work({"url": "https://sitea.com/scene/1",
                                  "key": "sitea.com/scene/1", "host": "sitea.com"}, 1)
        assert work["attribution"] == G.CERTAIN
        assert work["scraper"]["id"] == "SiteA"

    def test_several_handlers_mean_the_source_is_uncertain(self):
        # Stash picks the handler by iterating a Go map and does not report which one
        # ran, so claiming a specific scraper here would be a lie.
        rows = [
            {"id": "One", "name": "One", "scene": {"urls": ["shared.com/"],
                                                   "supported_scrapes": ["URL"]}},
            {"id": "Two", "name": "Two", "scene": {"urls": ["shared.com/v/"],
                                                   "supported_scrapes": ["URL"]}},
        ]
        work = build(rows).url_work({"url": "https://shared.com/v/9",
                                     "key": "shared.com/v/9", "host": "shared.com"}, 1)
        assert work["attribution"] == G.AMBIGUOUS
        assert work["scraper"] is None
        assert sorted(work["handlers"]) == ["One", "Two"]

    def test_no_handler_means_no_work(self, scrapers):
        assert build(scrapers).url_work(
            {"url": "https://nowhere.invalid/x", "key": "nowhere.invalid/x"}, 1) is None


class TestTargetKeys:
    def test_a_url_key_does_not_name_a_scraper(self):
        # Stash chooses the handler and may choose differently next time, so the URL
        # alone is the thing being asked about.
        item = {"method": G.M_URL, "url_key": "x.com/a", "scraper": {"id": "Whoever"}}
        assert G.target_key(item) == "URL|x.com/a"

    def test_a_fragment_key_names_the_scraper(self):
        assert G.target_key({"method": G.M_FRAGMENT_SCENE, "scraper": {"id": "A"}}) == \
            "FRAGMENT_SCENE|A|"

    def test_a_name_key_is_insensitive_to_query_formatting(self):
        left = G.target_key({"method": G.M_NAME, "scraper": {"id": "A"},
                             "target": "The  Example Scene!"})
        right = G.target_key({"method": G.M_NAME, "scraper": {"id": "A"},
                              "target": "the example scene"})
        assert left == right

    def test_different_methods_never_collide(self):
        keys = {
            G.target_key({"method": G.M_FRAGMENT_SCENE, "scraper": {"id": "A"}}),
            G.target_key({"method": G.M_FRAGMENT_INPUT, "scraper": {"id": "A"},
                          "target": "https://x.com/1"}),
            G.target_key({"method": G.M_NAME, "scraper": {"id": "A"}, "target": "x"}),
            G.target_key({"method": G.M_STASHBOX_FP, "scraper": {"id": "A"}}),
        }
        assert len(keys) == 4


class TestScrapeInputs:
    def test_a_fragment_scrape_asks_stash_to_build_the_fragment(self):
        item = {"method": G.M_FRAGMENT_SCENE, "scraper": {"id": "A"}}
        assert G.scrape_input_for(item, 42) == {"scene_id": "42"}
        assert G.source_for(item) == {"scraper_id": "A"}

    def test_a_name_scrape_sends_only_the_query(self):
        item = {"method": G.M_NAME, "scraper": {"id": "A"}, "target": "Example"}
        assert G.scrape_input_for(item, 42) == {"query": "Example"}

    def test_a_synthetic_fragment_carries_the_url_and_known_fields(self):
        # The only way to aim a specific scraper at a specific URL: there is no
        # per-scraper URL call in the API.
        item = {"method": G.M_FRAGMENT_INPUT, "scraper": {"id": "A"},
                "target": "https://x.com/1"}
        payload = G.scrape_input_for(item, 42, {"title": "T", "date": "2024-01-01",
                                                "duration": 99})
        assert payload["scene_input"]["url"] == "https://x.com/1"
        assert payload["scene_input"]["title"] == "T"
        # duration is not a field of ScrapedSceneInput, so it must not be sent.
        assert "duration" not in payload["scene_input"]

    def test_a_stashbox_uses_its_endpoint_not_a_scraper_id(self):
        item = {"method": G.M_STASHBOX_FP, "scraper": {"id": "https://box/graphql"}}
        assert G.source_for(item) == {"stash_box_endpoint": "https://box/graphql"}

    def test_a_url_attempt_has_no_source(self):
        assert G.source_for({"method": G.M_URL}) is None


class TestPlanning:
    def _snapshot(self, scene):
        return N.scene_snapshot(scene)

    def test_the_scenes_own_urls_come_first(self, scrapers, scene):
        registry = build(scrapers)
        plan = registry.plan(self._snapshot(scene))
        assert plan[0]["method"] == G.M_URL
        assert plan[0]["target"] == "https://example.com/scene/1"

    def test_a_normal_scan_leaves_name_search_out(self, scrapers, scene):
        plan = build(scrapers).plan(self._snapshot(scene), S.NORMAL)
        assert not [one for one in plan if one["method"] == G.M_NAME]

    def test_a_deep_scan_includes_it(self, scrapers, scene):
        plan = build(scrapers).plan(self._snapshot(scene), S.DEEP)
        assert [one["scraper"]["id"] for one in plan if one["method"] == G.M_NAME]

    def test_name_search_can_be_turned_on_for_normal_scans(self, scrapers, scene):
        registry = build(scrapers, {"normalIncludesNameScrapers": True})
        assert [one for one in registry.plan(self._snapshot(scene), S.NORMAL)
                if one["method"] == G.M_NAME]

    def test_a_scene_with_nothing_to_search_for_gets_no_name_attempts(self, scrapers):
        bare = N.scene_snapshot({"id": "9", "files": []})
        plan = build(scrapers).plan(bare, S.DEEP)
        assert not [one for one in plan if one["method"] == G.M_NAME]

    def test_the_scraper_that_owns_the_site_is_tried_before_the_rest(self, scrapers, scene):
        plan = build(scrapers).plan(self._snapshot(scene), S.NORMAL)
        fragments = [one for one in plan if one["method"] == G.M_FRAGMENT_SCENE]
        assert fragments[0]["scraper"]["id"] == "Example"
        assert fragments[0]["routed"] is True

    def test_restricting_a_normal_scan_to_routed_scrapers(self, scrapers, scene):
        registry = build(scrapers, {"normalIncludesUnroutedFragments": False})
        fragments = [one for one in registry.plan(self._snapshot(scene), S.NORMAL)
                     if one["method"] == G.M_FRAGMENT_SCENE]
        assert [one["scraper"]["id"] for one in fragments] == ["Example"]

    def test_stash_boxes_are_planned_by_fingerprint(self, scrapers, scene):
        registry = build(scrapers, boxes=[{"endpoint": "https://box/graphql", "name": "Box"}])
        plan = registry.plan(self._snapshot(scene), S.NORMAL)
        assert [one["method"] for one in plan if one["method"] == G.M_STASHBOX_FP]

    def test_every_item_is_ordered_and_keyed(self, scrapers, scene):
        plan = build(scrapers).plan(self._snapshot(scene), S.DEEP)
        assert [one["order"] for one in plan] == list(range(len(plan)))
        assert len({one["target_key"] for one in plan}) == len(plan)

    def test_planning_is_reproducible(self, scrapers, scene):
        registry = build(scrapers)
        first = registry.plan(self._snapshot(scene), S.DEEP)
        second = registry.plan(self._snapshot(scene), S.DEEP)
        assert [one["target_key"] for one in first] == [one["target_key"] for one in second]

    def test_untried_scrapers_are_marked(self, scrapers, scene):
        registry = build(scrapers)
        known = {one["id"]: one["fingerprint"] for one in registry.enabled()}
        stale = dict(known)
        stale["Example"] = "an older fingerprint"
        del stale["Searcher"]

        plan = registry.plan(self._snapshot(scene), S.DEEP, already_tried=stale)
        flags = {one["scraper"]["id"]: one["is_new"] for one in plan
                 if one.get("scraper") and one["method"] == G.M_FRAGMENT_SCENE}
        assert flags["Example"] is True   # changed since it was tried
        assert flags["Searcher"] is True  # never tried
        assert flags["SiteA"] is False    # tried, unchanged
