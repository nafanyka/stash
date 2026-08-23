#!/usr/bin/env python3
"""ScrapeAll - probe every non-URL scene source and return one merged result.

Registered as a scene fragment scraper, so it shows up both in the scene edit
panel ("Scrape with...") and as an Identify source. Three phases:

1. inventory - every scraper Stash has loaded, grouped by category (log only)
2. probe - for each source that can work from the scene itself (fragment
   scraping: stash-id, phash and other fingerprints, plus name search), call
   scrapeSingleScene. Hits log at Info, misses and failures at Error.
3. merge - combine the hits into a single scraped scene and return it, so the
   edit panel opens its normal merge dialog and Identify can apply it.

URL-bound scrapers are skipped: they need a URL on the scene, not the scene.

What may be returned is limited by the ScrapeAllSettings plugin's settings; see
stash_common/settings.py for the field list and how each field is combined.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config, graphql, log, settings  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage
    except (AttributeError, OSError):
        pass

# Stash derives a scraper's id from its yml filename, which matches this script's.
# Skipping it is what stops the probe phase from calling ScrapeAll recursively -
# an env marker would not survive, since the nested run is spawned by the server.
SELF_ID = Path(__file__).stem

PER_SOURCE_TIMEOUT = 60  # seconds; a scraper driving a browser is not quick
TOTAL_BUDGET = 300  # seconds for the whole probe phase - the UI blocks on it
MAX_RESULTS_LOGGED = 10  # a name search can return a long list

# Provenance letters written into `details`, one line per identifying source.
MODE_LETTERS = {"fragment": "F", "name": "N", "stash-box": "S"}

# Category key -> (heading, GraphQL ScrapeContentType). Order drives the report.
CATEGORIES = [
    ("scene", "SCENE", "SCENE"),
    ("group", "GROUP", "GROUP"),
    ("movie", "MOVIE", "MOVIE"),
    ("performer", "PERFORMER", "PERFORMER"),
    ("gallery", "GALLERY", "GALLERY"),
    ("image", "IMAGE", "IMAGE"),
]
CONTENT_TYPE = {key: content_type for key, _, content_type in CATEGORIES}

# Stash renamed MOVIE to GROUP in 0.28 and gained IMAGE in 0.20, so one query is
# not valid across every supported version. Probe widest-first: a version
# mismatch is a GraphQL validation error, not a partial result.
QUERY_TIERS = [
    ["scene", "group", "performer", "gallery", "image"],
    ["scene", "movie", "performer", "gallery", "image"],
    ["scene", "movie", "performer", "gallery"],
]

LEGACY_FIELDS = [
    ("listSceneScrapers", "scene"),
    ("listPerformerScrapers", "performer"),
    ("listGalleryScrapers", "gallery"),
    ("listMovieScrapers", "movie"),
]

SCRAPE_FLAGS = [("NAME", "N"), ("FRAGMENT", "F"), ("URL", "U")]
WIDTH = 72


# --------------------------------------------------------------------------- #
# inventory


def build_query(keys: list[str]) -> str:
    types = ", ".join(CONTENT_TYPE[key] for key in keys)
    fields = "\n    ".join(key + " { supported_scrapes urls }" for key in keys)
    return "query {\n  listScrapers(types: [" + types + "]) {\n    id\n    name\n    " + fields + "\n  }\n}"


def build_legacy_query() -> str:
    parts = [field + " { id name " + key + " { supported_scrapes urls } }" for field, key in LEGACY_FIELDS]
    return "query {\n  " + "\n  ".join(parts) + "\n}"


def from_graphql(url: str, api_key: str | None):
    """Ask the server. Returns (scrapers, how) or None if no query shape fits."""
    # Cheap reachability probe first: without it an offline or unauthenticated
    # server costs one failed request per tier below, all with the same cause.
    probe = graphql.try_call(url, "query { version { version } }", api_key=api_key)
    if probe is None:
        return None
    version = ((probe.get("version") or {}).get("version")) or "unknown version"

    for keys in QUERY_TIERS:
        data = graphql.try_call(url, build_query(keys), api_key=api_key)
        if data and isinstance(data.get("listScrapers"), list):
            return data["listScrapers"], "Stash " + version + ", listScrapers(" + ", ".join(keys) + ")"

    data = graphql.try_call(url, build_legacy_query(), api_key=api_key)
    if not data:
        return None

    merged: dict[str, dict] = {}
    for field, key in LEGACY_FIELDS:
        for scraper in data.get(field) or []:
            entry = merged.setdefault(scraper["id"], {"id": scraper["id"], "name": scraper["name"]})
            entry[key] = scraper.get(key)
    return list(merged.values()), "Stash " + version + ", legacy list*Scrapers"


def from_disk():
    """Last resort: read the sibling scraper folders instead of asking Stash.

    Regex-based rather than yaml-based on purpose - pyyaml is not guaranteed to be
    present, and all that is needed is the name plus which *By* actions exist.
    """
    root = Path(__file__).resolve().parent.parent
    action_re = re.compile(r"^(?P<kind>[a-z]+)By(?P<how>Name|Fragment|URL|QueryFragment)\s*:", re.MULTILINE)
    name_re = re.compile(r"^name\s*:\s*(?P<name>.+?)\s*$", re.MULTILINE)

    scrapers = []
    for path in sorted(root.rglob("*.yml")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("unreadable: " + path.name + ": " + str(exc))
            continue

        found: dict[str, set] = {}
        for match in action_re.finditer(text):
            kind = match.group("kind")
            if kind not in CONTENT_TYPE:
                continue
            how = match.group("how")
            found.setdefault(kind, set()).add("FRAGMENT" if how == "QueryFragment" else how.upper())
        if not found:
            continue

        name_match = name_re.search(text)
        name = name_match.group("name").strip("\"'") if name_match else path.stem
        entry = {"id": path.stem, "name": name}
        for kind, modes in found.items():
            entry[kind] = {"supported_scrapes": sorted(modes), "urls": []}
        scrapers.append(entry)

    return scrapers, "disk scan of " + str(root)


def flags_of(supported) -> str:
    have = set(supported or [])
    return "".join(char if name in have else "-" for name, char in SCRAPE_FLAGS)


def host_of(raw: str) -> str:
    return re.sub(r"^\w+://", "", str(raw or "").strip()).split("/")[0]


def domains_of(urls, limit: int = 4) -> str:
    hosts: list[str] = []
    for raw in urls or []:
        host = host_of(raw)
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return ""
    shown = ", ".join(hosts[:limit])
    return shown + (", +" + str(len(hosts) - limit) + " more" if len(hosts) > limit else "")


def render(scrapers: list[dict], source: str) -> str:
    lines = [
        "ScrapeAll - installed scraper inventory.",
        "",
        "Source: " + source,
        "Total scrapers: " + str(len(scrapers)),
        "",
    ]

    for key, heading, _ in CATEGORIES:
        members = [s for s in scrapers if isinstance(s.get(key), dict)]
        if not members:
            continue

        title = "== " + heading + " (" + str(len(members)) + ") "
        lines.append(title + "=" * max(0, WIDTH - len(title)))
        if key == "scene":
            usable = [s for s in members if "FRAGMENT" in (s[key].get("supported_scrapes") or [])]
            lines.append("   " + str(len(usable)) + " of these are usable as Identify sources (F flag).")

        for scraper in sorted(members, key=lambda s: (s.get("name") or s["id"]).lower()):
            spec = scraper[key]
            name = scraper.get("name") or scraper["id"]
            label = name if name == scraper["id"] else name + " [" + scraper["id"] + "]"
            lines.append("  " + flags_of(spec.get("supported_scrapes")) + "  " + label)
            domains = domains_of(spec.get("urls"))
            if domains:
                lines.append("          " + domains)
        lines.append("")

    lines += [
        "-" * WIDTH,
        "Flags: N = by name (Tagger)  F = by fragment (edit panel + Identify)  U = by URL",
        "A dash means that mode is not supported for this category.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# schema discovery

# Which fields exist varies by version (`url` became `urls`, `movies` became
# `groups`, `code`/`director` are newer), and asking for one that is gone fails
# the whole query. `__type` returns null for a type that does not exist at all,
# so this one request is safe everywhere.
INTROSPECT = """
query {
  scene: __type(name: "ScrapedScene") { fields { name } }
  studio: __type(name: "ScrapedStudio") { fields { name } }
  performer: __type(name: "ScrapedPerformer") { fields { name } }
  tag: __type(name: "ScrapedTag") { fields { name } }
  group: __type(name: "ScrapedGroup") { fields { name } }
  movie: __type(name: "ScrapedMovie") { fields { name } }
  source: __type(name: "ScraperSourceInput") { inputFields { name } }
  input: __type(name: "ScrapeSingleSceneInput") { inputFields { name } }
}
"""

SCALARS = ["title", "code", "details", "director", "date", "url", "urls", "remote_site_id"]
NAME_LISTS = ["performers", "tags", "groups", "movies"]

SCENE_TITLE_TIERS = [
    "query($id: ID!) { findScene(id: $id) { title files { basename } } }",
    "query($id: ID!) { findScene(id: $id) { title path } }",
    "query($id: ID!) { findScene(id: $id) { title } }",
]
SCENE_URL_TIERS = [
    "query($id: ID!) { findScene(id: $id) { urls } }",
    "query($id: ID!) { findScene(id: $id) { url } }",
]

STASH_BOXES = "query { configuration { general { stashBoxes { endpoint name } } } }"

# Only the bookkeeping tags are needed, so filter server-side; the unfiltered
# form is the fallback for a version whose tag filter differs.
TAG_LOOKUP_TIERS = [
    ("query($v: String!) { findTags(tag_filter: {name: {value: $v, modifier: INCLUDES}},"
     " filter: {per_page: -1}) { tags { id name } } }", True),
    ("query { findTags(filter: {per_page: -1}) { tags { id name } } }", False),
]
TAG_CREATE = "mutation($name: String!) { tagCreate(input: {name: $name}) { id name } }"


class Schema:
    """What this Stash's scrape types actually offer."""

    def __init__(self, data: dict | None):
        data = data or {}

        def fields(node, key="fields"):
            return {f["name"] for f in ((data.get(node) or {}).get(key) or [])}

        self.scene = fields("scene")
        self.nested = {
            "studio": fields("studio"),
            "performers": fields("performer"),
            "tags": fields("tag"),
            "groups": fields("group"),
            "movies": fields("movie"),
        }
        self.source_input = fields("source", "inputFields")
        self.scrape_input = fields("input", "inputFields")

        # Which spelling this version uses for the two renamed fields.
        self.url_key = "urls" if "urls" in self.scene else ("url" if "url" in self.scene else None)
        self.group_key = "groups" if "groups" in self.scene else ("movies" if "movies" in self.scene else None)

    def selection(self) -> str:
        parts = [f for f in SCALARS if f in self.scene] or ["title"]
        for key in ["studio"] + NAME_LISTS:
            if key not in self.scene:
                continue
            inner = "name" + (" stored_id" if "stored_id" in self.nested.get(key, set()) else "")
            parts.append(key + " { " + inner + " }")
        return "\n    ".join(parts)


