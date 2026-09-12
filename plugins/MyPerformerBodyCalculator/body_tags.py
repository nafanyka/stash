"""The tag vocabulary, and the rules that turn measurements into it.

Taken from stg-annon's Performer Body Calculator (GPL-3.0, see LICENSE) and kept as
close to the original as the job allows. Two things differ, both deliberate:

* the plugin's own marks - the tag alias prefix and the "managed by" line in a tag's
  description - name *this* plugin, so the two calculators can be installed side by side
  without either taking the other's tags to be its own;
* one comparison in `calculate_shape` is aligned with the reference it cites.

Everything else - every category, every threshold, every description - is the original's
and was left alone on purpose.
"""

import operator
from enum import Enum
from dataclasses import dataclass, field

# What identifies a tag as this plugin's. Every managed tag carries the prefixed alias,
# which is how the plugin finds its tags again after a user has renamed or merged them,
# and the marker line in its description, which is what `destroy_managed_tags` matches.
#
# Both differ from the original plugin's `PBC:` and `PBC Plugin`, and that is the whole
# mechanism by which the two stay out of each other's way: a tag one of them manages is
# invisible to the other's removal pass.
TAG_ALIAS_PREFIX = "MPBC:"
TAG_MANAGED_BY = "[Managed By: MPBC Plugin]"

@dataclass
class StashTagDC:
    threshold: tuple = None
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    image: str = None

    def tag_create_input(self, tag_name, alias_id):
        create_input = {"name": tag_name}
        create_input["description"] = TAG_MANAGED_BY + "\n" + self.description
        self.aliases.append(alias_id)
        create_input["aliases"] = self.aliases
        if self.image:
            create_input["image"] = self.image
        return create_input

class StashTagEnum(Enum):
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}.{self.name}"
    def __str__(self) -> str:
        return f"{self.__class__.__name__}.{self.name}"

class StashTagEnumComparable(StashTagEnum):
    def __new__(cls, *args):
        obj = object.__new__(cls)
        obj._order_ = len(cls.__members__)
        return obj

    def __gt__(self, other):
        try:
            return self._order_ > other._order_
        except:
            pass
        return NotImplemented

    def __lt__(self, other):
        try:
            return self._order_ < other._order_
        except:
            pass
        return NotImplemented
    
    def within_threshold(self, compare_value):
        if not self.value.threshold:
            return
        op, value = self.value.threshold
        if op == operator.contains:
            return op(value, compare_value)
        return op(compare_value, value)

    @classmethod
    def match_threshold(cls, compare_value):
        for enum in cls:
            if enum.within_threshold(compare_value):
                return enum
            
class PBCError(StashTagEnum):
    BAD_MEASUREMENTS = StashTagDC(description="performer measurements could not be parsed by PBC")
    NO_BODYSHAPE_MATCH = StashTagDC(description="Could not determine bodyshape from given measurements")

# body mass index determined from calculate_bmi()
class BodyMassIndex(StashTagEnum):
    SEVERELY_UNDERWEIGHT = StashTagDC(description='BMI: Severely Underweight')
    UNDERWEIGHT = StashTagDC(description='BMI: Underweight')
    HEALTHY = StashTagDC(description='BMI: Healthy')
    OVERWEIGHT = StashTagDC(description='BMI: Overweight')
    OBESE_CLASS_1 = StashTagDC(description='BMI: Obese Class 1')
    OBESE_CLASS_2 = StashTagDC(description='BMI: Obese Class 2')
    SEVERELY_OBESE = StashTagDC(description='BMI: Extremely Obese')

# Determined from Waist to Hip ratio
class HipSize(StashTagEnumComparable):
    WIDE = StashTagDC(threshold=(operator.le, 0.64))
    MEDIUM = StashTagDC(threshold=(operator.lt, 0.8))
    SLIM = StashTagDC(threshold=(operator.ge, 0.8))

