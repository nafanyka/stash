"""What reaches `calculate_shape`, measured rather than reasoned about.

The parser is tested next door, and it is not enough: a parser that reads `90G-60-80`
correctly is worth nothing if something between it and the classifier adds the cup back
on. So these build a real `StashPerformer` - the whole object, every `set_*` step, in
order - and put a spy on `calculate_shape` to record the three numbers it is actually
handed.

`performer_calculator` does `from body_tags import *`, so the name the pipeline calls is
`performer_calculator.calculate_shape`. Patching it there rather than in `body_tags` is
deliberate: it is the call the pipeline really makes.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import types

import pytest

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "plugins", "MyPerformerBodyCalculator")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import body_tags  # noqa: E402

# `performer_calculator` needs a `config` and a stashapi logger at import time. Standing
# in for both is cheaper than a Stash, and routing the log into a buffer means the tests
# can also read what the plugin says.
LOG = io.StringIO()


class _BufferHandler(logging.StreamHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(LOG)


if "stashapi.log" not in sys.modules:
    stashapi = types.ModuleType("stashapi")
    log_module = types.ModuleType("stashapi.log")
    log_module.StashLogHandler = _BufferHandler
    sys.modules.setdefault("stashapi", stashapi)
    sys.modules["stashapi.log"] = log_module

if "config" not in sys.modules:
    config = types.ModuleType("config")
    config.log_level = "DEBUG"
    config.TAGS_TO_USE = tuple(body_tags.get_tag_classes())
    sys.modules["config"] = config

import performer_calculator as PC  # noqa: E402

# The import above wires the buffer handler onto the root logger; make sure debug lines
# actually reach it whatever else has configured logging in this session.
logging.getLogger().setLevel(logging.DEBUG)
if not any(isinstance(h, _BufferHandler) for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(_BufferHandler())


def build(measurements, weight=55, height_cm=165, ethnicity="Asian", gender="FEMALE"):
    """A real performer, and what `calculate_shape` was handed for it."""
    return PC.StashPerformer({
        "id": "1", "name": "T", "measurements": measurements,
        "weight": weight, "height_cm": height_cm,
        "ethnicity": ethnicity, "gender": gender,
    })


@pytest.fixture
def at_classification(monkeypatch):
    """Records the bust/waist/hips `calculate_shape` receives, then lets it run."""
    seen = {}
    original = PC.calculate_shape

    def spy(performer):
        seen["bust"] = performer.bust
        seen["waist"] = performer.waist
        seen["hips"] = performer.hips
        seen["cup"] = performer.cupsize
        seen["type"] = performer.measurement_type
        seen["shapes"] = original(performer)
        return seen["shapes"]

    monkeypatch.setattr(PC, "calculate_shape", spy)
    return seen


class TestMetricBustReachesTheClassifierUntouched:
    """`90G-60-80`: 90cm bust, cup G. The cup must not move the bust, at any stage."""

    def test_the_values_at_the_moment_of_classification(self, at_classification):
        performer = build("90G-60-80")

        assert at_classification["cup"] == "G"
        assert at_classification["type"] == "metric_bust"
        assert at_classification["bust"] == pytest.approx(35.43, abs=0.01)
        assert at_classification["waist"] == pytest.approx(23.62, abs=0.01)
        assert at_classification["hips"] == pytest.approx(31.50, abs=0.01)
        # and the performer still says the same afterwards
        assert performer.bust == pytest.approx(35.43, abs=0.01)

    def test_the_bust_is_the_raw_value_converted_and_nothing_more(self, at_classification):
        build("90G-60-80")
        assert at_classification["bust"] == pytest.approx(90 / 2.54, abs=1e-9)

    def test_no_cup_difference_was_added_anywhere(self, at_classification):
        build("90G-60-80")
        # 90/2.54 + 7 = 42.43 is the number the original produced.
        assert at_classification["bust"] != pytest.approx(42.43, abs=0.01)
        assert at_classification["bust"] < 36

    def test_it_classifies(self, at_classification):
        build("90G-60-80")
        assert at_classification["shapes"] == [body_tags.BodyShape.TOP_HOURGLASS]

    def test_the_second_reported_case(self, at_classification):
        build("88(E)-58-89")
        assert at_classification["cup"] == "E"
        assert at_classification["bust"] == pytest.approx(34.65, abs=0.01)
        assert at_classification["waist"] == pytest.approx(22.83, abs=0.01)
        assert at_classification["hips"] == pytest.approx(35.04, abs=0.01)

    @pytest.mark.parametrize("raw, cm", [
        ("90G-60-80", 90), ("88E-58-89", 88), ("88(E)-58-89", 88),
        ("90(G)-60-80", 90), ("88 (E) - 58 - 89", 88), ("100J-60-90", 100),
        ("120J-60-95", 120), ("95H-58-88", 95),
    ])
    def test_every_metric_bust_arrives_as_itself(self, at_classification, raw, cm):
        build(raw)
        assert at_classification["type"] == "metric_bust"
        assert at_classification["bust"] == pytest.approx(cm / 2.54, abs=1e-9)


class TestImperialTriplesAreBustsToo:
    """A triple is a bust, waist and hips measurement whichever units it is in.

    These three are the ones that produced the reported busts of 50 and 55: the leading
    number was read as a band and the cup added to it.
    """

    @pytest.mark.parametrize("raw, bust, cup, waist, hips", [
        ("40J-24-37", 40, "J", 24, 37),
        ("44DDD-23-33", 44, "DDD", 23, 33),
        ("48DDDD-24-37", 48, "DDDD", 24, 37),
    ])
    def test_the_values_at_the_moment_of_classification(self, at_classification, raw,
                                                        bust, cup, waist, hips):
        build(raw)
        assert at_classification["type"] == "imperial_bust"
        assert at_classification["bust"] == bust
        assert at_classification["cup"] == cup
        assert at_classification["waist"] == waist
        assert at_classification["hips"] == hips

    @pytest.mark.parametrize("raw, inflated", [
        ("40J-24-37", 50), ("44DDD-23-33", 50), ("48DDDD-24-37", 55),
    ])
    def test_the_inflated_bust_never_reaches_the_classifier(self, at_classification,
                                                            raw, inflated):
        build(raw)
        assert at_classification["bust"] != inflated

    def test_the_western_bra_size_form_changed_with_them(self, at_classification):
        # Deliberate, and the one behaviour knowingly given up: `32D-28-34` really is a
        # bra size in western performer databases, and its true bust is nearer 36. A
        # triple cannot be read two ways at once, and the data that has to work is the
        # data with a 48" first number and a 24" waist.
        build("32D-28-34")
        assert at_classification["type"] == "imperial_bust"
        assert at_classification["bust"] == 32
        assert at_classification["cup"] == "D"

    def test_a_lone_bra_size_still_derives_its_bust(self):
        # The only place the band + cup formula survives.
        performer = build("32D")
        assert performer.measurement_type == "imperial_band"
        assert performer.band == 32
        assert performer.band_estimated is False
        assert performer.bust == 36

    def test_a_bust_with_no_cup_is_left_alone(self, at_classification):
        build("36-28-34")
        assert at_classification["bust"] == 36
        assert at_classification["type"] == "imperial_bust"


class TestTheRestOfThePipelineStillRuns:
    """The cup, breast size, butt and BMI tags all still come out for metric input."""

    def test_a_metric_performer_gets_the_full_set_of_tags(self):
        performer = build("90G-60-80")
        names = {str(tag) for tag in performer.tags_list}
        assert "BreastCup.G" in names
        assert any(n.startswith("BreastSize.") for n in names)
        assert any(n.startswith("ButtSize.") for n in names)
        assert any(n.startswith("BodyMassIndex.") for n in names)
        assert "BodyShape.TOP_HOURGLASS" in names

    def test_the_band_is_estimated_for_a_metric_bust_and_marked_so(self):
        performer = build("90G-60-80")
        assert performer.band_estimated is True
        assert performer.band == pytest.approx(90 / 2.54 - 7, abs=0.01)

    def test_a_null_measurements_does_not_end_the_performer(self):
        performer = build(None)
        assert performer.status == PC.STATUS_MISSING
        # height and weight still produce their tags
        assert any(str(t).startswith("BodyMassIndex.") for t in performer.tags_list)

    def test_a_null_ethnicity_does_not_end_the_performer(self):
        performer = build("90G-60-80", ethnicity=None)
        assert any(str(t).startswith("BodyMassIndex.") for t in performer.tags_list)


class TestTheReportedPerformers:
    """The eight busts from the last report, traced back to what could produce them.

    Each is checked two ways: the metric string that the *old* reading turns into that
    bust, and what the *new* reading makes of the same string.
    """

    # name, bust", waist", hips" as reported
    REPORTED = [
        ("Yuria Yoshine", 55, 24, 37),
        ("Waka Misono", 50, 23, 33),
        ("Shiori Tsukada", 50, 24, 37),
        ("Shion Utsunomiya", 50, 23, 36),
        ("Sara Saijo", 47, 24, 36),
        ("Momona Koibuchi", 54, 26, 36),
        ("Julia Boin", 50, 22, 33),
        ("Himeka Iori", 51, 22, 35),
    ]

    # The strings the diagnostic log showed, and the bust each one really states.
    ACTUAL = [
        ("Yuria Yoshine", "48DDDD-24-37", 48, 24, 37),
        ("Waka Misono", "44DDD-23-33", 44, 23, 33),
        ("Shiori Tsukada", "40J-24-37", 40, 24, 37),
    ]

    @pytest.mark.parametrize("name, raw, bust, waist, hips", ACTUAL)
    def test_the_logged_strings_reach_the_classifier_as_written(
            self, at_classification, name, raw, bust, waist, hips):
        build(raw)
        assert at_classification["bust"] == bust, name
        assert at_classification["waist"] == waist
        assert at_classification["hips"] == hips

    @pytest.mark.parametrize("name, bust, waist, hips", REPORTED)
    def test_the_reported_bust_is_not_one_this_plugin_can_produce(
            self, at_classification, name, bust, waist, hips):
        """Rebuild the source string for each plausible cup and check the bust that
        comes out is the number the string states, never the inflated one reported."""
        for cup, difference in [("F", 6), ("G", 7), ("H", 8), ("I", 9), ("J", 10)]:
            bust_cm = round((bust - difference) * 2.54)
            raw = f"{bust_cm}{cup}-{round(waist * 2.54)}-{round(hips * 2.54)}"
            build(raw)
            assert at_classification["type"] == "metric_bust"
            assert at_classification["bust"] == pytest.approx(bust_cm / 2.54, abs=1e-9)
            assert at_classification["bust"] < bust, (
                f"{name}: {raw} still reaches the classifier with an inflated bust")


class TestTheDiagnosticLogging:
    def test_the_trace_shows_the_bust_at_both_stages(self):
        LOG.truncate(0)
        LOG.seek(0)
        build("90G-60-80")
        output = LOG.getvalue()
        assert "Raw measurements: 90G-60-80" in output
        assert "type=metric_bust first_value_is_bust=True" in output
        assert "After conversion:" in output
        assert "before calculate_shape" in output

    def test_a_classification_failure_says_what_it_failed_to_classify(self):
        LOG.truncate(0)
        LOG.seek(0)
        # Region A: a real proportion no FFIT category covers.
        build("45D-25-32")
        output = LOG.getvalue()
        assert "could not classify bodyshape" in output
        assert "measurements='45D-25-32'" in output
        assert "read as imperial_bust" in output
        assert "no category covers" in output