def introspect(url: str, api_key: str | None) -> Schema:
    return Schema(graphql.try_call(url, INTROSPECT, api_key=api_key))


def scrape_query(selection: str) -> str:
    return (
        "query ScrapeAllProbe($source: ScraperSourceInput!, $input: ScrapeSingleSceneInput!) {\n"
        "  scrapeSingleScene(source: $source, input: $input) {\n    " + selection + "\n  }\n}"
    )


def search_term(url: str, api_key: str | None, scene_id: str, fragment: dict) -> str | None:
    """A query string for name-search scrapers: the scene title, else its filename."""
    title = (fragment.get("title") or "").strip()
    if title:
        return title

    for query in SCENE_TITLE_TIERS:
        data = graphql.try_call(url, query, {"id": scene_id}, api_key=api_key)
        if not data:
            continue
        scene = data.get("findScene") or {}
        title = (scene.get("title") or "").strip()
        if title:
            return title
        files = scene.get("files") or []
        raw = (files[0].get("basename") if files else None) or scene.get("path") or ""
        stem = Path(raw).stem.strip()
        if stem:
            return stem
        break
    return None


def existing_urls(url: str, api_key: str | None, scene_id: str, fragment: dict) -> list[str]:
    """URLs already on the scene - the base the merged list is added to."""
    found: list[str] = []
    for query in SCENE_URL_TIERS:
        data = graphql.try_call(url, query, {"id": scene_id}, api_key=api_key)
        scene = (data or {}).get("findScene")
        if scene is None:
            continue
        found = list(scene.get("urls") or [])
        if not found and scene.get("url"):
            found = [scene["url"]]
        break

    from_stdin = list(fragment.get("urls") or [])
    if not from_stdin and fragment.get("url"):
        from_stdin = [fragment["url"]]
    return [u for u in found + from_stdin if u]


