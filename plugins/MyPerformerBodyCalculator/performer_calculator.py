"""One performer, turned into a list of tags.

From stg-annon's Performer Body Calculator (GPL-3.0, see LICENSE). The calculations are
his and are unchanged: BMI, breast volume and cup, hip and butt size, height type, body
shape, body type. What changed:

* `parse_measurements` hands the string to `measurements.parse` instead of matching it
  here, so the meaning of the leading number is decided once, explicitly, and recorded -
  see that module for why the original got metric bust sizes wrong;
* the performer carries a `status`, so the two calculator tasks can tell "this performer
  has been dealt with, and there was nothing to calculate" from "this performer could
  not be read";
* two crashes are handled rather than raised: a null `measurements` and a null
  `ethnicity`. Both come straight out of Stash for a performer nobody has filled in, and
  both used to end the performer's processing with an AttributeError.
"""

import sys
import logging as log

import config
import measurements as measurements_module
from body_tags import *
from measurements import CM_TO_INCH  # noqa: F401  (kept exported, as the original had it)

try:
    from stashapi.log import StashLogHandler
except ModuleNotFoundError:
    print("You need to install stashapp-tools. (https://pypi.org/project/stashapp-tools/)", file=sys.stderr)
    print("If you have pip (normally installed with python), run this command in a terminal (cmd): 'pip install stashapp-tools'", file=sys.stderr)
    sys.exit()
log.basicConfig(format="%(message)s", handlers=[StashLogHandler()], level=config.log_level)

class DebugException(Exception):
    pass
class WarningException(Exception):
    pass
class ErrorException(Exception):
    pass

# How a performer came out of processing. All four mean the performer was *read* and
# dealt with; none of them is a failure. A failure is an exception, and the difference
# decides whether the performer gets a processed marker (see the entry point).
STATUS_OK = "ok"                    # measurements parsed, tags assigned
STATUS_MISSING = "missing"          # no measurements recorded in Stash
STATUS_UNPARSED = "unparsed"        # measurements present but in no known format
STATUS_NO_SHAPE = "no_shape"        # measurements fine, no body shape category matched

# `custom_fields` carries the processed marker; `tags { id }` is what makes it possible
# to report how many managed tag assignments a full update actually removed.
PERFORMER_FRAGMENT = """
id
name
measurements
weight
height_cm
ethnicity
gender
custom_fields
tags { id }
"""

