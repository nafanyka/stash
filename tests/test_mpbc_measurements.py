"""My Performer Body Calculator: reading measurements, and classifying what they describe.

The parser is the reason this fork exists, so it gets tested against the formats that
broke it and the formats that must not break. Both modules under test import nothing
from stashapi, which is what makes them testable at all.
"""

from __future__ import annotations

import os
import sys

import pytest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "plugins", "MyPerformerBodyCalculator")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import body_tags  # noqa: E402
import measurements  # noqa: E402


class Body:
    """Just enough of a performer for `calculate_shape`, which reads three attributes."""

    def __init__(self, bust, waist, hips):
        self.bust, self.waist, self.hips = bust, waist, hips


def shapes_for(bust, waist, hips):
    return body_tags.calculate_shape(Body(bust, waist, hips))


class TestMetricBustIsNotABraBand:
    """The bug this fork was made for.

    `90G-60-80` is the Japanese B-W-H convention: 90 is the bust circumference in
    centimetres and G is a cup size. Read as a bra band - which is what `32D-28-34`
    means, in the same characters - it produced a bust ten inches too large.
    """

    def test_the_leading_number_is_the_bust(self):
        m = measurements.parse("90G-60-80")
        assert m.measurement_type == measurements.METRIC_BUST
        assert m.first_value_is_bust is True
        assert m.first_value == 90
        assert m.cup == "G"

    def test_the_bust_is_the_leading_number_converted_and_nothing_else(self):
        m = measurements.parse("90G-60-80")
        assert m.bust == pytest.approx(35.43, abs=0.01)
        assert m.waist == pytest.approx(23.62, abs=0.01)
        assert m.hips == pytest.approx(31.50, abs=0.01)

    def test_the_cup_is_never_added_to_a_bust(self):
        # The old result: 90/2.54 = 35.43, + 7 for G = 42.43.
        m = measurements.parse("90G-60-80")
        assert m.bust < 36
        assert m.bust != pytest.approx(42.43, abs=0.01)

    def test_it_now_classifies(self):
        m = measurements.parse("90G-60-80")
        assert shapes_for(m.bust, m.waist, m.hips) == [body_tags.BodyShape.TOP_HOURGLASS]

    def test_the_old_reading_classified_as_nothing(self):
        # Which is where "could not classify bodyshape bust=42 waist=24 hips=31" came
        # from: not a hole in the classifier, a wrong bust handed to it.
        assert shapes_for(42.43, 23.62, 31.50) == []


class TestTheReportedFailure:
    """`could not parse measurements: '88(E)-58-89'` - the cup was in brackets."""

    def test_brackets_parse(self):
        m = measurements.parse("88(E)-58-89")
        assert m.ok
        assert m.cup == "E"
        assert m.bust == pytest.approx(34.65, abs=0.01)
        assert m.waist == pytest.approx(22.83, abs=0.01)
        assert m.hips == pytest.approx(35.04, abs=0.01)

    @pytest.mark.parametrize("raw", [
        "88(E)-58-89",
        "88E-58-89",
        "88 (E) - 58 - 89",
        "88e-58-89",
        "88 E - 58 - 89",
    ])
    def test_every_spelling_is_the_same_measurement(self, raw):
        m = measurements.parse(raw)
        base = measurements.parse("88E-58-89")
        assert (m.cup, m.measurement_type) == ("E", measurements.METRIC_BUST)
        assert m.bust == pytest.approx(base.bust)
        assert m.waist == pytest.approx(base.waist)
        assert m.hips == pytest.approx(base.hips)

    def test_the_cup_is_normalised_to_upper_case(self):
        assert measurements.parse("88e-58-89").cup == "E"