def stash_boxes(url: str, api_key: str | None) -> list[dict]:
    data = graphql.try_call(url, STASH_BOXES, api_key=api_key)
    general = ((data or {}).get("configuration") or {}).get("general") or {}
    return general.get("stashBoxes") or []


# --------------------------------------------------------------------------- #
# scrape tags


def existing_scrape_tags(url: str, api_key: str | None) -> dict | None:
    """Lowercased tag name -> id for every existing scrape tag, or None on failure.

    None means the question could not be asked, which is not the same as "there
    are none" - creating tags blind would risk duplicates.
    """
    for query, needs_var in TAG_LOOKUP_TIERS:
        variables = {"v": settings.SCRAPE_TAG_PREFIX} if needs_var else None
        data = graphql.try_call(url, query, variables, api_key=api_key)
        tags = ((data or {}).get("findTags") or {}).get("tags")
        if tags is not None:
            return {(tag.get("name") or "").lower(): tag.get("id") for tag in tags}
    return None


def sync_scrape_tags(url: str, api_key: str | None, probed: dict) -> list[dict]:
    """Make sure a tag exists for every probed source; return those to attach.

    `probed` maps a source name to whether it identified the scene. A source that
    found nothing still gets its tag created - just not attached - so the tag list
    doubles as the roster of what is installed and what is worth ignoring.
    """
    if not probed:
        return []

    known = existing_scrape_tags(url, api_key)
    if known is None:
        log.error("TAGS  could not list existing tags - no scrape tag was created")
        return []

    attach, created, failed = [], 0, 0
    for name, hit in probed.items():
        tag_name = settings.scrape_tag(name)
        tag_id = known.get(tag_name.lower())

        if tag_id is None:
            data = graphql.try_call(url, TAG_CREATE, {"name": tag_name}, api_key=api_key)
            tag_id = ((data or {}).get("tagCreate") or {}).get("id")
            if tag_id:
                created += 1
                known[tag_name.lower()] = tag_id
                log.info("TAGS  created " + tag_name
                         + (" (attaching)" if hit else " (not attached - did not identify the scene)"))
            else:
                failed += 1
                log.error("TAGS  could not create " + tag_name)

        if hit:
            # Without an id Stash matches by name and offers to create on apply.
            attach.append({"name": tag_name, "stored_id": tag_id} if tag_id else {"name": tag_name})

    log.info("TAGS  " + str(len(probed)) + " source(s): " + str(created) + " tag(s) created, "
             + str(len(probed) - created - failed) + " already present, "
             + str(len(attach)) + " to attach")
    return attach


