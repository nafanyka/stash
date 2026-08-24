"""ScrapeDiscovery configuration.

Stash keeps plugin settings in its own config as a flat map, reachable at
`configuration.plugins.<pluginId>` and writable with `configurePlugin`. That map is
the single store for every ScrapeDiscovery setting - the scalars the manifest declares
(so they appear in Stash's own plugin settings UI) and the structured ones the
ScrapeDiscovery settings page writes as JSON strings. One store means the two UIs can
never disagree.

Two facts about Stash plugin settings shape this module (see docs/architecture.md, L4):

* a setting has no declarable default - an untouched one arrives absent, not false or
  zero - so every default lives here;
* only STRING, NUMBER and BOOLEAN exist, so anything structured (per-scraper table,
  scoring weights) is a STRING holding JSON.

Nothing here talks to the network; `stash.py` fetches the raw map and hands it over.
"""

from __future__ import annotations

import json

# Stash derives a plugin's id from its yml filename.
PLUGIN_ID = "ScrapeDiscovery"

STRING, NUMBER, BOOLEAN, JSON_ = "STRING", "NUMBER", "BOOLEAN", "JSON"

# Scan modes.
NORMAL = "normal"
DEEP = "deep"

# Priority bands for a scraper, ordered best first. "disabled" is stored as a band
# rather than a separate flag so one dropdown per scraper covers every state.
PRIORITY_HIGH = "high"
PRIORITY_NORMAL = "normal"
PRIORITY_LOW = "low"
PRIORITY_DISABLED = "disabled"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_NORMAL, PRIORITY_LOW, PRIORITY_DISABLED)
PRIORITY_ORDER = {PRIORITY_HIGH: 0, PRIORITY_NORMAL: 1, PRIORITY_LOW: 2}

# Scrapers that are local utilities rather than metadata sources. They answer every
# fragment scrape with something derived from the file we already have, which is not
# discovery, so they are off the default work list. Deep Scan can re-include them by
# clearing this setting.
DEFAULT_EXCLUDED_SCRAPERS = (
    "builtin_autotag",
    "CopyMetadata",
    "CopyToGallery",
    "FileMetadata",
    "FileMetadata_Windows",
    "Filename",
    "scrapeCoverFromFile",
    "scene-cover-in-dir",
)

# Weight per scoring signal. Only the signals that apply to a given scene take part in
# the normalisation, so a scene with no existing metadata is not punished for the
# comparisons that could not be made. See docs/architecture.md section 6.
DEFAULT_SCORE_WEIGHTS = {
    "fingerprint": 30.0,
    "url_agreement": 25.0,
    "title_similarity": 20.0,
    "duration": 18.0,
    "date_agreement": 10.0,
    "studio_agreement": 10.0,
    "performer_agreement": 10.0,
    "source_agreement": 12.0,
}

DEFAULT_CONFIDENCE_LEVELS = {
    "almost_certain": 95,
    "strong": 80,
    "possible": 60,
}

# Settings where an empty string is a value the user chose, not an absent one.
#
# Stash cannot tell the two apart - a cleared text box and an untouched one both
# arrive as "" - and for almost every setting falling back to the default is right.
# For a list whose default is non-empty it is exactly wrong: clearing the excluded
# scrapers is how a user asks for the local utilities back, and quietly reinstating
# the default would ignore that while the UI showed an empty field.
EMPTY_MEANS_EMPTY = ("excludedScrapers",)


