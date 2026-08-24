"""URL normalisation, result normalisation and fingerprinting.

These are the functions every later stage trusts: correlation only works if two
scrapers' spelling of one URL collapses to the same key, and reprocessing only works
if a fingerprint is stable across runs.
"""

from __future__ import annotations

import base64

from scrapediscovery import normalize as N


class TestUrlNormalisation:
    def test_scheme_host_case_and_www_are_not_identity(self):
        keys = {
            N.normalize_url(url)["key"]
            for url in (
                "https://www.Example.com/Scene/1",
                "http://example.com/Scene/1",
                "https://EXAMPLE.com/Scene/1/",
                "https://example.com:443/Scene/1",
            )
        }
        assert keys == {"example.com/Scene/1"}

    def test_path_case_is_preserved(self):
        # Plenty of sites route on a case-sensitive slug, so folding the path would
        # merge two genuinely different pages.
        assert N.normalize_url("https://x.com/A")["key"] != \
            N.normalize_url("https://x.com/a")["key"]

    def test_tracking_parameters_are_dropped_and_the_rest_sorted(self):
        parsed = N.normalize_url("https://x.com/p?b=2&utm_source=news&a=1&ref=z")
        assert parsed["key"] == "x.com/p?a=1&b=2"

    def test_meaningful_parameters_survive(self):
        assert N.normalize_url("https://x.com/v?id=99")["key"] == "x.com/v?id=99"

    def test_fragment_and_duplicate_slashes_go(self):
        assert N.normalize_url("https://x.com//a//b/#top")["key"] == "x.com/a/b"

    def test_non_http_schemes_are_refused(self):
        for url in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
                    "ftp://x.com/f", "not a url", "", None):
            assert N.normalize_url(url) is None
            assert N.is_safe_url(url) is False

    def test_a_port_that_is_not_default_is_kept(self):
        assert N.normalize_url("http://x.com:8080/a")["key"] == "x.com:8080/a"

    def test_host_is_returned_without_www(self):
        assert N.normalize_url("https://www.x.co.uk/a")["host"] == "x.co.uk"


class TestFingerprinting:
    def test_key_order_and_whitespace_do_not_change_the_fingerprint(self):
        assert N.fingerprint({"a": 1, "b": [1, 2]}) == N.fingerprint({"b": [1, 2], "a": 1})

    def test_different_content_changes_the_fingerprint(self):
        assert N.fingerprint({"a": 1}) != N.fingerprint({"a": 2})

    def test_list_order_does_change_it(self):
        # Order is content for a list; only mapping key order is incidental.
        assert N.fingerprint([1, 2]) != N.fingerprint([2, 1])


class TestTextHelpers:
    def test_title_from_filename_drops_technical_noise(self):
        assert N.title_from_filename("Example_Scene_1080p_x264-KTR.mp4") == \
            "Example Scene KTR"

    def test_title_from_filename_keeps_an_unknown_word(self):
        # Over-cleaning invents a title the site never used, which is worse for a name
        # search than leaving a stray token in.
        assert "Gear" in N.title_from_filename("211 - No Girls Allowed Gear vr.mp4")

    def test_similarity_ranks_sensibly(self):
        exact = N.similarity("Example Scene", "Example Scene")
        ampersand = N.similarity("Fucked & Fertile", "Fucked and Fertile")
        contained = N.similarity("Example Scene", "Studio X - Example Scene (2024)")
        unrelated = N.similarity("Alpha Beta", "Zulu Yankee")
        assert exact == 1.0
        assert 0.8 < ampersand < 1.0
        assert 0.6 < contained < 1.0
        assert unrelated < 0.4

    def test_only_an_exact_match_scores_one(self):
        # A permutation is a strong signal but not the same title; if it scored 1.0 an
        # exact match could not outrank it.
        assert N.similarity("No Girls Allowed", "Girls No Allowed") < 1.0

    def test_names_ignore_spacing(self):
        # Observed live: Stash held the studio as "18VR", the site's own scraper
        # returned "18 VR". Treating that as a disagreement would cost a real signal.
        assert N.canon_name("18VR") == N.canon_name("18 VR")
        assert N.canon_name("Alice O'Brien-Smith") == N.canon_name("alice obrien smith")

    def test_titles_keep_their_word_boundaries(self):
        assert N.canon_text("The Example") == "the example"