# --------------------------------------------------------------------------- #
# which sources to probe


def build_sources(scrapers, boxes, schema: Schema, scene_id: str, term: str | None, opts):
    """Every source that can scrape from the scene itself. URL-bound ones are out.

    A scraper supporting several non-URL modes gets one entry per mode, so the log
    shows which mode produced which result.
    """
    sources = []
    can_fragment = "scene_id" in schema.scrape_input or not schema.scrape_input
    can_search = "query" in schema.scrape_input or not schema.scrape_input

    for scraper in scrapers:
        spec = scraper.get("scene")
        if not isinstance(spec, dict):
            continue
        identifier, name = scraper["id"], scraper.get("name") or scraper["id"]
        if identifier == SELF_ID or name.lower() == SELF_ID.lower():
            continue  # never recurse into ourselves
        if opts.ignores(identifier, name):
            log.info("SKIP  " + name + " - ignored by the ScrapeAllSettings plugin")
            continue

        modes = set(spec.get("supported_scrapes") or [])
        if "FRAGMENT" in modes and can_fragment:
            sources.append({
                "label": name + " (fragment)", "name": name, "mode": "fragment",
                "source": {"scraper_id": identifier}, "input": {"scene_id": scene_id},
            })
        if "NAME" in modes and can_search and term:
            sources.append({
                "label": name + ' (name search "' + term + '")', "name": name, "mode": "name",
                "source": {"scraper_id": identifier}, "input": {"query": term},
            })

    # Stash-boxes are not in listScrapers but are exactly the fingerprint sources
    # this is about: they match on stash-id, phash, oshash and duration.
    box_key = "stash_box_endpoint" if "stash_box_endpoint" in schema.source_input else "stash_box_index"
    for index, box in enumerate(boxes):
        endpoint = box.get("endpoint") or ""
        # An unnamed box would otherwise carry a whole endpoint URL into its
        # scrape tag; its host reads far better in a tag list.
        name = (box.get("name") or "").strip() or host_of(endpoint) or "stash-box " + str(index)
        if opts.ignores(name, endpoint):
            log.info("SKIP  " + name + " - ignored by the ScrapeAllSettings plugin")
            continue
        sources.append({
            "label": name + " (stash-box fingerprints)", "name": name, "mode": "stash-box",
            "source": {box_key: endpoint if box_key == "stash_box_endpoint" else index},
            "input": {"scene_id": scene_id},
        })

    return sources