class TestFormatsThatMustNotBreak:
    """Everything the original understood, still understood the same way."""

    def test_imperial_bra_size_with_waist_and_hips(self):
        m = measurements.parse("32D-28-34")
        assert m.measurement_type == measurements.IMPERIAL_BAND
        assert m.first_value_is_bust is False
        assert m.band == 32
        # bust = band + cup difference, exactly as the original had it
        assert m.bust == 36
        assert (m.waist, m.hips) == (28, 34)

    def test_the_cup_may_come_first(self):
        # Not the whole record: `raw`, `text` and `pattern` say how it was written and
        # are supposed to differ. Everything about the body must not.
        def meaning(m):
            return (m.measurement_type, m.first_value_is_bust, m.cup,
                    m.band, m.bust, m.waist, m.hips)

        assert meaning(measurements.parse("D32-28-34")) == \
            meaning(measurements.parse("32D-28-34"))

    def test_three_numbers_with_no_cup_is_a_bust(self):
        m = measurements.parse("36-28-34")
        assert m.measurement_type == measurements.IMPERIAL_BUST
        assert m.first_value_is_bust is True
        assert (m.bust, m.waist, m.hips) == (36, 28, 34)

    def test_a_bra_size_on_its_own(self):
        m = measurements.parse("32D")
        assert m.measurement_type == measurements.IMPERIAL_BAND
        assert m.band == 32
        assert m.bust == 36
        assert m.waist is None and m.hips is None

    def test_a_bra_size_with_its_metric_equivalent_in_brackets(self):
        # "32D (81D)" - the bracketed half is an alternate spelling of the same size and
        # the original read only the first, which is still the right answer.
        m = measurements.parse("32D (81D)")
        assert (m.band, m.cup) == (32, "D")

    def test_slash_separators(self):
        m = measurements.parse("86/64/89")
        assert m.units == measurements.METRIC
        assert m.bust == pytest.approx(33.86, abs=0.01)

    def test_waist_and_hips_only(self):
        m = measurements.parse("NoneNone-23-35")
        assert m.measurement_type == measurements.WAIST_HIPS
        assert (m.waist, m.hips) == (23, 35)
        assert m.bust is None

    def test_multi_letter_cups(self):
        m = measurements.parse("DDD38-None-None")
        assert (m.band, m.cup) == (38, "DDD")

    @pytest.mark.parametrize("raw, error", [
        ("", measurements.MISSING),
        (None, measurements.MISSING),
        ("   ", measurements.MISSING),
        ("37-None-None", measurements.UNSUPPORTED),
        ("no idea", measurements.UNSUPPORTED),
    ])
    def test_what_cannot_be_read_says_so_rather_than_raising(self, raw, error):
        m = measurements.parse(raw)
        assert m.error == error
        assert not m.ok


class TestUnitsAndSemantics:
    def test_metric_is_decided_by_the_whole_set_not_one_number(self):
        # 90 alone could be either; 90-60-80 could not.
        assert measurements.parse("90G-60-80").units == measurements.METRIC
        assert measurements.parse("36-28-34").units == measurements.IMPERIAL

    def test_a_set_that_does_not_agree_is_left_in_inches(self):
        # One value over the threshold and two under it is a typo, not a unit system.
        # The original converted nothing in that case either.
        m = measurements.parse("90-28-34")
        assert m.units == measurements.IMPERIAL
        assert m.bust == 90

    def test_a_lone_metric_bra_size_is_a_band(self):
        """With no waist and hips there is no B-W-H triple for the number to belong to,
        and a number written with a cup letter and nothing else is a bra size."""
        m = measurements.parse("75E")
        assert m.measurement_type == measurements.METRIC_BAND
        assert m.first_value_is_bust is False
        assert m.band == pytest.approx(29.53, abs=0.01)

    def test_a_metric_bust_gets_an_estimated_band_so_breast_size_still_works(self):
        # BreastSize and the BMI breast-weight correction both need a band, and a
        # B-W-H triple does not state one. Inverting the cup rule gives it.
        m = measurements.parse("90G-60-80")
        assert m.band_estimated is True
        assert m.band == pytest.approx(35.43 - 7, abs=0.01)
        assert m.breast_volume == pytest.approx((35.43 - 7) / 2 + 7, abs=0.01)

    def test_an_imperial_band_is_not_estimated(self):
        assert measurements.parse("32D-28-34").band_estimated is False

    def test_an_unknown_cup_is_reported_not_raised(self):
        m = measurements.parse("32ZZZ-28-34")
        assert m.ok
        assert m.cup_known is False
        assert m.notes


