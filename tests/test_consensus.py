"""What independent sources agree on, and what is only pretending to agree.

Every case here is taken from one real scan of one real scene: 191 attempts, 21 of them
"matches", of which three were the scene. The rest are the reason this module exists.
"""

from __future__ import annotations

from scrapediscovery import consensus as C, registry as G


def result(id=1, scraper="Site", method=G.M_FRAGMENT_SCENE, title=None, date=None,
           code=None, details=None, urls=(), performers=(), tags=(), studio=None,
           fingerprints=(), image=None, target=""):
    """A stored result row, shaped as the repository returns it."""
    return {
        "id": id,
        "scraper_id": scraper,
        "scraper_name": scraper,
        "method": method,
        "target": target,
        "image_sha256": image,
        "normalized": {
            "title": title,
            "date": date,
            "code": code,
            "details": details,
            "studio": {"name": studio, "canon": (studio or "").lower().replace(" ", "")}
                      if studio else None,
            "performers": [{"name": one, "canon": one.lower().replace(" ", "")}
                           for one in performers],
            "tags": [{"name": one, "canon": one.lower().replace(" ", "")} for one in tags],
            "urls": [{"url": "https://%s" % one, "normalized": "https://%s" % one,
                      "key": one, "host": one.split("/")[0]} for one in urls],
            "fingerprints": list(fingerprints),
        },
    }


SCENE = {
    "title": "No Girls Allowed",
    "filename_title": "211 No Girls Allowed Gear",
    "urls": [{"key": "czechvrcasting.com/detail-1796", "host": "czechvrcasting.com",
              "url": "https://czechvrcasting.com/detail-1796"}],
}


class TestEvidence:
    def test_a_title_alone_is_never_evidence(self):
        # It could have come from the filename, from the fragment, or from nowhere.
        assert C.contributes_evidence(result(title="No Girls Allowed"), SCENE) is False
        assert C.contributes_evidence(result(title="Something Else Entirely"),
                                      SCENE) is False

    def test_a_url_handed_back_is_not_evidence(self):
        assert C.contributes_evidence(
            result(urls=["czechvrcasting.com/detail-1796"]), SCENE) is False

    def test_a_url_the_scene_did_not_have_is(self):
        assert C.contributes_evidence(result(urls=["elsewhere.com/x/1"]), SCENE) is True

    def test_a_code_that_is_really_a_url_is_not(self):
        # Observed: a scraper answered with the very link it had been given, in `code`.
        assert C.contributes_evidence(
            result(code="https://www.czechvrcasting.com/detail-1796-no-girls-allowed"),
            SCENE) is False

    def test_a_real_code_is(self):
        assert C.contributes_evidence(result(code="211"), SCENE) is True

    def test_a_scraper_naming_itself_as_the_studio_is_not(self):
        # Observed: Mature.nl answered with studio "Mature.nl" and nothing else.
        assert C.contributes_evidence(
            result(scraper="Mature.nl", studio="Mature.nl"), SCENE) is False

    def test_a_different_studio_is(self):
        assert C.contributes_evidence(
            result(scraper="Mature.nl", studio="Czech VR Casting"), SCENE) is True

    def test_dates_people_and_synopses_are(self):
        assert C.contributes_evidence(result(date="2021-11-08"), SCENE) is True
        assert C.contributes_evidence(result(performers=["Maya B"]), SCENE) is True
        assert C.contributes_evidence(result(details="a synopsis"), SCENE) is True


class TestSourceIdentity:
    def test_two_scrapers_for_one_site_are_one_source(self):
        hosts = {"AlphaScraper": {"example.com"}, "BetaScraper": {"example.com"}}
        left = result(scraper="AlphaScraper")
        right = result(scraper="BetaScraper")
        assert C.source_key(left, hosts) == C.source_key(right, hosts)

    def test_two_stash_boxes_are_two_sources(self):
        left = result(scraper="https://stashdb.org/graphql", method=G.M_STASHBOX_FP)
        right = result(scraper="https://theporndb.net/graphql", method=G.M_STASHBOX_FP)
        assert C.source_key(left) != C.source_key(right)

    def test_a_source_is_not_identified_by_the_url_it_returned(self):
        # Several sources legitimately report the same site's link; keying on that
        # would collapse independent databases into one witness.
        hosts = {"CzechVR": {"czechvrcasting.com", "czechvrnetwork.com"}}
        box = result(scraper="https://stashdb.org/graphql", method=G.M_STASHBOX_FP,
                     urls=["czechvrcasting.com/detail-1796"])
        site = result(scraper="CzechVR", method=G.M_URL,
                      urls=["czechvrcasting.com/detail-1796"])
        assert C.source_key(box, hosts) != C.source_key(site, hosts)

    def test_a_multi_site_scraper_is_keyed_by_itself(self):
        hosts = {"Aggregator": {"one.com", "two.com"}}
        assert C.source_key(result(scraper="Aggregator"), hosts) == "scraper:Aggregator"

    def test_an_unattributed_url_scrape_falls_back_to_its_host(self):
        row = result(scraper=None, method=G.M_URL, target="https://site.com/a/1")
        row["scraper_id"] = row["scraper_name"] = None
        assert C.source_key(row) == "url:site.com"