# --------------------------------------------------------------------------- #
# merging


def clip(value, limit: int = 90) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def url_key(raw: str) -> str:
    """Normalised form used only for "is this URL already here?"."""
    text = str(raw or "").strip().lower().rstrip("/")
    text = re.sub(r"^https?://", "", text)
    return re.sub(r"^www\.", "", text)


def entity_key(entry: dict) -> str:
    name = (entry.get("name") or "").strip().lower()
    return name or "id:" + str(entry.get("stored_id") or "")


class Merged:
    """One scene assembled from every source that answered.

    Scalars are first-wins in probe order. Lists accumulate. `studio` is a single
    field in the schema, so the most-agreed-on answer wins rather than the first.
    """

    def __init__(self, base_urls):
        self.scalars: dict[str, str] = {}
        self.urls: list[str] = []
        self.url_seen: set[str] = set()
        self.kept_existing = 0
        self.studio_votes: dict[str, list] = {}
        self.entities: dict[str, dict] = {"performers": {}, "tags": {}, "groups": {}}
        self.provenance: list[str] = []

        for raw in base_urls:
            if self.add_url(raw):
                self.kept_existing += 1

    def add_url(self, raw: str) -> bool:
        key = url_key(raw)
        if not key or key in self.url_seen:
            return False
        self.url_seen.add(key)
        self.urls.append(str(raw).strip())
        return True

    def add_tag(self, tag: dict) -> None:
        """Attach one tag that did not come from a scraped scene."""
        self.entities["tags"].setdefault(entity_key(tag), dict(tag))

    def add(self, mode: str, name: str, scene: dict, group_key: str | None) -> None:
        self.provenance.append(MODE_LETTERS.get(mode, "?") + ": " + name)

        for field in ("title", "code", "date", "director"):
            value = (scene.get(field) or "").strip() if isinstance(scene.get(field), str) else scene.get(field)
            if value and field not in self.scalars:
                self.scalars[field] = value

        for raw in scene.get("urls") or ([scene["url"]] if scene.get("url") else []):
            self.add_url(raw)

        studio = scene.get("studio")
        if isinstance(studio, dict) and (studio.get("name") or studio.get("stored_id")):
            slot = self.studio_votes.setdefault(entity_key(studio), [0, dict(studio), len(self.studio_votes)])
            slot[0] += 1
            if studio.get("stored_id") and not slot[1].get("stored_id"):
                # Keep the first spelling of the name, but adopt the link to an
                # existing Stash studio from whichever source knows it.
                slot[1]["stored_id"] = studio["stored_id"]

        for field in ("performers", "tags", "groups"):
            incoming = scene.get(field)
            if field == "groups" and not incoming and group_key == "movies":
                incoming = scene.get("movies")
            for entry in incoming or []:
                if not isinstance(entry, dict) or not (entry.get("name") or entry.get("stored_id")):
                    continue
                bucket = self.entities[field]
                key = entity_key(entry)
                if key not in bucket:
                    bucket[key] = dict(entry)
                elif entry.get("stored_id") and not bucket[key].get("stored_id"):
                    bucket[key]["stored_id"] = entry["stored_id"]  # same entity, keep first name

    def studio(self) -> dict | None:
        if not self.studio_votes:
            return None
        count, studio, _ = max(self.studio_votes.values(), key=lambda slot: (slot[0], -slot[2]))
        return studio

    def studio_report(self) -> list[str]:
        rows = sorted(self.studio_votes.values(), key=lambda slot: (-slot[0], slot[2]))
        return [str(slot[0]) + "x " + (slot[1].get("name") or "?") for slot in rows]

    def payload(self, schema: Schema, opts) -> dict:
        """The scraped scene to hand back, minus anything the settings exclude."""
        out: dict = {}

        def keep(field):
            return opts.permits(field) and field in schema.scene

        for field, value in self.scalars.items():
            if keep(field):
                out[field] = value

        if self.urls and schema.url_key and opts.permits("urls"):
            if schema.url_key == "urls":
                out["urls"] = self.urls
            else:
                out["url"] = self.urls[0]  # pre-0.24 holds a single URL

        studio = self.studio()
        if studio and keep("studio"):
            out["studio"] = trim_entity(studio)

        for field in ("performers", "tags"):
            entries = list(self.entities[field].values())
            if entries and keep(field):
                out[field] = [trim_entity(e) for e in entries]

        groups = list(self.entities["groups"].values())
        if groups and schema.group_key and opts.permits("groups"):
            out[schema.group_key] = [trim_entity(e) for e in groups]

        if self.provenance and opts.permits("details") and "details" in schema.scene:
            out["details"] = "\n".join(self.provenance)

        return out