def _spec():
    """name -> (type, default, description).

    The description is what the settings page shows; the manifest repeats it for the
    keys it declares, because Stash cannot read it from here.
    """
    return {
        # -- general ------------------------------------------------------------
        "databasePath": (
            STRING, "",
            "Where scrape-discovery.sqlite lives. Empty means "
            "<stash config dir>/scrape-discovery/scrape-discovery.sqlite. Keep it "
            "outside the plugin directory: updating a plugin package deletes the "
            "files the package installed.",
        ),
        "historyRetentionDays": (
            NUMBER, 180,
            "Delete scans, attempts and raw results older than this. 0 keeps everything.",
        ),
        "debugLogging": (
            BOOLEAN, False,
            "Log every attempt's input and timing at debug level.",
        ),
        # -- discovery ---------------------------------------------------------
        "defaultMode": (
            STRING, NORMAL,
            "Which scan the Discover button runs: normal or deep.",
        ),
        "maxDepth": (
            NUMBER, 2,
            "How far URL expansion may follow URLs found inside results. 0 disables "
            "expansion; the scene's own URLs are depth 0.",
        ),
        "maxUrlsPerScan": (NUMBER, 40, "Stop discovering new URLs after this many."),
        "maxAttemptsPerScan": (NUMBER, 250, "Hard ceiling on scraper invocations per scene."),
        "sceneTimeBudget": (
            NUMBER, 900,
            "Seconds one scene's scan may take before it stops early. 0 removes the limit.",
        ),
        "normalIncludesNameScrapers": (
            BOOLEAN, False,
            "Run name-search scrapers during a Normal Scan. Off by default: a name "
            "search is the slowest kind of attempt and returns mostly near-misses. "
            "Deep Scan always runs them.",
        ),
        "expandRelatedUrls": (
            BOOLEAN, False,
            "Also follow URLs that belong to a performer, studio or group rather than "
            "to the scene. Off by default: a performer's homepage is not a scene page, "
            "so handing it to a scene URL scraper spends an attempt for nothing.",
        ),
        "normalIncludesUnroutedFragments": (
            BOOLEAN, True,
            "During a Normal Scan also try fragment scrapers that have no URL pattern "
            "matching this scene. Off restricts a Normal Scan to routed scrapers only.",
        ),
        # -- execution ---------------------------------------------------------
        "maxConcurrency": (
            NUMBER, 3,
            "Scraper invocations in flight at once. Each one is a separate request to "
            "Stash, which may spawn a browser, so keep this small.",
        ),
        "defaultTimeout": (
            NUMBER, 30,
            "Seconds before a fragment or URL attempt is abandoned. Abandoning the "
            "request cancels the scraper process on the server.",
        ),
        "nameTimeout": (NUMBER, 60, "Seconds before a name-search attempt is abandoned."),
        "urlRescrapeAttempts": (
            NUMBER, 1,
            "How often to scrape a URL that several installed scrapers match. Stash "
            "picks the handler non-deterministically, so more than one attempt can "
            "surface more than one scraper's answer. Duplicates are discarded.",
        ),
        # -- cache -------------------------------------------------------------
        "ttlNoMatchDays": (NUMBER, 30, "Reuse a stored NO_MATCH for this long."),
        "ttlMatchDays": (NUMBER, 90, "Reuse a stored MATCH for this long."),
        "ttlErrorDays": (NUMBER, 1, "Retry an attempt that errored after this long."),
        "ttlTimeoutDays": (NUMBER, 3, "Retry an attempt that timed out after this long."),
        # -- confidence --------------------------------------------------------
        "scoreWeights": (
            JSON_, DEFAULT_SCORE_WEIGHTS,
            "Weight per scoring signal. Absolute values do not matter, only their "
            "ratios: the score is normalised over the signals that apply.",
        ),
        "confidenceLevels": (
            JSON_, DEFAULT_CONFIDENCE_LEVELS,
            "Lower bound of each confidence level.",
        ),
        "minCandidateConfidence": (
            NUMBER, 25,
            "A result scoring below this stays in the history but is not offered as a "
            "candidate. This is what keeps a name search's near-misses out of the "
            "review list.",
        ),
        "singleSourceCap": (
            NUMBER, 89,
            "Highest confidence a candidate may reach when only one source, with no "
            "fingerprint or URL confirmation, supports it.",
        ),
        "durationTolerance": (
            NUMBER, 30,
            "Seconds of difference still counted as the same running time.",
        ),
        "titleMergeThreshold": (
            NUMBER, 0.86,
            "Title similarity (0-1) required to merge two results into one candidate "
            "when they share no URL.",
        ),
        # -- stop conditions ---------------------------------------------------
        "stopOnConfidence": (
            NUMBER, 0,
            "End a Normal Scan once a candidate reaches this confidence. 0 disables. "
            "Deep Scan ignores it.",
        ),
        "stopMinIndependentSources": (
            NUMBER, 2,
            "Independent sources a candidate needs before stopOnConfidence may fire.",
        ),
        # -- scrapers ----------------------------------------------------------
        "excludedScrapers": (
            STRING, ",".join(DEFAULT_EXCLUDED_SCRAPERS),
            "Comma-separated scraper ids or names never to invoke.",
        ),
        "scraperOverrides": (
            JSON_, {},
            'Per-scraper settings, as {"<scraper id>": {"priority": "high|normal|low|'
            'disabled", "timeout": 30, "cacheDays": 30}}.',
        ),
        # -- automation --------------------------------------------------------
        "inputTag": (STRING, "needs_scrape", "Tag that marks a scene for batch discovery."),
        "reviewTag": (STRING, "scrape_review", "Tag added when a scan found candidates."),
        "failedTag": (STRING, "scrape_failed", "Tag added when a scan found nothing."),
        "removeTagsOnApply": (
            BOOLEAN, True,
            "Remove the input and review tags once metadata has been applied.",
        ),
        "manageTags": (
            BOOLEAN, False,
            "Let ScrapeDiscovery add and remove those tags. Off means discovery never "
            "touches a scene at all, not even its tags.",
        ),
        "autoApply": (
            BOOLEAN, False,
            "Apply a candidate without review when it clears the thresholds below. "
            "Off by default, deliberately.",
        ),
        "autoApplyMinConfidence": (NUMBER, 95, "Confidence auto-apply requires."),
        "autoApplyMinIndependentSources": (
            NUMBER, 2, "Independent sources auto-apply requires.",
        ),
        "autoApplyOnlyEmptyFields": (
            BOOLEAN, True,
            "Auto-apply may only fill a singular field that is currently empty.",
        ),
        "autoApplyAddSetFields": (
            BOOLEAN, True,
            "Auto-apply may add performers, tags and URLs.",
        ),
        "createMissingPerformers": (
            BOOLEAN, False, "Create a performer that does not exist yet when applying.",
        ),
        "createMissingTags": (BOOLEAN, False, "Create a tag that does not exist yet."),
        "createMissingStudios": (BOOLEAN, False, "Create a studio that does not exist yet."),
    }


