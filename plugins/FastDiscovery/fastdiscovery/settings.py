"""FastDiscovery configuration.

Stash keeps plugin settings in its own config, at `configuration.plugins.<pluginId>`,
writable with `configurePlugin`. Two facts about that store shape this module:

* a setting has no declarable default - an untouched one arrives absent, not false or
  zero - so every default lives here;
* only STRING, NUMBER and BOOLEAN exist.

FastDiscovery deliberately has **no** stash-box configuration of its own. The
endpoints and their credentials belong to Stash (`configuration.general.stashBoxes`),
and a second copy of an API key is a second thing to leak (requirement 2, 43).
"""

from __future__ import annotations

STRING, NUMBER, BOOLEAN = "STRING", "NUMBER", "BOOLEAN"

# Stash derives a plugin's id from its yml filename.
PLUGIN_ID = "FastDiscovery"

# Scrapers FastDiscovery must never invoke, whatever the configuration says.
#
# Not merely a default. These are orchestrators: FastDiscovery's own scraper entry
# point starts a run, so invoking it from inside a run would start another one, and
# ScrapeAll answers one call by probing every other source it can find. None of them
# declares a URL pattern, so in practice a URL scrape cannot reach them anyway - this
# is the guard that keeps that true if one ever does.
NEVER_INVOKE = ("fastdiscovery", "scrapediscovery", "scrapeall")


def _spec():
    """name -> (type, default, description). The description is what the UI shows."""
    return {
        "databasePath": (
            STRING, "",
            "Where fastdiscovery.sqlite lives. Empty means "
            "<stash config dir>/fast-discovery/fastdiscovery.sqlite. Keep it outside "
            "the plugin directory: updating a plugin package deletes the files the "
            "package installed."),
        "recursiveUrlDiscovery": (
            BOOLEAN, True,
            "Follow URLs found inside scraper results, and the URLs those results "
            "mention in turn. Off means only the URLs already on the scene and the "
            "ones the stash-boxes returned are scraped."),
        "maxDepth": (
            NUMBER, 3,
            "How far to follow discovered URLs. The scene's own URLs and the "
            "stash-box results are depth 0."),
        "maxUrlsPerRun": (
            NUMBER, 60,
            "Stop discovering new URLs for one scene after this many. A guard against "
            "a site that links to a hundred related scenes, not a target."),
        "maxSourcesPerRun": (
            NUMBER, 120,
            "Hard ceiling on scraper invocations for one run."),
        "maxConcurrentScrapers": (
            NUMBER, 3,
            "How many scraper invocations run at once. Each one is a request to Stash "
            "that may launch a browser, so keep this small. Maximum 16."),
        "scraperTimeout": (
            NUMBER, 30,
            "How long one scraper gets before its source is abandoned. Abandoning the "
            "request also cancels the scraper process on the server."),
        "aimAmbiguousUrls": (
            BOOLEAN, True,
            "When several installed scrapers match one URL, Stash's own URL scrape "
            "cannot be aimed at a chosen one. With this on, every such scraper that "
            "also supports fragment scraping is additionally called directly with the "
            "URL, so each gets its own reviewable column."),
        "stashboxNameSearch": (
            BOOLEAN, False,
            "Also search every stash-box by title, on top of the fingerprint lookup "
            "that always runs. Off by default: a name search answers with near-misses, "
            "which become columns you have to read past."),
        "maxResultsPerSource": (
            NUMBER, 3,
            "A source that answers with a list - a stash-box name search - contributes "
            "at most this many columns."),
        "runTimeBudget": (
            NUMBER, 900,
            "Stop a run after this many seconds, keeping everything already found. "
            "0 removes the limit."),
        "staleRunHours": (
            NUMBER, 12,
            "A run still claiming to be running after this long was killed rather than "
            "finished, and is swept to FAILED."),
        "debugLogging": (
            BOOLEAN, False,
            "Log every source's input, timing and returned field counts. Never logs "
            "credentials, and never dumps a full payload."),
    }


SPEC = _spec()
DEFAULTS = {name: default for name, (_type, default, _doc) in SPEC.items()}
TYPES = {name: kind for name, (kind, _default, _doc) in SPEC.items()}
DESCRIPTIONS = {name: doc for name, (_kind, _default, doc) in SPEC.items()}

# Bounds that exist to protect the server, not to express taste.
LIMITS = {
    "maxDepth": (0, 10),
    "maxUrlsPerRun": (1, 1000),
    "maxSourcesPerRun": (1, 2000),
    "maxConcurrentScrapers": (1, 16),
    "scraperTimeout": (5, 600),
    "maxResultsPerSource": (1, 25),
    "runTimeBudget": (0, 86400),
    "staleRunHours": (1, 720),
}


def as_bool(value, default):
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def as_number(value, default):
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if float(number).is_integer() else number


class Config:
    """Parsed settings. Reads like a dict, and knows what it could not make sense of."""

    def __init__(self, values, problems):
        self.values = values
        self.problems = problems

    def __getitem__(self, name):
        return self.values[name]

    def get(self, name, fallback=None):
        return self.values.get(name, fallback)

    def as_dict(self):
        return dict(self.values)

    def may_invoke(self, scraper_id, name="") -> bool:
        """The one rule no setting can turn off (see NEVER_INVOKE)."""
        for candidate in (scraper_id, name):
            if str(candidate or "").strip().lower().replace(" ", "") in NEVER_INVOKE:
                return False
        return True


def parse(raw) -> Config:
    raw = raw if isinstance(raw, dict) else {}
    values, problems = {}, []

    for name, (kind, default, _doc) in SPEC.items():
        given = raw.get(name)
        if kind == BOOLEAN:
            values[name] = as_bool(given, default)
            continue
        if kind == NUMBER:
            number = as_number(given, default)
            low, high = LIMITS.get(name, (None, None))
            if low is not None and number < low:
                problems.append("%s was %s, clamped to %s" % (name, number, low))
                number = low
            elif high is not None and number > high:
                problems.append("%s was %s, clamped to %s" % (name, number, high))
                number = high
            values[name] = number
            continue
        values[name] = str(given).strip() if given is not None else default

    return Config(values, problems)


def describe(config: Config):
    """The effective configuration, one line per setting, for the diagnostics task."""
    return ["%-22s %s" % (name, config[name]) for name in sorted(SPEC)]