class TestTheReportedClassificationFailures:
    """The nine `could not classify` warnings, and where they came from.

    Each is a bust that no body has with those hips - the signature of a metric bust
    that had a cup difference added to it. The test reconstructs the source measurement
    each one implies and checks that reading it correctly produces something sane.
    """

    REPORTED = [
        (42, 24, 31), (44, 23, 33), (49, 25, 39), (48, 25, 36), (47, 26, 37),
        (55, 24, 37), (50, 23, 33), (50, 24, 37), (47, 24, 36),
    ]

    @pytest.mark.parametrize("bust, waist, hips", REPORTED)
    def test_as_reported_they_classify_as_nothing(self, bust, waist, hips):
        assert shapes_for(bust, waist, hips) == []

    @pytest.mark.parametrize("bust, waist, hips", REPORTED)
    def test_all_of_them_are_in_the_one_known_gap(self, bust, waist, hips):
        region = body_tags.uncovered_region(bust, waist, hips)
        assert region == body_tags.UNCOVERED_REGIONS[0]

    CUPS = [("D", 4), ("E", 5), ("F", 6), ("G", 7), ("H", 8),
            ("I", 9), ("J", 10), ("K", 11), ("L", 12)]

    @pytest.mark.parametrize("bust, waist, hips", REPORTED)
    def test_read_as_metric_bust_and_cup_they_classify(self, bust, waist, hips):
        """Rebuild the string that produced each warning and parse it properly.

        The reported bust is `bust_cm / 2.54 + cup_difference`, so for any given cup the
        source string is recoverable. Every one of the nine lands in a real category
        again once the cup stops being added to the bust.

        Not for every cup, and that is the point rather than a weakness: the larger the
        cup, the more of the reported bust was invented, so the two most extreme reports
        - a bust of 50 and of 55 inches - only come back to a plausible body if the cup
        was large, which is exactly what a report that extreme implies.
        """
        classifies = []
        for cup, difference in self.CUPS:
            bust_cm = round((bust - difference) * 2.54)
            raw = f"{bust_cm}{cup}-{round(waist * 2.54)}-{round(hips * 2.54)}"
            m = measurements.parse(raw)
            assert m.measurement_type == measurements.METRIC_BUST, raw
            # Correcting always makes the bust smaller, by exactly the cup difference.
            assert m.bust == pytest.approx(bust_cm / 2.54, abs=0.01)
            assert m.bust < bust
            if shapes_for(m.bust, m.waist, m.hips):
                classifies.append(cup)
        assert classifies, f"bust={bust} waist={waist} hips={hips} classifies for no cup"


class TestTheClassifierItself:
    """What the shape rules do and do not cover, checked rather than assumed."""

    def test_the_categories_are_the_originals(self):
        assert [s.name for s in body_tags.BodyShape] == [
            "HOURGLASS", "BOTTOM_HOURGLASS", "TOP_HOURGLASS", "SPOON", "TRIANGLE",
            "INVERTED_TRIANGLE", "RECTANGLE", "DIAMOND", "OVAL",
        ]

    def test_the_spoon_ratio_boundary_is_covered(self):
        """FFIT gives spoon as ratio >= 1.193 and bottom hourglass as < 1.193; the
        original transcribed spoon as a strict >, leaving the ratio itself in neither."""
        waist = 47.0
        hips = waist * 1.193
        bust = hips - 6.0
        assert hips / waist == pytest.approx(1.193)
        assert shapes_for(bust, waist, hips) == [body_tags.BodyShape.SPOON]

    def test_every_gap_found_by_a_sweep_is_a_known_one(self):
        """Nothing falls through that is not one of the two documented regions.

        A sweep rather than examples: the rules are a set of overlapping inequalities,
        and the only honest way to know what they miss is to ask all of them.
        """
        unexplained = []
        for bust in range(28, 61):
            for waist in range(20, 51):
                for hips in range(28, 61):
                    if shapes_for(bust, waist, hips):
                        continue
                    if body_tags.uncovered_region(bust, waist, hips) is None:
                        unexplained.append((bust, waist, hips))
        assert unexplained == []

    def test_both_documented_gaps_are_real(self):
        # Region A: a bust far past the hips with a defined waist.
        assert shapes_for(45, 25, 32) == []
        assert body_tags.uncovered_region(45, 25, 32) == body_tags.UNCOVERED_REGIONS[0]
        # Region B: the same in reverse, on a frame wide enough to keep the ratio low.
        assert shapes_for(52, 52, 62) == []
        assert body_tags.uncovered_region(52, 52, 62) == body_tags.UNCOVERED_REGIONS[1]

    def test_ordinary_proportions_still_land_where_they_did(self):
        assert body_tags.BodyShape.HOURGLASS in shapes_for(36, 26, 36)
        assert body_tags.BodyShape.TOP_HOURGLASS in shapes_for(38, 27, 34)
        assert body_tags.BodyShape.SPOON in shapes_for(34, 30, 40)
        assert body_tags.BodyShape.TRIANGLE in shapes_for(34, 34, 38)
        assert body_tags.BodyShape.RECTANGLE in shapes_for(35, 30, 35)

    def test_the_failure_report_shows_the_differences_the_rules_are_written_in(self):
        report = body_tags.shape_diagnostics(35.4, 23.6, 31.5)
        assert "bust=35.4 waist=23.6 hips=31.5" in report
        assert "bust-waist=11.8" in report
        assert "hips-waist=7.9" in report
        assert "bust-hips=3.9" in report

    def test_it_names_the_gap_when_it_knows_it(self):
        assert "no category covers" in body_tags.shape_diagnostics(45, 25, 32)
        assert "no category covers" not in body_tags.shape_diagnostics(36, 26, 36)