SPEC = _spec()
KEYS = tuple(SPEC)


def defaults() -> dict:
    return {key: SPEC[key][1] for key in SPEC}


def _coerce(kind, raw, fallback):
    """One raw setting value into the type its spec promises.

    Stash stores what its UI produced, which for a NUMBER can still be a string, and
    for an untouched BOOLEAN is nothing at all. Anything unparseable falls back to the
    default rather than raising: a typo in one setting must not stop discovery.
    """
    if raw is None or raw == "":
        return fallback
    try:
        if kind == BOOLEAN:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if kind == NUMBER:
            text = str(raw).strip()
            value = float(text)
            # Keep ints as ints so limits and day counts do not print as 30.0.
            return int(value) if value.is_integer() and "." not in text else value
        if kind == JSON_:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return parsed if isinstance(parsed, dict) else fallback
        return str(raw)
    except (TypeError, ValueError):
        return fallback


class Config:
    """Effective settings, plus the per-scraper view of them."""

    def __init__(self, values: dict, problems: list):
        self._values = values
        self.problems = problems

    def __getitem__(self, key):
        return self._values[key]

    def get(self, key, fallback=None):
        return self._values.get(key, fallback)

    def as_dict(self) -> dict:
        return dict(self._values)

    # -- derived views -----------------------------------------------------

    def excluded(self) -> set:
        raw = str(self._values.get("excludedScrapers") or "")
        raw = raw.replace(";", ",").replace("\n", ",")
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    def override(self, scraper_id: str) -> dict:
        table = self._values.get("scraperOverrides") or {}
        entry = table.get(scraper_id)
        return entry if isinstance(entry, dict) else {}

    def priority(self, scraper_id: str) -> str:
        value = str(self.override(scraper_id).get("priority") or PRIORITY_NORMAL).lower()
        return value if value in PRIORITIES else PRIORITY_NORMAL

    def is_enabled(self, scraper_id: str, scraper_name: str = "") -> bool:
        if self.priority(scraper_id) == PRIORITY_DISABLED:
            return False
        excluded = self.excluded()
        return not (scraper_id.lower() in excluded or (scraper_name or "").lower() in excluded)

    def timeout_for(self, scraper_id: str, method: str) -> float:
        override = self.override(scraper_id).get("timeout")
        if override:
            return _coerce(NUMBER, override, self._values["defaultTimeout"])
        key = "nameTimeout" if method == "NAME" else "defaultTimeout"
        return self._values[key]

    def cache_days(self, scraper_id: str, status: str) -> float:
        """TTL in days for a stored attempt with this status."""
        override = self.override(scraper_id).get("cacheDays")
        by_status = {
            "MATCH": "ttlMatchDays",
            "NO_MATCH": "ttlNoMatchDays",
            "ERROR": "ttlErrorDays",
            "TIMEOUT": "ttlTimeoutDays",
        }
        if status not in by_status:
            return 0
        # A per-scraper override stands in for the two long TTLs only: a scraper that
        # is merely slow to update should not also stop erroring quickly.
        if override and status in ("MATCH", "NO_MATCH"):
            return _coerce(NUMBER, override, self._values[by_status[status]])
        return self._values[by_status[status]]

    def weights(self) -> dict:
        merged = dict(DEFAULT_SCORE_WEIGHTS)
        for key, value in (self._values.get("scoreWeights") or {}).items():
            if key in merged:
                try:
                    merged[key] = float(value)
                except (TypeError, ValueError):
                    pass
        return merged

    def level_of(self, confidence) -> str:
        levels = dict(DEFAULT_CONFIDENCE_LEVELS)
        for key, value in (self._values.get("confidenceLevels") or {}).items():
            if key in levels:
                levels[key] = _coerce(NUMBER, value, levels[key])
        if confidence is None:
            return "unknown"
        if confidence >= levels["almost_certain"]:
            return "almost_certain"
        if confidence >= levels["strong"]:
            return "strong"
        if confidence >= levels["possible"]:
            return "possible"
        return "weak"


