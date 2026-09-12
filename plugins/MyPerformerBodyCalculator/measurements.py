"""Turning a performer's `measurements` string into numbers, with their meaning attached.

Extracted from `StashPerformer.parse_measurements` in the original Performer Body
Calculator, and the one part of it that is genuinely rewritten. It imports nothing but
`body_tags`, so it can be exercised on its own - which is what the regression tests do.

--------------------------------------------------------------------------------------
The bug this exists to fix

The original matched `32D-28-34` and `90G-60-80` with the same regex and gave both the
same meaning: leading number is a **bra band**, letter is a cup, and the real bust is
`band + cup difference`. That is right for the first and wrong for the second.

`90G-60-80` is the Japanese/metric B-W-H convention: 90 *is* the bust circumference, in
centimetres, and `G` is a separate cup size. Reading it as a band produced

    90 / 2.54 = 35.4  ->  35.4 + 7 (G) = 42.4" bust,  23.6" waist,  31.5" hips

a bust ten inches larger than the hips with a waist nineteen inches smaller - a shape
the classifier has no category for, which is where the "could not classify bodyshape
bust=42 waist=24 hips=31" warnings came from. They were not a classifier problem.

--------------------------------------------------------------------------------------
How the leading number's meaning is decided

Not by the regex - the two forms are textually identical, `digits + letters`. By the
whole measurement set:

* **Three metric values** (`90G-60-80`, `88(E)-58-89`): a B-W-H triple, so the leading
  number is a bust circumference and the cup is extra information about the same bust.
  It is never added to anything.  -> `metric_bust`
* **Three imperial values** (`32D-28-34`): a bra size followed by waist and hips, which
  is the western convention. The leading number is a band, and the bust is derived from
  it exactly as the original did.  -> `imperial_band`
* **A bra size on its own** (`32D`, `75E`): there is no triple to belong to, and a
  number written with a cup letter and nothing else is a bra size whichever units it is
  in. The leading number is a band.  -> `imperial_band` / `metric_band`
* **No cup letter at all** (`36-28-34`, `86/64/89`): the leading number is a bust, in
  both unit systems, exactly as the original had it.  -> `imperial_bust` / `metric_bust`

The result carries `first_value_is_bust` and `measurement_type` so no later step has to
re-derive any of this - and, in particular, so nothing downstream can add a cup
difference to a number that is already a bust.

Units are decided the way the original decided them - values above 50 are centimetres -
applied to whichever of bust/band, waist and hips are present rather than requiring all
three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from body_tags import BreastCup, get_bust_band_difference

CM_TO_INCH = 2.54

# Above this, a measurement is centimetres. The original used the same number: no
# adult's bust, waist or hips is over 50 inches *and* under 50 centimetres, so the two
# ranges do not overlap in practice.
METRIC_THRESHOLD = 50

IMPERIAL = "imperial"
METRIC = "metric"

# What the leading number means, and therefore what may be done with it.
METRIC_BUST = "metric_bust"        # leading number is a bust circumference, in cm
IMPERIAL_BUST = "imperial_bust"    # leading number is a bust circumference, in inches
METRIC_BAND = "metric_band"        # leading number is a bra band, in cm
IMPERIAL_BAND = "imperial_band"    # leading number is a bra band, in inches
WAIST_HIPS = "waist_hips"          # no leading number at all

# Why a string produced nothing usable. Both are data problems, not failures: a
# performer whose measurements are blank or unreadable has been processed, and saying
# so is the point.
MISSING = "missing"
UNSUPPORTED = "unsupported"


@dataclass
class Measurements:
    """What a measurements string says, in inches, with its semantics attached."""

    raw: str = ""
    text: str = ""                       # whitespace removed, as matched
    units: str = ""                      # IMPERIAL | METRIC
    measurement_type: str = ""           # one of the constants above
    pattern: str = ""                    # which shape matched, for the debug log

    first_value: float | None = None     # the leading number, in its original units
    first_value_is_bust: bool | None = None

    cup: str | None = None               # normalised to upper case
    cup_known: bool = True               # False when BreastCup has no such cup
    bust_band_difference: int | None = None

    raw_waist: float | None = None       # as written, before any conversion
    raw_hips: float | None = None

    band: float | None = None            # inches
    bust: float | None = None            # inches
    waist: float | None = None           # inches
    hips: float | None = None            # inches
    band_estimated: bool = False

    breast_volume: float | None = None
    error: str | None = None             # MISSING | UNSUPPORTED
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


# Order matters, and it is the original's order: the three-value forms are tried before
# the prefix forms, or `32D-28-34` would match the bra-size-only pattern and lose its
# waist and hips.
#
# Every pattern accepts the cup in brackets or without them, in either case, and either
# side of the number - `88(E)-58-89`, `88E-58-89`, `88e-58-89` and `E88-58-89` are the
# same measurement. Whitespace is removed before matching, so `88 (E) - 58 - 89` is too.
_SEP = r"[-/]"
_NUM = r"\d+(?:\.\d+)?"
_CUP = r"\(?(?P<cup>[A-Za-z]+)\)?"

PATTERNS = (
    # Waist and hips only, as some databases write an unknown bra size.
    ("waist_hips", re.compile(
        rf"^NoneNone{_SEP}(?P<waist>{_NUM}){_SEP}(?P<hips>{_NUM})$", re.IGNORECASE)),

    # Leading number with a cup, then waist and hips: "32D-28-34", "90G-60-80",
    # "88(E)-58-89", "90(G)-60-80".
    ("first_cup_waist_hips", re.compile(
        rf"^(?P<first>{_NUM}){_CUP}{_SEP}(?P<waist>{_NUM}){_SEP}(?P<hips>{_NUM})$")),

    # The same with the cup written first: "D32-28-34", "DD32-23-27", "H35-26-37".
    ("cup_first_waist_hips", re.compile(
        rf"^{_CUP}(?P<first>{_NUM}){_SEP}(?P<waist>{_NUM}){_SEP}(?P<hips>{_NUM})$")),

    # Three numbers, no cup: "36-28-34", "86/64/89".
    ("bust_waist_hips", re.compile(
        rf"^(?P<first>{_NUM}){_SEP}(?P<waist>{_NUM}){_SEP}(?P<hips>{_NUM})$")),

    # A bra size on its own, possibly with something unparseable after it:
    # "32D", "32D(81D)", "32D-??-??".
    ("first_cup", re.compile(rf"^(?P<first>{_NUM}){_CUP}")),

    # The same with the cup first: "D32", "B32-None-None", "DDD38-None-None".
    ("cup_first", re.compile(rf"^{_CUP}(?P<first>{_NUM})")),
)


def parse(raw) -> Measurements:
    """Read a measurements string. Never raises; unreadable input comes back as an error."""
    out = Measurements(raw=raw or "")
    out.text = re.sub(r"\s+", "", str(raw or ""))

    if not out.text:
        out.error = MISSING
        return out

    matched = None
    for name, pattern in PATTERNS:
        found = pattern.match(out.text)
        if found:
            matched = (name, found.groupdict())
            break

    if matched is None:
        out.error = UNSUPPORTED
        return out

    out.pattern, groups = matched

    first = _number(groups.get("first"))
    waist = _number(groups.get("waist"))
    hips = _number(groups.get("hips"))
    cup = (groups.get("cup") or "").upper() or None

    out.first_value = first
    out.raw_waist = waist
    out.raw_hips = hips
    out.cup = cup

    # ---------------------------------------------------------------- units
    # Whichever values are present have to agree. One value over 50 with two under it is
    # not a unit system, it is a typo, and guessing at it would be worse than leaving it
    # in inches - which is what the original did with anything it could not confirm.
    present = [value for value in (first, waist, hips) if value]
    out.units = METRIC if present and all(v > METRIC_THRESHOLD for v in present) else IMPERIAL
    divisor = CM_TO_INCH if out.units == METRIC else 1.0

    # ------------------------------------------------------------ semantics
    if first is None:
        out.measurement_type = WAIST_HIPS
        out.first_value_is_bust = None
    elif cup is None:
        # No cup letter: the leading number has only ever meant a bust circumference.
        out.measurement_type = METRIC_BUST if out.units == METRIC else IMPERIAL_BUST
        out.first_value_is_bust = True
    elif waist is not None and hips is not None and out.units == METRIC:
        # The case the original got wrong: a metric B-W-H triple. The number is the
        # bust, and the cup says something about that bust rather than adding to it.
        out.measurement_type = METRIC_BUST
        out.first_value_is_bust = True
    else:
        # A bra size: either imperial (with or without waist and hips) or metric with no
        # triple around it to make it a bust.
        out.measurement_type = METRIC_BAND if out.units == METRIC else IMPERIAL_BAND
        out.first_value_is_bust = False

    # ------------------------------------------------------------ the numbers
    if waist is not None:
        out.waist = waist / divisor
    if hips is not None:
        out.hips = hips / divisor

    if cup:
        try:
            out.bust_band_difference = get_bust_band_difference(cup)
        except Exception:
            # A cup BreastCup has never heard of. Kept as text so the rest of the record
            # is still usable, and reported rather than raised: it is bad data, not a
            # broken run.
            out.cup_known = False
            out.notes.append(f"unknown cup size {cup!r}")

    if out.first_value_is_bust:
        out.bust = first / divisor
        if out.bust_band_difference is not None:
            # The band is not stated, but the breast-size and BMI calculations both need
            # one. Inverting the cup rule gives it: a cup size *is* the bust-band
            # difference in inches, so the band is the bust less that difference. Marked
            # estimated so nothing reports it as measured.
            out.band = out.bust - out.bust_band_difference
            out.band_estimated = True
    elif out.first_value_is_bust is False:
        out.band = first / divisor
        if out.bust_band_difference is not None:
            out.bust = out.band + out.bust_band_difference

    if out.band is not None and out.bust_band_difference is not None:
        # Unchanged from the original: half the band plus the cup difference, the
        # plugin's own "volume points" scale that BreastSize thresholds are tuned to.
        out.breast_volume = (out.band / 2.0) + out.bust_band_difference

    return out


def _number(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number else None


def describe(m: Measurements) -> str:
    """One line for the debug log, saying what was read and how it was understood."""
    if not m.ok:
        return f"{m.raw!r}: {m.error}"
    parts = [f"{m.raw!r} -> {m.measurement_type} ({m.units})"]
    if m.bust is not None:
        parts.append(f"bust={m.bust:.2f}\"")
    if m.band is not None:
        parts.append(f"band={m.band:.2f}\"" + ("(est)" if m.band_estimated else ""))
    if m.cup:
        parts.append(f"cup={m.cup}")
    if m.waist is not None:
        parts.append(f"waist={m.waist:.2f}\"")
    if m.hips is not None:
        parts.append(f"hips={m.hips:.2f}\"")
    return " ".join(parts)