class BreastCup(StashTagEnumComparable):
    AA = StashTagDC(threshold=(operator.contains, ['AA']))
    A  = StashTagDC(threshold=(operator.contains, ['A']))
    B  = StashTagDC(threshold=(operator.contains, ['B']))
    C  = StashTagDC(threshold=(operator.contains, ['C']))
    D  = StashTagDC(threshold=(operator.contains, ['D']))
    E  = StashTagDC(threshold=(operator.contains, ['E','DD']))
    F  = StashTagDC(threshold=(operator.contains, ['F','DDD','EE']))
    G  = StashTagDC(threshold=(operator.contains, ['G','DDDD']))
    H  = StashTagDC(threshold=(operator.contains, ['H','FF']))
    I  = StashTagDC(threshold=(operator.contains, ['I']))
    J  = StashTagDC(threshold=(operator.contains, ['J','GG']))
    K  = StashTagDC(threshold=(operator.contains, ['K']))
    L  = StashTagDC(threshold=(operator.contains, ['L','HH']))
    M  = StashTagDC(threshold=(operator.contains, ['M']))
    N  = StashTagDC(threshold=(operator.contains, ['N','JJ']))
    O  = StashTagDC(threshold=(operator.contains, ['O']))
    P  = StashTagDC(threshold=(operator.contains, ['P','KK']))
    Q  = StashTagDC(threshold=(operator.contains, ['Q']))
    R  = StashTagDC(threshold=(operator.contains, ['R','LL']))

# shape determined from calculate_shape()
class BodyShape(StashTagEnum):
    HOURGLASS = StashTagDC(
        "Figure: Hourglass",
        image="https://user-images.githubusercontent.com/14135675/225106804-d573bba1-7ca2-4c55-b1e1-d858c03040be.jpg",
        description='Hourglass:\nIf hips and bust are nearly equal in size with a well-defined waist that’s narrower than both.\n\nLegs and upper body are probably considered proportionate.\n\nShoulders may be slightly rounded, and most likely has a rounded buttocks.'
    )
    BOTTOM_HOURGLASS = StashTagDC(
        "Figure: Bottom Hourglass",
        image="https://user-images.githubusercontent.com/14135675/225106797-c818b1ab-d82d-4130-8184-b661c9fd7b44.jpg",
        description='Bottom hourglass:\nHas the general hourglass shape, but hip measurements are slightly larger than bust.'
    )
    TOP_HOURGLASS = StashTagDC(
        "Figure: Top Hourglass",
        image="https://user-images.githubusercontent.com/14135675/225106819-6462ac6a-8ca7-41ac-9cd5-467c9c80e7f1.jpg",
        description='Top hourglass:\nHas the general hourglass shape, but bust measurements are slightly larger than hips.'
    )
    SPOON = StashTagDC(
        "Figure: Spoon",
        image="https://user-images.githubusercontent.com/14135675/225106816-574eee46-d14c-4869-9137-6477bdedd2d5.jpg",
        description='Spoon:\nThe spoon body type is similar to the Triangle or “Pear” shape.\n\nHips are larger than bust or the rest of body and may have a “shelf”-like appearance.\n\nLikely has a defined waist. May also carry some weight in upper arms and upper thighs.'
    )
    TRIANGLE = StashTagDC(
        "Figure: Triangle",
        image="https://user-images.githubusercontent.com/14135675/225106820-6b8d71a7-3ba1-4aec-a763-a4e52e71363b.jpg",
        description='Triangle / “Pear”:\nShoulders and bust are narrower than hips.\n\nLikely to have slim arms and a fairly defined waist. waist most likely slopes out to hips.'
    )
    INVERTED_TRIANGLE = StashTagDC(
        "Figure: Inverted Triangle",
        image="https://user-images.githubusercontent.com/14135675/225106808-38ff54fa-a8cd-4f2e-b9b7-ae8ec6371357.jpg",
        description='Inverted triangle / “Apple”:\nShoulders and bust are larger than relatively narrow hips.'
    )
    RECTANGLE = StashTagDC(
        "Figure: Rectangle",
        image="https://user-images.githubusercontent.com/14135675/225106815-a4afdeda-835a-4888-b581-f382c18f3487.jpg",
        description='Rectangle / Straight / “Banana”:\nWaist measurements are about the same as hip or bust, and shoulders and hips are about the same width.'
    )
    DIAMOND = StashTagDC(
        "Figure: Diamond",
        image="https://user-images.githubusercontent.com/14135675/225106802-8cb88df8-7804-4f1f-a65c-c6e86a64698d.jpg",
        description='Diamond:\nBroader hips than shoulders, a narrow bust, and a fuller waistline.\n\nMay carry a little more weight in upper legs. May also have slender arms.'
    )
    OVAL = StashTagDC(
        "Figure: Oval",
        image="https://user-images.githubusercontent.com/14135675/225106814-e80f5cfe-c467-4b91-bd27-d9360dbb0894.jpg",
        description='Round / Oval:\nBust is larger than the rest of body, hips are narrow, and waist is fuller.'
    )