def trim_entity(entry: dict) -> dict:
    """Only name and stored_id: everything else was never requested."""
    out = {"name": entry.get("name") or ""}
    if entry.get("stored_id"):
        out["stored_id"] = entry["stored_id"]
    return out


def summarise(scene: dict) -> str:
    bits = []
    for key in ("title", "date", "code", "director", "remote_site_id"):
        value = scene.get(key)
        if value:
            bits.append(key + '="' + clip(value) + '"')

    studio = (scene.get("studio") or {}).get("name") if isinstance(scene.get("studio"), dict) else None
    if studio:
        bits.append('studio="' + clip(studio) + '"')

    for key in NAME_LISTS:
        entries = scene.get(key)
        if entries:
            bits.append(key + "=" + str(len(entries)))

    urls = scene.get("urls") or ([scene["url"]] if scene.get("url") else [])
    if urls:
        bits.append("url=" + clip(urls[0], 60) + (" +" + str(len(urls) - 1) if len(urls) > 1 else ""))

    if scene.get("details"):
        bits.append("details=" + str(len(scene["details"])) + "ch")

    return "  ".join(bits) or "(result carried no usable fields)"


def elapsed(started: float) -> str:
    return format(time.monotonic() - started, ".1f") + "s"


def probe(url, api_key, sources, schema: Schema, merged: Merged) -> dict:
    """Returns {source name: did it identify the scene}, in probe order.

    Every source is registered up front, so a source the budget never reached
    still appears - the scrape-tag roster is about what is installed, not about
    what happened to run.
    """
    query = scrape_query(schema.selection())
    deadline = time.monotonic() + TOTAL_BUDGET
    total = len(sources)
    hits = misses = failures = 0
    probed = {}
    for source in sources:
        probed.setdefault(source["name"], False)

    log.info("")
    header = "== PROBE: " + str(total) + " non-URL source(s) "
    log.info(header + "=" * max(0, WIDTH - len(header)))

    for index, source in enumerate(sources, 1):
        if time.monotonic() > deadline:
            log.error("BUDGET exhausted: " + str(TOTAL_BUDGET) + "s spent - "
                      + str(total - index + 1) + " source(s) not tried")
            break

        label = "[" + str(index) + "/" + str(total) + "] " + source["label"]
        started = time.monotonic()
        try:
            data = graphql.call(
                url, query, {"source": source["source"], "input": source["input"]},
                api_key, timeout=PER_SOURCE_TIMEOUT,
            )
        except graphql.GraphQLError as exc:
            failures += 1
            log.error("FAIL  " + label + " after " + elapsed(started) + ": " + clip(exc, 200))
            continue

        results = data.get("scrapeSingleScene") or []
        if not results:
            misses += 1
            log.error("MISS  " + label + " after " + elapsed(started))
            continue

        hits += 1
        # Only the first result feeds the merge. A name search is ordered by the
        # scraper's own relevance, and folding a dozen candidates into one scene
        # would mix unrelated releases together.
        suffix = "" if len(results) == 1 else " (" + str(len(results)) + " results, merging the first)"
        log.info("FOUND " + label + " after " + elapsed(started) + suffix)
        for result in results[:MAX_RESULTS_LOGGED]:
            log.info("        - " + summarise(result))
        if len(results) > MAX_RESULTS_LOGGED:
            log.info("        - ... " + str(len(results) - MAX_RESULTS_LOGGED) + " more not listed")

        merged.add(source["mode"], source["name"], results[0], schema.group_key)
        probed[source["name"]] = True

    log.info("")
    log.info("-" * WIDTH)
    log.info("PROBE DONE: found " + str(hits) + ", missed " + str(misses)
             + ", failed " + str(failures) + " of " + str(total))
    return probed