class TestWitnesses:
    def test_everything_rereading_a_known_url_is_one_witness(self):
        # Several scrapers answer a fragment scrape by fetching whichever URL is in the
        # fragment. Correct or not, that is one piece of evidence, not five.
        rows = [result(id=n, scraper="S%d" % n, urls=["czechvrcasting.com/detail-1796"])
                for n in range(4)]
        assert len({C.witness_key(one, SCENE) for one in rows}) == 1

    def test_a_fingerprint_match_is_its_own_witness_whatever_url_it_printed(self):
        box = result(scraper="https://stashdb.org/graphql", method=G.M_STASHBOX_FP,
                     urls=["czechvrcasting.com/detail-1796"])
        reread = result(scraper="Other", urls=["czechvrcasting.com/detail-1796"])
        assert C.witness_key(box, SCENE) != C.witness_key(reread, SCENE)

    def test_a_source_bringing_a_new_url_is_its_own_witness(self):
        one = result(scraper="A", urls=["czechvrcasting.com/detail-1796"])
        two = result(scraper="B", urls=["forum.example.com/thread/1"])
        assert C.witness_key(one, SCENE) != C.witness_key(two, SCENE)


class TestGrouping:
    def test_a_shared_url_groups(self):
        rows = [result(id=1, title="One", urls=["site.com/a"]),
                result(id=2, title="Totally Different", urls=["site.com/a"])]
        assert len(C.group(rows)) == 1

    def test_a_close_title_groups(self):
        rows = [result(id=1, title="No Girls Allowed", date="2021-11-08"),
                result(id=2, title="211 - No Girls Allowed", date="2021-11-08")]
        assert len(C.group(rows)) == 1

    def test_a_similar_but_different_title_does_not(self):
        # "No Girls Allowed" and "No dirty sluts allowed" share words and nothing else.
        rows = [result(id=1, title="No Girls Allowed"),
                result(id=2, title="No dirty sluts allowed")]
        assert len(C.group(rows)) == 2

    def test_contradicting_dates_keep_results_apart(self):
        rows = [result(id=1, title="No Girls Allowed", date="2021-11-08"),
                result(id=2, title="No Girls Allowed", date="2019-05-04")]
        assert len(C.group(rows)) == 2

    def test_grouping_is_transitive_through_a_chain(self):
        rows = [result(id=1, title="A Scene", urls=["site.com/a"]),
                result(id=2, title="A Scene", urls=["other.com/b"]),
                result(id=3, title="Nothing Alike", urls=["other.com/b"])]
        assert len(C.group(rows)) == 1


class TestTrust:
    def test_a_fingerprint_match_alone_is_enough(self):
        agreed = C.best([result(scraper="https://stashdb.org/graphql",
                                method=G.M_STASHBOX_FP, title="No Girls Allowed",
                                date="2021-11-08")], SCENE)
        assert agreed["reason"] == C.BY_FINGERPRINT

    def test_the_sites_own_page_for_a_known_url_is_enough(self):
        agreed = C.best([result(scraper="CzechVR", method=G.M_URL,
                                target="https://czechvrcasting.com/detail-1796",
                                title="No Girls Allowed", date="2021-11-08",
                                urls=["czechvrcasting.com/detail-1796"])], SCENE)
        assert agreed["reason"] == C.BY_SCENE_URL

    def test_two_independent_sources_are_enough(self):
        hosts = {"A": {"a.com"}, "B": {"b.com"}}
        agreed = C.best([
            result(id=1, scraper="A", title="A Scene", date="2020-01-01",
                   urls=["a.com/1"]),
            result(id=2, scraper="B", title="A Scene", date="2020-01-01",
                   urls=["b.com/1"]),
        ], SCENE, scraper_hosts=hosts)
        assert agreed["reason"] == C.BY_AGREEMENT
        assert agreed["independent_sources"] == 2

    def test_one_uncorroborated_source_is_not(self):
        # The whole point: 18 of 21 "matches" on the test scene looked like this.
        assert C.best([result(scraper="Whoever", title="Some Scene",
                              date="2019-01-01", urls=["whoever.com/1"])], SCENE) is None

    def test_junk_is_left_out_of_the_answer(self):
        hosts = {"CzechVR": {"czechvrcasting.com"}, "WhatIsMyIP": {"whatismyip.com"},
                 "Tube8Vip": {"tube8vip.com"}}
        rows = [
            result(id=1, scraper="https://stashdb.org/graphql", method=G.M_STASHBOX_FP,
                   title="No Girls Allowed", date="2021-11-08",
                   urls=["czechvrcasting.com/detail-1796"]),
            result(id=2, scraper="CzechVR", method=G.M_URL,
                   title="No Girls Allowed", date="2021-11-08",
                   urls=["czechvrcasting.com/detail-1796"]),
            # Real answers from the real scan.
            result(id=3, scraper="WhatIsMyIP", title="178.212.196.25"),
            result(id=4, scraper="Tube8Vip", title="No dirty sluts allowed",
                   date="2012-05-29", urls=["tube8vip.com/9"]),
        ]
        agreed = C.best(rows, SCENE, scraper_hosts=hosts)
        assert agreed["fields"]["title"]["value"] == "No Girls Allowed"
        assert 3 not in agreed["result_ids"]
        assert 4 not in agreed["result_ids"]
        assert agreed["discarded_groups"] >= 1

    def test_nothing_at_all_is_none(self):
        assert C.best([], SCENE) is None
        assert C.best([result(title=None)], SCENE) is None


