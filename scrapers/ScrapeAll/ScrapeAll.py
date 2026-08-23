#!/usr/bin/env python3
"""ScrapeAll - inventory of every scraper Stash has loaded, grouped by category.

Registered as a scene fragment scraper, so it shows up both in the scene edit
panel ("Scrape with...") and as an Identify source. It scrapes nothing remote and
writes nothing: it asks the local Stash server which scrapers exist, logs the
answer, and returns an empty result so no scene can be modified.

Read the report in Settings -> Logs. See README.md.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config, graphql, log  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage
    except (AttributeError, OSError):
        pass

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
# collecting


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


def collect():
    url, api_key, origin = config.load(__file__)
    log.debug("stash endpoint " + url + " (from " + origin + "), api key " + ("set" if api_key else "absent"))

    result = from_graphql(url, api_key)
    if result:
        scrapers, how = result
        return scrapers, url + " via " + how

    log.warning("GraphQL unavailable - falling back to a disk scan (see README.md for config.ini)")
    return from_disk()


# --------------------------------------------------------------------------- #
# rendering


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


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "scene-fragment"
    fragment = read_fragment()
    log.info("ScrapeAll (" + mode + ") on scene id=" + str(fragment.get("id") or "?"))

    try:
        scrapers, source = collect()
    except Exception as exc:  # a crashed scraper shows the user nothing actionable
        log.error("ScrapeAll failed: " + repr(exc))
        print("null")
        return 0

    log.info(render(scrapers, source))

    # Read-only by design: an empty result is the only way to guarantee no scene
    # field can be touched, from the edit panel or from a batch Identify run.
    # Stash reads a null result as "nothing found".
    print("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