class StashPerformer:

    def __init__(self, resp) -> None:

        self.__dict__.update(resp)

        self.cupsize        = None
        self.band           = None
        self.waist          = None
        self.hips           = None

        self.bust           = None
        self.bust_band_diff = None
        self.breast_volume  = None

        # What the leading number in the measurements string turned out to mean. Kept on
        # the performer so nothing downstream has to guess, and so a debug log can say.
        self.raw_measurements = None
        self.first_value = None
        self.raw_waist = None
        self.raw_hips = None
        self.measurement_type = None
        self.first_value_is_bust = None
        self.band_estimated = False

        self.bmi = 0
        self.status = STATUS_OK

        self.tags_list = []

        self.parse_measurements()
        self.calculate_bmi()
        self.set_bmi_tag()

        self.set_breast_size()
        self.set_breast_cup()

        self.set_hip_size()

        self.set_butt_size()

        self.set_height_type()

        self.match_body_shapes()
        self.set_type_descriptor()

    def parse_measurements(self):
        if self.weight:
            self.weight = float(self.weight)
        if self.height_cm:
            self.height_cm = float(self.height_cm)

        parsed = measurements_module.parse(self.measurements)
        self.raw_measurements = parsed.raw
        self.measurements = parsed.text

        if parsed.error == measurements_module.MISSING:
            self.status = STATUS_MISSING
            log.debug(f"{self}: no measurements recorded")
            return

        if parsed.error == measurements_module.UNSUPPORTED:
            self.status = STATUS_UNPARSED
            self.tags_list.append(PBCError.BAD_MEASUREMENTS)
            log.warning(f"{self}: could not parse measurements: '{parsed.text}'")
            return

        self.cupsize = parsed.cup
        self.first_value = parsed.first_value
        self.raw_waist = parsed.raw_waist
        self.raw_hips = parsed.raw_hips
        self.band = parsed.band
        self.bust = parsed.bust
        self.waist = parsed.waist
        self.hips = parsed.hips
        self.bust_band_diff = parsed.bust_band_difference
        self.breast_volume = parsed.breast_volume
        self.measurement_type = parsed.measurement_type
        self.first_value_is_bust = parsed.first_value_is_bust
        self.band_estimated = parsed.band_estimated

        for note in parsed.notes:
            log.warning(f"{self}: {note}")

        log.debug(f"{self}: {measurements_module.describe(parsed)}")
        self.log_pipeline()

    def log_pipeline(self):
        """What the string said, and what came out of it, stage by stage.

        Debug level, so it costs nothing on a normal run and can be turned on in
        config.py when a performer's numbers need explaining. Printing the same three
        numbers at two stages is the point: `first_value` is what the string said and
        `bust` is what the pipeline carries, and for a metric bust the second has to be
        the first converted and nothing more.
        """
        def inches(value):
            return "%.2f" % value if value is not None else "None"

        estimated = " (estimated from bust - cup difference)" if self.band_estimated else ""
        log.debug("\n".join([
            f"{self}: pipeline trace",
            f"Raw measurements: {self.raw_measurements}",
            "Parsed:",
            f"  first_value={self.first_value} cup={self.cupsize} "
            f"waist={self.raw_waist} hips={self.raw_hips}",
            f"  type={self.measurement_type} "
            f"first_value_is_bust={self.first_value_is_bust}",
            "After conversion:",
            f"  bust={inches(self.bust)} waist={inches(self.waist)} "
            f"hips={inches(self.hips)}",
            f"  band={inches(self.band)}{estimated}",
        ]))

    def calculate_bmi(self):
        if not self.weight or not self.height_cm:
            return
        breast_weight = approximate_breast_weight(self.bust_band_diff)
        self.bmi = (self.weight-breast_weight) / (self.height_cm/100) ** 2

    def match_body_shapes(self):
        # The same three numbers again, read off the performer at the moment they are
        # handed over. If they differ from the line above, something between the two
        # changed them - which is the whole question a metric bust raises.
        log.debug("\n".join([
            f"{self}: before calculate_shape",
            f"  bust={self.bust} waist={self.waist} hips={self.hips}",
        ]))
        self.body_shapes = calculate_shape(self)
        for body_shape in self.body_shapes:
            self.tags_list.append(body_shape)
        if not self.body_shapes:
            if not self.bust or not self.waist or not self.hips:
                log.debug(f"{self}: could not classify bodyshape, missing required measurements")
            else:
                # Every category is a rule about differences, so the differences are what
                # the warning shows. Three raw numbers would leave whoever reads it to do
                # the subtraction themselves before they could tell whether the data or
                # the rules were at fault.
                log.warning(
                    f"{self}: could not classify bodyshape:\n"
                    f"measurements={self.raw_measurements!r} "
                    f"read as {self.measurement_type}\n"
                    + shape_diagnostics(self.bust, self.waist, self.hips))
                self.status = STATUS_NO_SHAPE
                self.tags_list.append(PBCError.NO_BODYSHAPE_MATCH)

    def set_type_descriptor(self):
        descriptor = None
        if not self.bmi:
            return
        descriptor = BodyType.match_threshold(self.bmi)
        if descriptor == BodyType.FIT and HeightType.SHORT.within_threshold(self.height_cm):
            descriptor = BodyType.PETITE
        if descriptor == BodyType.AVERAGE and self.body_shapes and any(bs in self.body_shapes for bs in CURVY_SHAPES):
            descriptor = BodyType.CURVY
        if descriptor:
            self.tags_list.append(descriptor)

    def set_breast_size(self):
        if not self.breast_volume:
            return
        if breast_size := BreastSize.match_threshold(self.breast_volume):
            self.tags_list.append(breast_size)

    def set_breast_cup(self):
        if not self.cupsize:
            return
        if breast_cup := BreastCup.match_threshold(self.cupsize):
            self.tags_list.append(breast_cup)

    def set_hip_size(self):
        if not self.waist or not self.hips:
            return None
        if hip_size := HipSize.match_threshold((self.waist/self.hips)):
            self.tags_list.append(hip_size)

    def set_butt_size(self):
        if not self.hips:
            return
        if butt_size := ButtSize.match_threshold(self.hips):
            self.tags_list.append(butt_size)

    def set_height_type(self):
        # only tuned on female heights
        if not self.height_cm or self.gender != 'FEMALE':
            return
        height_type = HeightType.match_threshold(self.height_cm)
        if height_type:
            self.tags_list.append(height_type)

    def set_bmi_tag(self):
        if bmi_tag := calculate_bmi(self):
            self.tags_list.append(bmi_tag)

    def get_tag_updates(self, tag_updates={}):
        for tag_enum in self.tags_list:
            tag_updates[tag_enum].append(self.id)

    def str_details(self) -> str:
        p_str = str(self)
        if self.band and self.cupsize:
            p_str += f" {self.band:.0f}{self.cupsize}"
            if self.band_estimated:
                p_str += "(est)"
        elif self.bust:
            p_str += f" {self.bust:.0f}"
        if self.waist and self.hips:
            p_str += f"-{self.waist:.0f}-{self.hips:.0f}"
        p_str += f" {self.bmi:5.2f}bmi"
        return p_str
    def __str__(self) -> str:
        return f"{self.name} ({self.id})"
    def __repr__(self) -> str:
        return str(self)