class TestVoting:
    def test_the_majority_value_wins(self):
        rows = [result(id=1, scraper="A", title="Right", date="2020-01-01",
                       urls=["a.com/1"]),
                result(id=2, scraper="B", title="Right", date="2020-01-01",
                       urls=["b.com/1"]),
                result(id=3, scraper="C", title="Wrong", date="2020-01-01",
                       urls=["a.com/1"])]
        agreed = C.best(rows, SCENE, scraper_hosts={"A": {"a.com"}, "B": {"b.com"},
                                                     "C": {"c.com"}})
        assert agreed["fields"]["title"]["value"] == "Right"
        assert agreed["fields"]["title"]["agreement"] == 2

    def test_a_tie_prefers_the_better_provenance_then_the_shorter_value(self):
        # Observed: two sources offered code "211", two offered the page's whole HTML
        # title. Counting votes alone picked whichever happened to come first.
        rows = [
            result(id=1, scraper="https://stashdb.org/graphql", method=G.M_STASHBOX_FP,
                   title="S", date="2020-01-01", code="211"),
            result(id=2, scraper="Site", method=G.M_FRAGMENT_SCENE, title="S",
                   date="2020-01-01",
                   code="Czech VR Casting 211 No Girls Allowed - Porn Videos"),
        ]
        agreed = C.best(rows, SCENE)
        assert agreed["fields"]["code"]["value"] == "211"

    def test_alternatives_are_reported_not_hidden(self):
        rows = [result(id=1, scraper="A", title="One", date="2020-01-01",
                       urls=["a.com/1"]),
                result(id=2, scraper="B", title="One", date="2020-01-01",
                       urls=["b.com/1"]),
                result(id=3, scraper="C", title="Two", date="2020-01-01",
                       urls=["a.com/1"])]
        agreed = C.best(rows, SCENE, scraper_hosts={"A": {"a.com"}, "B": {"b.com"},
                                                     "C": {"c.com"}})
        assert agreed["fields"]["title"]["alternatives"][0]["value"] == "Two"

    def test_set_fields_are_merged_with_their_sources(self):
        rows = [result(id=1, scraper="A", title="S", date="2020-01-01",
                       urls=["a.com/1"], performers=["Alice"], tags=["X"]),
                result(id=2, scraper="B", title="S", date="2020-01-01",
                       urls=["b.com/1"], performers=["Alice", "Bob"], tags=["Y"])]
        agreed = C.best(rows, SCENE, scraper_hosts={"A": {"a.com"}, "B": {"b.com"}})
        names = {one["name"] for one in agreed["fields"]["performers"]}
        assert names == {"Alice", "Bob"}
        alice = [one for one in agreed["fields"]["performers"]
                 if one["name"] == "Alice"][0]
        assert len(alice["sources"]) == 2


class TestScrapedSceneOutput:
    def _agreed(self):
        rows = [result(id=1, scraper="https://stashdb.org/graphql",
                       method=G.M_STASHBOX_FP, title="A Scene", date="2020-01-01",
                       code="C1", details="synopsis", studio="A Studio",
                       performers=["Alice"], tags=["Tag"], urls=["a.com/1"])]
        return C.best(rows, SCENE)

    def test_the_payload_uses_only_fields_stash_defines(self):
        payload = C.to_scraped_scene(self._agreed())
        allowed = {"title", "code", "details", "director", "date", "url", "urls",
                   "image", "studio", "tags", "performers"}
        assert set(payload) <= allowed

    def test_entities_are_name_objects_as_stash_expects(self):
        payload = C.to_scraped_scene(self._agreed())
        assert payload["studio"] == {"name": "A Studio"}
        assert payload["performers"] == [{"name": "Alice"}]
        assert payload["tags"] == [{"name": "Tag"}]

    def test_both_url_spellings_are_set(self):
        # Stash still reads the deprecated singular field in places.
        payload = C.to_scraped_scene(self._agreed())
        assert payload["url"] == payload["urls"][0]

    def test_an_image_is_included_only_when_supplied(self):
        assert "image" not in C.to_scraped_scene(self._agreed())
        with_image = C.to_scraped_scene(self._agreed(), "data:image/jpeg;base64,AAAA")
        assert with_image["image"].startswith("data:image/jpeg")

    def test_no_consensus_produces_no_payload(self):
        assert C.to_scraped_scene(None) is None