CURVY_SHAPES = [BodyShape.TOP_HOURGLASS, BodyShape.BOTTOM_HOURGLASS, BodyShape.HOURGLASS]
class BodyType(StashTagEnumComparable):
    # threshold based off of performer.bmi
    PETITE  = StashTagDC(threshold=None)
    CURVY   = StashTagDC(threshold=None)
    SKINNY  = StashTagDC(threshold=(operator.lt, 18))
    FIT     = StashTagDC(threshold=(operator.lt, 23))
    AVERAGE = StashTagDC(threshold=(operator.lt, 29))
    BBW     = StashTagDC(threshold=(operator.lt, 55))
    SSBBW   = StashTagDC(threshold=(operator.ge, 55))

# https://ourworldindata.org/human-height
# current implementation uses global average
# TODO - can further segment by country/continent factor, see:
# https://www.worlddata.info/average-bodyheight.php
# height means by ethnicity (US)
# https://thebonescience.com/blogs/journal/average-height-around-the-world
# height standard deviation (global)
# https://www.nber.org/system/files/working_papers/h0108/h0108.pdf
F_HEIGHT_MEAN = 164.7
F_HEIGHT_SD = 7.07
class HeightType(StashTagEnumComparable):
    # threshold based off of performer.height_cm
    TINY   = StashTagDC(threshold=(operator.le, F_HEIGHT_MEAN - F_HEIGHT_SD * 2))
    SHORT   = StashTagDC(threshold=(operator.lt, F_HEIGHT_MEAN - F_HEIGHT_SD))
    AVERAGE  = StashTagDC(threshold=(operator.lt, F_HEIGHT_MEAN + F_HEIGHT_SD))
    TALL    = StashTagDC(threshold=(operator.lt, F_HEIGHT_MEAN + F_HEIGHT_SD))
    VERY_TALL    = StashTagDC(threshold=(operator.ge, F_HEIGHT_MEAN + F_HEIGHT_SD * 2))

class BreastSize(StashTagEnumComparable):
    # threshold based off of performer.breast_volume
    TINY    = StashTagDC(threshold=(operator.lt, 16))
    SMALL   = StashTagDC(threshold=(operator.lt, 19))
    MEDIUM  = StashTagDC(threshold=(operator.lt, 23))
    LARGE   = StashTagDC(threshold=(operator.lt, 27))
    HUGE    = StashTagDC(threshold=(operator.lt, 31))
    MASSIVE = StashTagDC(threshold=(operator.ge, 31))

class ButtSize(StashTagEnumComparable):
    # threshold based off of performer.hips 
    TINY    = StashTagDC(threshold=(operator.lt, 28))
    SMALL   = StashTagDC(threshold=(operator.lt, 32))
    MEDIUM  = StashTagDC(threshold=(operator.lt, 40))
    LARGE   = StashTagDC(threshold=(operator.lt, 44))
    HUGE    = StashTagDC(threshold=(operator.lt, 48))
    MASSIVE = StashTagDC(threshold=(operator.ge, 48))
         