def parse(raw: dict) -> Config:
    """Validate the raw plugin settings map into something the engine can act on."""
    raw = raw if isinstance(raw, dict) else {}
    values, problems = {}, []

    for key, (kind, default, _description) in SPEC.items():
        given = raw.get(key)
        if kind == JSON_ and isinstance(given, str) and given.strip():
            # Checked before coercion, because _coerce falls back silently and a
            # structured setting that quietly reverts to its default is a setting the
            # user believes is in force. Say so instead.
            try:
                parsed = json.loads(given)
            except ValueError as exc:
                problems.append("%s is not valid JSON (%s) - using the default"
                                % (key, exc))
                parsed = None
            if parsed is not None and not isinstance(parsed, dict):
                problems.append("%s must be a JSON object - using the default" % key)
        if key in EMPTY_MEANS_EMPTY and isinstance(given, str) and not given.strip():
            values[key] = ""
        else:
            values[key] = _coerce(kind, given, default)

    if values["defaultMode"] not in (NORMAL, DEEP):
        problems.append('defaultMode must be "normal" or "deep" - using normal')
        values["defaultMode"] = NORMAL

    # Guard the two settings that could make a scan hammer the server.
    if values["maxConcurrency"] < 1:
        values["maxConcurrency"] = 1
    if values["maxConcurrency"] > 16:
        problems.append("maxConcurrency capped at 16 to keep Stash responsive")
        values["maxConcurrency"] = 16
    for key in ("defaultTimeout", "nameTimeout"):
        if values[key] < 5:
            problems.append("%s raised to 5s - anything less times out healthy scrapers" % key)
            values[key] = 5
    if values["urlRescrapeAttempts"] < 1:
        values["urlRescrapeAttempts"] = 1

    for key in ("priority",):  # validated lazily per scraper, but report obvious typos
        for scraper_id, entry in (values["scraperOverrides"] or {}).items():
            if not isinstance(entry, dict):
                problems.append("scraperOverrides[%s] is not an object" % scraper_id)
                continue
            given = str(entry.get(key) or PRIORITY_NORMAL).lower()
            if given not in PRIORITIES:
                problems.append(
                    "scraperOverrides[%s].priority %r is not one of %s"
                    % (scraper_id, entry.get(key), ", ".join(PRIORITIES))
                )

    return Config(values, problems)


def describe(config: Config) -> list:
    """A few lines summarising the configuration, for the diagnostics task."""
    return [
        "database        : " + (config["databasePath"] or "<stash config dir>/scrape-discovery/"),
        "default mode    : " + config["defaultMode"],
        "limits          : depth %s, %s urls, %s attempts, %ss budget"
        % (config["maxDepth"], config["maxUrlsPerScan"],
           config["maxAttemptsPerScan"], config["sceneTimeBudget"]),
        "execution       : %s concurrent, %ss timeout (%ss for name search)"
        % (config["maxConcurrency"], config["defaultTimeout"], config["nameTimeout"]),
        "cache ttl       : match %sd, no-match %sd, error %sd, timeout %sd"
        % (config["ttlMatchDays"], config["ttlNoMatchDays"],
           config["ttlErrorDays"], config["ttlTimeoutDays"]),
        "excluded        : %s scraper(s)" % len(config.excluded()),
        "auto apply      : " + ("on" if config["autoApply"] else "off"),
        "tag management  : " + ("on" if config["manageTags"] else "off"),
    ]
