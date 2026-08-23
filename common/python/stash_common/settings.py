"""The settings ScrapeAll obeys, as declared by the ScrapeAllSettings plugin.

Both ends read this module so they cannot drift: the plugin's task parses and
reports the settings, the scraper parses and acts on them. Stash keeps plugin
settings in its own config, reachable at `configuration.plugins`, which is why a
plugin is the place to put them - a scraper has nowhere to store configuration a
user can edit in the UI.
"""

from __future__ import annotations

from . import graphql

# Stash derives a plugin's id from its yml filename.
PLUGIN_ID = "ScrapeAllSettings"

ALLOWED_FIELDS = "allowedFields"
IGNORED_SCRAPERS = "ignoredScrapers"
CREATE_SCRAPE_TAGS = "createScrapeTags"

# Stash plugin settings have no declarable default, so an untouched boolean
# arrives absent rather than false - the default lives here instead.
CREATE_SCRAPE_TAGS_DEFAULT = True

# Marks a tag as bookkeeping rather than content. The brackets are only a naming
# convention - they make the tags sort and filter together in the UI.
SCRAPE_TAG_PREFIX = "[scrapper:"
SCRAPE_TAG_SUFFIX = "]"

# Scene field -> how several sources' answers for it are combined. The keys are
# the whitelist vocabulary, so this dict is also what the plugin's hint lists.
MERGE_RULES = {
    "title": "first source that returned one",
    "code": "first source that returned one",
    "date": "first source that returned one",
    "director": "first source that returned one",
    "details": "one line per source that identified the scene",
    "urls": "the scene's existing URLs plus every new one, duplicates dropped",
    "studio": "the studio the most sources agree on",
    "performers": "union of every source, de-duplicated",
    "tags": "union of every source, de-duplicated",
    "groups": "union of every source, de-duplicated",
}
FIELDS = tuple(MERGE_RULES)

# What people will plausibly type instead of the schema's own name.
ALIASES = {
    "url": "urls",
    "performer": "performers",
    "tag": "tags",
    "studios": "studio",
    "group": "groups",
    "movie": "groups",
    "movies": "groups",
    "description": "details",
    "detail": "details",
}

QUERY = "query { configuration { plugins } }"


def fetch(url: str, api_key: str | None = None, cookie: str | None = None) -> dict:
    """Raw settings dict for the plugin, or {} when unset or unreachable."""
    data = graphql.try_call(url, QUERY, api_key=api_key, cookie=cookie)
    plugins = ((data or {}).get("configuration") or {}).get("plugins") or {}
    values = plugins.get(PLUGIN_ID)
    return values if isinstance(values, dict) else {}


def split(raw) -> list[str]:
    """Comma-separated list, forgiving about semicolons, newlines and spacing."""
    text = str(raw or "").replace(";", ",").replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def scrape_tag(source_name: str) -> str:
    """The bookkeeping tag naming one source, e.g. "[scrapper:StashDB]"."""
    return SCRAPE_TAG_PREFIX + str(source_name).strip() + SCRAPE_TAG_SUFFIX


def as_bool(value, default: bool) -> bool:
    """Stash stores an unset boolean setting as absent, not as false."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class Parsed:
    """allowed is None for "every field"; an empty set means "no field"."""

    def __init__(self, allowed, ignored, create_tags, notes, errors):
        self.allowed = allowed
        self.ignored = ignored
        self.create_tags = create_tags
        self.notes = notes
        self.errors = errors

    def permits(self, field: str) -> bool:
        return self.allowed is None or field in self.allowed

    def ignores(self, *names) -> bool:
        return any((name or "").strip().lower() in self.ignored for name in names)

    def describe(self) -> list[str]:
        fields = "every field" if self.allowed is None else (
            ", ".join(sorted(self.allowed)) if self.allowed else "NOTHING - no field will be written")
        return [
            "allowed fields : " + fields,
            "ignored sources: " + (", ".join(sorted(self.ignored)) or "(none)"),
            "scrape tags    : " + ("on - " + scrape_tag("<source>") + " per probed source"
                                   if self.create_tags else "off"),
        ]


def parse(raw: dict) -> Parsed:
    """Validate the raw settings dict into something the scraper can act on."""
    notes, errors = [], []

    requested = split(raw.get(ALLOWED_FIELDS))
    allowed = None
    if requested:
        allowed = set()
        for name in requested:
            key = ALIASES.get(name.lower(), name.lower())
            if key in MERGE_RULES:
                if key != name.lower():
                    notes.append('read "' + name + '" as "' + key + '"')
                allowed.add(key)
            else:
                errors.append('unknown field "' + name + '" - known fields: ' + ", ".join(FIELDS))
        if not allowed:
            # Falling back to "everything" would write far more than was asked
            # for, so an all-typos whitelist stays a whitelist of nothing.
            errors.append("no valid field in " + ALLOWED_FIELDS + " - nothing will be written")

    return Parsed(
        allowed,
        {name.lower() for name in split(raw.get(IGNORED_SCRAPERS))},
        as_bool(raw.get(CREATE_SCRAPE_TAGS), CREATE_SCRAPE_TAGS_DEFAULT),
        notes,
        errors,
    )


def hint() -> str:
    """The available-fields hint shown under the setting in the Stash UI."""
    return "Available: " + ", ".join(FIELDS)