# https://www.ncbi.nlm.nih.gov/books/NBK541070/
def calculate_bmi(performer):
    if performer.bmi < 1:
        return None
    # `or ""` because Stash returns null for a performer whose ethnicity nobody
    # filled in, and the original raised AttributeError on it - ending that
    # performer's processing before any tag was assigned.
    elif (performer.ethnicity or "").title() == 'Asian':
        if performer.bmi < 16.5:
            return BodyMassIndex.SEVERELY_UNDERWEIGHT
        elif performer.bmi < 18.5:
            return BodyMassIndex.UNDERWEIGHT
        elif performer.bmi < 23:
            return BodyMassIndex.HEALTHY
        elif performer.bmi < 25:
            return BodyMassIndex.OVERWEIGHT
        elif performer.bmi < 30:
            return BodyMassIndex.OBESE_CLASS_1
        elif performer.bmi < 35:
            return BodyMassIndex.OBESE_CLASS_2
        else:
            return BodyMassIndex.SEVERELY_OBESE
    else:
        if performer.bmi < 16.5:
            return BodyMassIndex.SEVERELY_UNDERWEIGHT
        elif performer.bmi < 18.5:
            return BodyMassIndex.UNDERWEIGHT
        elif performer.bmi < 25:
            return BodyMassIndex.HEALTHY
        elif performer.bmi < 30:
            return BodyMassIndex.OVERWEIGHT
        elif performer.bmi < 35:
            return BodyMassIndex.OBESE_CLASS_1
        elif performer.bmi < 40:
            return BodyMassIndex.OBESE_CLASS_2
        else:
            return BodyMassIndex.SEVERELY_OBESE


# Shape Calculation References:
#  https://en.wikipedia.org/wiki/Female_body_shape#FFIT_for_Apparel_measurements
#  https://scholarsbank.uoregon.edu/xmlui/bitstream/handle/1794/25863/2022sokolowski.pdf?sequence=1&isAllowed=y
def calculate_shape(performer):

    shapes = []
    if not performer.bust or not performer.waist or not performer.hips:
        return shapes

    bust_hips = performer.bust - performer.hips
    bust_waist = performer.bust - performer.waist
    hips_bust = performer.hips - performer.bust
    hips_waist = performer.hips - performer.waist
    
    hips_over_waist = performer.hips/performer.waist # highip/waist

    # bust to waist ratio close to 1 with a small waist
    if bust_hips <= 1 and hips_bust < 3.6 and (9 <= bust_waist or 10 <= hips_waist):
        shapes.append(BodyShape.HOURGLASS)

    # If (hip-bust) ≥ 3.6 and (hip-bust) < 10, then if (hip-waist) ≥ 9, then if (high hip/waist) < 1.193 
    # hourglass with more defined waist
    if 3.6 <= hips_bust and hips_bust < 10 and 9 <= hips_waist and hips_over_waist < 1.193:
        shapes.append(BodyShape.BOTTOM_HOURGLASS)

    # hourglass with more defined bust
    if 1 < bust_hips and bust_hips < 10 and 9 <= bust_waist:
        shapes.append(BodyShape.TOP_HOURGLASS)

    # spoon: hips greater than bust, triangle with smaller waist
    #
    # `<=` rather than `<`, which is the only rule changed from the original. FFIT gives
    # spoon as "(high hip/waist) >= 1.193" and bottom hourglass as "< 1.193"; the
    # original transcribed spoon as a strict `>`, leaving the single ratio 1.193 itself
    # in neither. A hole one float wide is still a hole, and closing it costs nothing.
    if 2 < hips_bust and 7 <= hips_waist and 1.193 <= hips_over_waist:
        shapes.append(BodyShape.SPOON)
    
    # triangle small bust large hips with larger/tapered waist
    if 3.6 <= hips_bust and 0 <= hips_waist < 9 or bust_waist < 0 and 0 <= hips_waist:
        shapes.append(BodyShape.TRIANGLE)

    if 3.6 <= bust_hips and bust_waist < 9:
        shapes.append(BodyShape.INVERTED_TRIANGLE)

    if hips_bust < 3.6 and bust_hips < 3.6 and 0 <= bust_waist < 9 and 0 <= hips_waist < 10:
        shapes.append(BodyShape.RECTANGLE)
    
    if hips_waist < 0 and bust_waist < 0:
        shapes.append(BodyShape.DIAMOND)
    
    if hips_waist < 0 and 0 <= bust_waist:
        shapes.append(BodyShape.OVAL)

    return shapes