class TestScalars:
    def test_dates(self):
        assert N.parse_date("2025-04-07") == "2025-04-07"
        assert N.parse_date("2025-04-07T12:30:00") == "2025-04-07"
        assert N.parse_date("April 7, 2025") == "2025-04-07"
        assert N.parse_date("September 10, 2025") == "2025-09-10"
        assert N.parse_date("20250407") == "2025-04-07"
        assert N.parse_date("07/04/2025") == "2025-04-07"  # day-first, documented

    def test_unparseable_dates_are_dropped_not_guessed(self):
        for value in ("garbage", "", None, "2025", "13/13/2025"):
            assert N.parse_date(value) is None

    def test_durations(self):
        assert N.parse_duration(2889.09) == 2889
        assert N.parse_duration("1:23:45") == 5025
        assert N.parse_duration("48:10") == 2890
        assert N.parse_duration("83 min") == 4980
        assert N.parse_duration(0) is None
        assert N.parse_duration(None) is None
        assert N.parse_duration("unknown") is None


class TestImageExternalisation:
    def _uri(self, payload=b"\xff\xd8\xffhello"):
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode()

    def test_an_image_is_replaced_by_a_reference(self):
        stripped, images = N.externalize_images({"title": "x", "image": self._uri()})
        assert len(images) == 1
        assert stripped["image"]["$blob"] == images[0]["sha256"]
        assert stripped["title"] == "x"

    def test_the_original_can_be_rebuilt_byte_for_byte(self):
        original = self._uri(b"some jpeg bytes")
        stripped, images = N.externalize_images({"image": original})
        assert N.rebuild_data_uri(stripped["image"], images[0]) == original

    def test_identical_images_share_one_hash(self):
        _stripped, images = N.externalize_images(
            {"image": self._uri(), "studio": {"image": self._uri()}})
        assert len({image["sha256"] for image in images}) == 1

    def test_a_non_image_data_uri_is_refused(self):
        stripped, images = N.externalize_images({"image": "data:text/html;base64,PGI+"})
        assert images == []
        assert stripped["image"]["$blob"] is None

    def test_oversized_images_are_refused(self):
        payload = base64.b64encode(b"x" * (N.MAX_IMAGE_BYTES + 10)).decode()
        assert N.split_data_uri("data:image/jpeg;base64," + payload) is None


class TestSceneResult:
    def test_fields_are_normalised_and_urls_deduplicated(self):
        result = N.scene_result({
            "title": "  Example Scene  ",
            "date": "January 20, 2024",
            "urls": ["https://www.Example.com/Scene/1/",
                     "http://example.com/Scene/1"],
            "studio": {"name": "Example Studio"},
            "performers": [{"name": "Alice"}, {"name": "alice"}],
            "duration": "30:00",
        })
        assert result["title"] == "Example Scene"
        assert result["date"] == "2024-01-20"
        assert result["duration"] == 1800
        assert len(result["urls"]) == 1
        assert len(result["performers"]) == 1

    def test_urls_in_details_are_found(self):
        result = N.scene_result({"details": "mirror at https://other.com/x/2 today"})
        assert [one["host"] for one in result["urls"]] == ["other.com"]

    def test_related_urls_are_separated_from_scene_urls(self):
        # A performer's homepage is not a scene page; handing it to a scene URL
        # scraper would spend an attempt for nothing.
        raw = {"urls": ["https://a.com/scene/1"],
               "performers": [{"name": "Alice", "url": "https://a.com/model/alice"}]}
        roles = {entry["url"]: entry["role"] for entry in N.extract_urls(raw)}
        assert roles["https://a.com/scene/1"] == N.ROLE_SCENE
        assert roles["https://a.com/model/alice"] == N.ROLE_RELATED
        assert [one["url"] for one in N.scene_result(raw)["urls"]] == \
            ["https://a.com/scene/1"]

    def test_an_all_null_payload_counts_as_empty(self):
        # Several scrapers answer a fragment scrape this way rather than with nothing,
        # and storing it would put an empty candidate in front of the user.
        empty = N.scene_result({"title": None, "date": None, "performers": [],
                                "tags": [], "urls": []})
        assert N.is_empty_result(empty) is True

    def test_anything_at_all_counts_as_not_empty(self):
        assert N.is_empty_result(N.scene_result({"code": "ABC-1"})) is False
        assert N.is_empty_result(N.scene_result({"tags": [{"name": "x"}]})) is False


class TestSceneSnapshot:
    def test_the_search_term_falls_back_to_the_filename(self, scene):
        snapshot = N.scene_snapshot(scene)
        assert snapshot["search_term"] == "Example Scene"

        untitled = dict(scene, title=None)
        assert N.scene_snapshot(untitled)["search_term"] == "example scene"

    def test_fingerprints_and_duration_come_from_the_primary_file(self, scene):
        snapshot = N.scene_snapshot(scene)
        assert snapshot["duration"] == 1800
        assert {one["algorithm"] for one in snapshot["fingerprints"]} == {"oshash", "phash"}

    def test_a_scene_with_no_files_does_not_explode(self):
        snapshot = N.scene_snapshot({"id": "7"})
        assert snapshot["duration"] is None
        assert snapshot["display_title"] == "scene 7"
