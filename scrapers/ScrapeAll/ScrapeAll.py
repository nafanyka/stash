#!/usr/bin/env python3
"""ScrapeAll - run every non-URL scene scraper against one scene and log what came back.

Registered as a scene fragment scraper, so it shows up both in the scene edit
panel ("Scrape with...") and as an Identify source. Two phases, both log-only:

1. inventory - every scraper Stash has loaded, grouped by category
2. probe - for each source that can work from the scene itself (fragment
   scraping: stash-id, phash and other fingerprints, plus name search), call
   scrapeSingleScene and log the result. Hits at Info, misses at Error.

URL-bound scrapers are skipped: they need a URL on the scene, not the scene.

Nothing is written. The scraper returns an empty result, so no scene field can be
modified from the edit panel or from a batch Identify run. Read Settings -> Logs.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config, graphql, log  # noqa: E402

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


def domains_of(urls, limit: int = 4) -> str:
    hosts: list[str] = []
    for raw in urls or []:
        host = re.sub(r"^\w+://", "", (raw or "").strip()).split("/")[0]
        if host and host not in hosts:
            hosts.append(host)
    if not hosts:
        return ""
    shown = ", ".join(hosts[:limit])
    return shown + (", +" + str(len(hosts) - limit) + " more" if len(hosts) > limit else "")


def render(scrapers: list[dict], source: str) -> str:
    lines = [
        "ScrapeAll - installed scraper inventory (log only, nothing written).",
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
# probing: what to ask for

# Which ScrapedScene fields exist varies by version (`url` became `urls`, `code`
# and `director` are newer, `movies` became `groups`), and asking for one that is
# gone fails the whole query. Introspect once and select the intersection.
INTROSPECT = """
query {
  scene: __type(name: "ScrapedScene") { fields { name } }
  source: __type(name: "ScraperSourceInput") { inputFields { name } }
  input: __type(name: "ScrapeSingleSceneInput") { inputFields { name } }
}
"""

SCALAR_WISHLIST = ["title", "code", "details", "director", "date", "url", "urls", "remote_site_id"]
NAME_LISTS = ["performers", "tags", "groups", "movies"]

SCENE_TITLE_TIERS = [
    "query($id: ID!) { findScene(id: $id) { title files { basename } } }",
    "query($id: ID!) { findScene(id: $id) { title path } }",
    "query($id: ID!) { findScene(id: $id) { title } }",
]

STASH_BOXES = "query { configuration { general { stashBoxes { endpoint name } } } }"


def introspect(url: str, api_key: str | None):
    data = graphql.try_call(url, INTROSPECT, api_key=api_key)
    if not data:
        return set(), set(), set()

    def names(node, key):
        return {f["name"] for f in ((data.get(node) or {}).get(key) or [])}

    return names("scene", "fields"), names("source", "inputFields"), names("input", "inputFields")


def scene_selection(scene_fields: set) -> str:
    parts = [f for f in SCALAR_WISHLIST if f in scene_fields] or ["title"]
    if "studio" in scene_fields:
        parts.append("studio { name }")
    for name in NAME_LISTS:
        if name in scene_fields:
            parts.append(name + " { name }")
    return "\n    ".join(parts)


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


def stash_boxes(url: str, api_key: str | None) -> list[dict]:
    data = graphql.try_call(url, STASH_BOXES, api_key=api_key)
    general = ((data or {}).get("configuration") or {}).get("general") or {}
    return general.get("stashBoxes") or []


def build_sources(scrapers, boxes, source_fields, input_fields, scene_id, term):
    """Every source that can scrape from the scene itself. URL-bound ones are out.

    A scraper supporting several non-URL modes gets one entry per mode, so the log
    shows which mode produced which result.
    """
    sources = []
    can_fragment = "scene_id" in input_fields or not input_fields
    can_search = "query" in input_fields or not input_fields

    for scraper in scrapers:
        spec = scraper.get("scene")
        if not isinstance(spec, dict):
            continue
        identifier, name = scraper["id"], scraper.get("name") or scraper["id"]
        if identifier == SELF_ID or name.lower() == SELF_ID.lower():
            continue  # never recurse into ourselves

        modes = set(spec.get("supported_scrapes") or [])
        if "FRAGMENT" in modes and can_fragment:
            sources.append({
                "label": name + " (fragment)",
                "source": {"scraper_id": identifier},
                "input": {"scene_id": scene_id},
            })
        if "NAME" in modes and can_search and term:
            sources.append({
                "label": name + ' (name search "' + term + '")',
                "source": {"scraper_id": identifier},
                "input": {"query": term},
            })

    # Stash-boxes are not in listScrapers but are exactly the fingerprint sources
    # the ask is about: they match on stash-id, phash, oshash and duration.
    box_key = "stash_box_endpoint" if "stash_box_endpoint" in source_fields else "stash_box_index"
    for index, box in enumerate(boxes):
        endpoint = box.get("endpoint") or ""
        value = endpoint if box_key == "stash_box_endpoint" else index
        sources.append({
            "label": (box.get("name") or endpoint or "stash-box " + str(index)) + " (stash-box fingerprints)",
            "source": {box_key: value},
            "input": {"scene_id": scene_id},
        })

    return sources


# --------------------------------------------------------------------------- #
# probing: running and reporting


def clip(value, limit: int = 90) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def probe(url: str, api_key: str | None, sources: list[dict], selection: str) -> None:
    query = scrape_query(selection)
    deadline = time.monotonic() + TOTAL_BUDGET
    total = len(sources)
    hits = misses = failures = 0

    log.info("")
    header = "== PROBE: " + str(total) + " non-URL source(s) "
    log.info(header + "=" * max(0, WIDTH - len(header)))

    for index, source in enumerate(sources, 1):
        if time.monotonic() > deadline:
            log.error("BUDGET exhausted: " + str(TOTAL_BUDGET) + "s spent - " + str(total - index + 1) + " source(s) not tried")
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
        suffix = "" if len(results) == 1 else " (" + str(len(results)) + " results)"
        log.info("FOUND " + label + " after " + elapsed(started) + suffix)
        for result in results[:MAX_RESULTS_LOGGED]:
            log.info("        - " + summarise(result))
        if len(results) > MAX_RESULTS_LOGGED:
            log.info("        - ... " + str(len(results) - MAX_RESULTS_LOGGED) + " more not listed")

    log.info("")
    tally = "found " + str(hits) + ", missed " + str(misses) + ", failed " + str(failures) + " of " + str(total)
    log.info("-" * WIDTH)
    log.info("PROBE DONE: " + tally + ". Nothing was written to the scene.")


def elapsed(started: float) -> str:
    return format(time.monotonic() - started, ".1f") + "s"


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


def run(mode: str, fragment: dict) -> None:
    url, api_key, origin = config.load(__file__)
    log.debug("stash endpoint " + url + " (from " + origin + "), api key " + ("set" if api_key else "absent"))

    inventory = from_graphql(url, api_key)
    if inventory is None:
        scrapers, source = from_disk()
        log.info(render(scrapers, source))
        log.error("GraphQL unreachable - inventory came off disk and no scraper could be probed. "
                  "See README.md for config.ini.")
        return

    scrapers, how = inventory
    log.info(render(scrapers, url + " via " + how))

    scene_id = str(fragment.get("id") or "").strip()
    if not scene_id:
        log.error("no scene id on stdin (" + mode + ") - inventory only, nothing to probe against")
        return

    scene_fields, source_fields, input_fields = introspect(url, api_key)
    term = search_term(url, api_key, scene_id, fragment)
    boxes = stash_boxes(url, api_key)
    sources = build_sources(scrapers, boxes, source_fields, input_fields, scene_id, term)

    if not term:
        log.debug("no title or filename for this scene - name-search scrapers skipped")
    log.debug("URL-only scrapers are skipped by design; " + str(len(boxes)) + " stash-box(es) configured")

    if not sources:
        log.error("no non-URL source can scrape scene " + scene_id)
        return
    probe(url, api_key, sources, scene_selection(scene_fields))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "scene-fragment"
    fragment = read_fragment()
    log.info("ScrapeAll (" + mode + ") on scene id=" + str(fragment.get("id") or "?"))

    try:
        run(mode, fragment)
    except Exception as exc:  # a crashed scraper shows the user nothing actionable
        log.error("ScrapeAll failed: " + repr(exc))

    # Read-only by design: an empty result is the only way to guarantee no scene
    # field can be touched, from the edit panel or from a batch Identify run.
    # Stash reads a null result as "nothing found".
    print("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