# The two regions of the proportion space that no category covers.
#
# Both are inherited from FFIT rather than introduced here, and both are real: FFIT caps
# the hourglass variants at a ten-inch bust/hip difference and hands anything past it to
# the triangles, which in turn require an *undefined* waist. A body with a difference
# that large *and* a waist nine inches smaller than its bust therefore matches nothing.
#
# They are reported rather than patched over. Widening a category to swallow them would
# put a name on a shape no reference gives one to, and a wrong tag is worse than an
# absent one: the point of the calculator is that its tags mean something.
UNCOVERED_REGIONS = (
    ("bust-hips >= 10 and bust-waist >= 9",
     "a bust far larger than the hips, with a defined waist - past the top hourglass "
     "ceiling, but too defined for an inverted triangle"),
    ("hips-bust >= 10 and hips-waist >= 9 and hips/waist <= 1.193",
     "hips far larger than the bust with a defined waist, on a frame wide enough that "
     "the hip/waist ratio stays low - past the bottom hourglass ceiling, but below the "
     "spoon ratio"),
)


def uncovered_region(bust, waist, hips):
    """Which of the known gaps a set of measurements falls in, if any."""
    if bust - hips >= 10 and bust - waist >= 9:
        return UNCOVERED_REGIONS[0]
    if waist and hips - bust >= 10 and hips - waist >= 9 and hips / waist <= 1.193:
        return UNCOVERED_REGIONS[1]
    return None


def shape_diagnostics(bust, waist, hips):
    """Why a set of measurements matched no shape, in the terms the rules are written in.

    Every category is defined by differences rather than by the measurements themselves,
    so the differences are what has to be shown: three numbers alone leave the reader to
    do the subtraction that would tell them whether the data or the rules are at fault.
    """
    lines = [
        "bust=%.1f waist=%.1f hips=%.1f" % (bust, waist, hips),
        "bust-waist=%.1f" % (bust - waist),
        "hips-waist=%.1f" % (hips - waist),
        "bust-hips=%.1f" % (bust - hips),
        ("hips/waist=%.3f" % (hips / waist)) if waist else "hips/waist=n/a",
    ]
    region = uncovered_region(bust, waist, hips)
    if region:
        lines.append("no category covers %s - %s" % region)
    return chr(10).join(lines)


# SEE: https://en.wikipedia.org/wiki/Bra_size#The_meaning_of_cup_sizes_varies
# "cup size approximates the difference between the Over-the-bust and band measurements in inches"
# index == bust band difference in inches
def get_bust_band_difference(cupsize):
    for difference, cup_enum in enumerate(BreastCup):
        if cup_enum.within_threshold(cupsize):
            return difference    
    raise Exception(f"could not identify cupsize '{cupsize}' add to 'BreastCup' enum")

# Approximates breasts weight in kg, derived from this chart https://i.imgur.com/QZBhze8.png
def approximate_breast_weight(bust_band_diff):
    if not bust_band_diff:
        return 0
    bust_band_diff -= 1 
    # 3 Degree Polynomial Trendline
    weight_lb = 0.765 + 0.415 * bust_band_diff + -0.0168 * bust_band_diff ** 2 + 0.00247 * bust_band_diff ** 3
    return weight_lb * 0.453

def get_tag_classes():
    import sys, inspect
    tag_classes = []
    for _, cls in inspect.getmembers(sys.modules[__name__], inspect.isclass):
        if cls in [StashTagEnum, StashTagEnumComparable]:
            continue
        if issubclass(cls, StashTagEnum):
            tag_classes.append(cls)
    return tag_classes
