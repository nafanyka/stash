"""Configuration parsing.

Stash gives a plugin setting no declarable default and only three types, so this
module is the only thing standing between an untouched install and nonsense values.
"""

from __future__ import annotations

import json

from scrapediscovery import settings as S


class TestDefaults:
    def test_an_untouched_install_gets_every_default(self):
        config = S.parse({})
        assert config.problems == []
        assert config["defaultMode"] == S.NORMAL
        assert config["maxConcurrency"] == 3
        assert config["autoApply"] is False
        assert set(config.weights()) == set(S.DEFAULT_SCORE_WEIGHTS)

    def test_an_absent_boolean_is_the_default_not_false(self):
        # Stash stores an untouched boolean as absent, so "missing" cannot be read as
        # "off" - manageTags happens to default off, normalIncludesUnroutedFragments on.
        assert S.parse({})["normalIncludesUnroutedFragments"] is True
        assert S.parse({"normalIncludesUnroutedFragments": False})[
            "normalIncludesUnroutedFragments"] is False

    def test_local_utility_scrapers_are_excluded_by_default(self):
        excluded = S.parse({}).excluded()
        assert "filename" in excluded
        assert "builtin_autotag" in excluded


class TestCoercion:
    def test_numbers_arrive_as_strings_from_the_stash_ui(self):
        config = S.parse({"maxDepth": "4", "titleMergeThreshold": "0.9"})
        assert config["maxDepth"] == 4
        assert abs(config["titleMergeThreshold"] - 0.9) < 1e-9

    def test_booleans_accept_the_usual_spellings(self):
        for raw in ("true", "1", "yes", "on", True):
            assert S.parse({"debugLogging": raw})["debugLogging"] is True
        for raw in ("false", "0", "no", "off", False):
            assert S.parse({"debugLogging": raw})["debugLogging"] is False

    def test_a_broken_value_falls_back_instead_of_raising(self):
        # One bad setting must not stop discovery working.
        config = S.parse({"maxDepth": "not a number"})
        assert config["maxDepth"] == 2

    def test_json_settings_accept_text_or_an_object(self):
        as_text = S.parse({"scraperOverrides": json.dumps({"A": {"priority": "high"}})})
        as_object = S.parse({"scraperOverrides": {"A": {"priority": "high"}}})
        assert as_text.priority("A") == as_object.priority("A") == "high"

    def test_invalid_json_is_reported_and_defaulted(self):
        config = S.parse({"scraperOverrides": "{not json"})
        assert config.override("anything") == {}
        assert any("scraperOverrides" in problem for problem in config.problems)


class TestGuards:
    def test_concurrency_is_clamped(self):
        assert S.parse({"maxConcurrency": 0})["maxConcurrency"] == 1
        capped = S.parse({"maxConcurrency": 500})
        assert capped["maxConcurrency"] == 16
        assert any("maxConcurrency" in problem for problem in capped.problems)

    def test_absurdly_short_timeouts_are_raised(self):
        config = S.parse({"defaultTimeout": 1})
        assert config["defaultTimeout"] == 5
        assert config.problems

    def test_an_unknown_mode_is_rejected(self):
        config = S.parse({"defaultMode": "sideways"})
        assert config["defaultMode"] == S.NORMAL
        assert config.problems


class TestPerScraper:
    def test_priority_and_enablement(self):
        config = S.parse({"scraperOverrides": {"Slow": {"priority": "disabled"},
                                               "Good": {"priority": "high"}}})
        assert config.is_enabled("Slow") is False
        assert config.is_enabled("Good") is True
        assert config.priority("Good") == "high"
        assert config.priority("Unknown") == "normal"

    def test_exclusion_matches_id_or_name_case_insensitively(self):
        config = S.parse({"excludedScrapers": "SiteA, other name"})
        assert config.is_enabled("sitea") is False
        assert config.is_enabled("X", "Other Name") is False
        assert config.is_enabled("Keep", "Keep") is True

    def test_a_bad_priority_is_reported(self):
        config = S.parse({"scraperOverrides": {"A": {"priority": "urgent"}}})
        assert any("urgent" in problem for problem in config.problems)

    def test_timeouts_fall_back_by_method_then_override(self):
        config = S.parse({"scraperOverrides": {"Slow": {"timeout": 90}}})
        assert config.timeout_for("Slow", "FRAGMENT_SCENE") == 90
        assert config.timeout_for("Other", "FRAGMENT_SCENE") == 30
        assert config.timeout_for("Other", "NAME") == 60

    def test_a_per_scraper_cache_override_only_moves_the_long_ttls(self):
        # A scraper that is slow to publish should not also stop erroring quickly.
        config = S.parse({"scraperOverrides": {"S": {"cacheDays": 7}}})
        assert config.cache_days("S", "MATCH") == 7
        assert config.cache_days("S", "NO_MATCH") == 7
        assert config.cache_days("S", "ERROR") == config["ttlErrorDays"]


class TestLevels:
    def test_confidence_levels_use_the_configured_bounds(self):
        config = S.parse({})
        assert config.level_of(99) == "almost_certain"
        assert config.level_of(85) == "strong"
        assert config.level_of(70) == "possible"
        assert config.level_of(10) == "weak"
        assert config.level_of(None) == "unknown"

    def test_the_bounds_can_be_changed(self):
        config = S.parse({"confidenceLevels": {"almost_certain": 90}})
        assert config.level_of(92) == "almost_certain"

    def test_weights_merge_over_the_defaults(self):
        config = S.parse({"scoreWeights": {"duration": 50, "nonsense": 1}})
        weights = config.weights()
        assert weights["duration"] == 50
        assert "nonsense" not in weights
        assert weights["title_similarity"] == S.DEFAULT_SCORE_WEIGHTS["title_similarity"]


class TestClearedValues:
    def test_clearing_the_excluded_list_really_clears_it(self):
        # Stash cannot tell a cleared text box from an untouched one - both arrive as
        # "" - so without this the field would look empty in the UI while the eight
        # default exclusions were still in force.
        assert S.parse({"excludedScrapers": ""}).excluded() == set()
        assert S.parse({"excludedScrapers": "   "}).excluded() == set()

    def test_an_absent_excluded_list_still_gets_the_defaults(self):
        assert "filename" in S.parse({}).excluded()

    def test_clearing_a_normal_string_setting_still_falls_back(self):
        # defaultMode has no meaningful empty value, so an empty box means "default".
        assert S.parse({"defaultMode": ""})["defaultMode"] == S.NORMAL
        assert S.parse({"defaultMode": ""}).problems == []