def report_merge(merged: Merged, payload: dict, opts) -> None:
    log.info("")
    header = "== MERGE "
    log.info(header + "=" * max(0, WIDTH - len(header)))

    if not merged.provenance:
        log.info("no source identified this scene - returning nothing")
        return

    log.info("identified by: " + ", ".join(merged.provenance))
    log.info("urls         : " + str(len(merged.urls)) + " total ("
             + str(merged.kept_existing) + " already on the scene, "
             + str(len(merged.urls) - merged.kept_existing) + " new)")
    if merged.studio_votes:
        log.info("studio votes : " + ", ".join(merged.studio_report()))
    for field in ("performers", "tags", "groups"):
        if merged.entities[field]:
            names = [e.get("name") or "?" for e in merged.entities[field].values()]
            log.info(field.ljust(13) + ": " + str(len(names)) + " - " + clip(", ".join(names), 160))

    dropped = sorted(set(settings.FIELDS) - set(payload) - {"urls", "groups"}) if opts.allowed is not None else []
    if opts.allowed is not None:
        log.info("settings     : whitelist " + ", ".join(sorted(opts.allowed)))
        if dropped:
            log.debug("not returned : " + ", ".join(dropped))

    log.info("returning    : " + (", ".join(sorted(payload)) if payload else "nothing"))


# --------------------------------------------------------------------------- #


def read_fragment() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.debug("stdin was not JSON - ignoring")
        return {}


def run(mode: str, fragment: dict) -> dict | None:
    url, api_key, origin = config.load(__file__)
    log.debug("stash endpoint " + url + " (from " + origin + "), api key " + ("set" if api_key else "absent"))

    inventory = from_graphql(url, api_key)
    if inventory is None:
        scrapers, source = from_disk()
        log.info(render(scrapers, source))
        log.error("GraphQL unreachable - inventory came off disk and no scraper could be probed. "
                  "See README.md for config.ini.")
        return None

    scrapers, how = inventory
    log.info(render(scrapers, url + " via " + how))

    scene_id = str(fragment.get("id") or "").strip()
    if not scene_id:
        log.error("no scene id on stdin (" + mode + ") - inventory only, nothing to probe against")
        return None

    opts = settings.parse(settings.fetch(url, api_key))
    for note in opts.notes:
        log.warning("settings: " + note)
    for problem in opts.errors:
        log.error("settings: " + problem)

    schema = introspect(url, api_key)
    if not schema.scene:
        log.warning("could not introspect ScrapedScene - falling back to a minimal field set")
    if schema.url_key == "url":
        log.warning("this Stash exposes a single scene url, not a list - only the first URL can be returned")

    term = search_term(url, api_key, scene_id, fragment)
    if not term:
        log.debug("no title or filename for this scene - name-search scrapers skipped")

    boxes = stash_boxes(url, api_key)
    sources = build_sources(scrapers, boxes, schema, scene_id, term, opts)
    log.debug("URL-only scrapers are skipped by design; " + str(len(boxes)) + " stash-box(es) configured")
    if not sources:
        log.error("no non-URL source can scrape scene " + scene_id)
        return None

    merged = Merged(existing_urls(url, api_key, scene_id, fragment))
    probed = probe(url, api_key, sources, schema, merged)

    if opts.create_tags:
        log.info("")
        attach = sync_scrape_tags(url, api_key, probed)
        if attach and opts.permits("tags"):
            for tag in attach:
                merged.add_tag(tag)
        elif attach:
            log.info("TAGS  created but not attached - \"tags\" is not in the allowed fields")

    payload = merged.payload(schema, opts)
    report_merge(merged, payload, opts)
    return payload or None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "scene-fragment"
    fragment = read_fragment()
    log.info("ScrapeAll (" + mode + ") on scene id=" + str(fragment.get("id") or "?"))

    payload = None
    try:
        payload = run(mode, fragment)
    except Exception as exc:  # a crashed scraper shows the user nothing actionable
        log.error("ScrapeAll failed: " + repr(exc))

    # A result opens the edit panel's normal merge dialog, where nothing is saved
    # until the user applies it. Identify, by contrast, writes straight away.
    print(json.dumps(payload, ensure_ascii=False) if payload else "null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
