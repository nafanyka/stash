"""What is installed, what each scraper can do, and which of them a URL belongs to.

The URL match rule here is Stash's own, copied deliberately rather than approximated:
`pkg/scraper/definition.go` matches with `strings.Contains(url, pattern)` - a plain
substring test, not a prefix or a host test. Anything looser would claim scrapers
Stash would not use; anything stricter would miss ones it would.

That matters because of the limitation this module exists to work around: Stash's
`scrapeSceneURL` picks the handler itself, by iterating a Go map, so with several
matching scrapers the winner is non-deterministic and unreported. Computing the
matching set with the same rule is the only way to say honestly which scraper produced
a URL result - or to admit that we cannot.
"""

from __future__ import annotations

import hashlib
import json

from . import settings

FRAGMENT = "FRAGMENT"
NAME = "NAME"
URL = "URL"

# Attempt methods, in the order a scan prefers them.
M_URL = "URL"
M_STASHBOX_FP = "STASHBOX_FP"
M_STASHBOX_QUERY = "STASHBOX_QUERY"
M_FRAGMENT_SCENE = "FRAGMENT_SCENE"
M_FRAGMENT_INPUT = "FRAGMENT_INPUT"
M_NAME = "NAME"

CERTAIN = "CERTAIN"
AMBIGUOUS = "AMBIGUOUS"


def fingerprint_of(scraper) -> str:
    """A stand-in for the version a scraper config does not have.

    Name, capabilities and URL patterns are everything Stash exposes about a scraper,
    so a change to any of them is the only signal available that the scraper is not
    the one we tried last time. Coarse - an edit to a scraper's parsing rules is
    invisible here - but it never claims a scraper is new when it is not.
    """
    # JSON rather than a joined string, so no separator can be confused with content:
    # a pattern containing the separator must not be able to fake a different scraper.
    parts = json.dumps([
        str(scraper.get("id") or ""),
        str(scraper.get("name") or ""),
        sorted(scraper.get("kinds") or []),
        sorted(str(one) for one in (scraper.get("url_patterns") or [])),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:32]


def from_list_scrapers(rows):
    """`listScrapers(types:[SCENE])` rows as our own scraper records."""
    out = []
    for row in rows or []:
        scene = row.get("scene") or {}
        record = {
            "id": row.get("id") or "",
            "name": row.get("name") or row.get("id") or "",
            "kinds": sorted(set(scene.get("supported_scrapes") or [])),
            "url_patterns": [str(one) for one in (scene.get("urls") or []) if one],
        }
        if not record["id"]:
            continue
        record["fingerprint"] = fingerprint_of(record)
        out.append(record)
    out.sort(key=lambda entry: entry["id"].lower())
    return out


class Registry:
    """The installed scene scrapers, with the config applied on top."""

    def __init__(self, scrapers, config: settings.Config, stash_boxes=None):
        self.config = config
        self.scrapers = list(scrapers)
        self.by_id = {entry["id"]: entry for entry in self.scrapers}
        self.stash_boxes = list(stash_boxes or [])

    # -- capability views --------------------------------------------------

    def enabled(self):
        return [entry for entry in self.scrapers
                if self.config.is_enabled(entry["id"], entry["name"])]

    def with_kind(self, kind):
        return [entry for entry in self.enabled() if kind in entry["kinds"]]

    def handlers_for_url(self, url):
        """Every enabled scraper Stash would consider for this URL.

        Same rule as Stash: a scraper matches when any of its patterns appears as a
        substring of the URL. Patterns are compared case-insensitively against a
        lowercased URL, which is how the URLs and patterns in the community index are
        written, and avoids missing a match on a host the scraper spelled in capitals.
        """
        if not url:
            return []
        needle = str(url).lower()
        found = []
        for entry in self.enabled():
            for pattern in entry["url_patterns"]:
                if pattern and str(pattern).lower() in needle:
                    found.append(entry)
                    break
        found.sort(key=lambda entry: (settings.PRIORITY_ORDER.get(
            self.config.priority(entry["id"]), 1), entry["id"].lower()))
        return found

    def hosts_of(self, scraper):
        """The hosts a scraper's patterns mention, for domain routing."""
        hosts = set()
        for pattern in scraper.get("url_patterns") or []:
            text = str(pattern).lower()
            for prefix in ("https://", "http://"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
            if text.startswith("www."):
                text = text[4:]
            host = text.split("/", 1)[0]
            if host:
                hosts.add(host)
        return hosts

    def routed_for_hosts(self, hosts):
        """Scrapers whose patterns mention one of these hosts.

        Used to try the scrapers that own a scene's known sites before the rest. Not a
        restriction: a scan without a stop condition still reaches everything else.
        """
        wanted = {str(host).lower() for host in hosts if host}
        if not wanted:
            return []
        out = []
        for entry in self.enabled():
            entry_hosts = self.hosts_of(entry)
            if any(host in entry_hosts or any(host.endswith("." + candidate) or
                                              candidate.endswith("." + host)
                                              for candidate in entry_hosts)
                   for host in wanted):
                out.append(entry)
        return out

    # -- planning ----------------------------------------------------------

    def plan(self, snapshot, mode=settings.NORMAL, already_tried=None):
        """The ordered work list for one scene.

        Ordering is the cheap, likely-right work first (a URL the scene already has,
        a fingerprint lookup, then the scrapers that own the scene's sites), then the
        broad sweep. A Normal Scan may stop early, so what comes first decides what a
        user actually gets; a Deep Scan runs the same list to the end.

        `already_tried` maps scraper id to the fingerprint last used against this
        scene. Entries are not dropped from the plan because of it - the cache decides
        that, per attempt, with TTLs - but a scraper that has never been tried in its
        current form is marked `is_new`, which is what the new-scraper task selects on.
        """
        deep = mode == settings.DEEP
        already_tried = already_tried or {}
        term = (snapshot or {}).get("search_term") or ""
        scene_urls = [entry for entry in (snapshot or {}).get("urls") or []]
        scene_hosts = {entry.get("host") for entry in scene_urls if entry.get("host")}

        routed_ids = {entry["id"] for entry in self.routed_for_hosts(scene_hosts)}
        work = []

        def is_new(scraper):
            previous = already_tried.get(scraper["id"])
            return previous is None or previous != scraper["fingerprint"]

        # 1. the scene's own URLs. Stash routes these itself, so the record carries
        #    the handler set we computed and the attribution that follows from it.
        for entry in scene_urls:
            handlers = self.handlers_for_url(entry.get("url"))
            if not handlers:
                continue
            work.append({
                "method": M_URL,
                "target": entry["url"],
                "url_key": entry.get("key") or entry["url"],
                "host": entry.get("host") or "",
                "handlers": [handler["id"] for handler in handlers],
                "scraper": handlers[0] if len(handlers) == 1 else None,
                "attribution": CERTAIN if len(handlers) == 1 else AMBIGUOUS,
                "depth": 0,
                "band": 0,
                "is_new": any(is_new(handler) for handler in handlers),
            })

        # 2. stash-boxes by fingerprint. The strongest identifier available, and cheap.
        for box in self.stash_boxes:
            if self.config.is_enabled(box.get("endpoint", ""), box.get("name", "")):
                work.append({
                    "method": M_STASHBOX_FP,
                    "target": box["endpoint"],
                    "scraper": {"id": box["endpoint"], "name": box.get("name") or
                                box["endpoint"], "fingerprint": "stashbox"},
                    "attribution": CERTAIN,
                    "depth": 0,
                    "band": 1,
                    "is_new": False,
                })

        # 3. fragment scrapers, high priority and site-owning ones first.
        for entry in self.with_kind(FRAGMENT):
            priority = self.config.priority(entry["id"])
            routed = entry["id"] in routed_ids
            if not deep and not routed and priority != settings.PRIORITY_HIGH \
                    and not self.config["normalIncludesUnroutedFragments"]:
                continue
            band = 2 if priority == settings.PRIORITY_HIGH else (
                3 if routed else 5 if priority == settings.PRIORITY_LOW else 4)
            work.append({
                "method": M_FRAGMENT_SCENE,
                "target": "",
                "scraper": entry,
                "attribution": CERTAIN,
                "depth": 0,
                "band": band,
                "routed": routed,
                "is_new": is_new(entry),
            })

        # 4. name search. Slowest kind of attempt by a wide margin, and the one that
        #    answers with a dozen near-misses, so a Normal Scan leaves it out unless
        #    asked. Needs something to search for at all.
        if term and (deep or self.config["normalIncludesNameScrapers"]):
            for box in self.stash_boxes:
                if self.config.is_enabled(box.get("endpoint", ""), box.get("name", "")):
                    work.append({
                        "method": M_STASHBOX_QUERY, "target": term,
                        "scraper": {"id": box["endpoint"],
                                    "name": box.get("name") or box["endpoint"],
                                    "fingerprint": "stashbox"},
                        "attribution": CERTAIN, "depth": 0, "band": 6, "is_new": False,
                    })
            for entry in self.with_kind(NAME):
                priority = self.config.priority(entry["id"])
                band = 6 if priority == settings.PRIORITY_HIGH else (
                    8 if priority == settings.PRIORITY_LOW else 7)
                work.append({
                    "method": M_NAME, "target": term, "scraper": entry,
                    "attribution": CERTAIN, "depth": 0, "band": band,
                    "is_new": is_new(entry),
                })

        work.sort(key=lambda item: (item["band"], str((item.get("scraper") or {}).get("id")
                                                      or item.get("target") or "").lower()))
        for index, item in enumerate(work):
            item["order"] = index
            item["target_key"] = target_key(item)
        return work

    def url_work(self, url_record, depth, parent_attempt_id=None):
        """The attempt for one discovered URL, or None when nothing handles it."""
        handlers = self.handlers_for_url(url_record.get("url"))
        if not handlers:
            return None
        item = {
            "method": M_URL,
            "target": url_record["url"],
            "url_key": url_record.get("key") or url_record["url"],
            "host": url_record.get("host") or "",
            "handlers": [handler["id"] for handler in handlers],
            "scraper": handlers[0] if len(handlers) == 1 else None,
            "attribution": CERTAIN if len(handlers) == 1 else AMBIGUOUS,
            "depth": int(depth),
            "band": 0,
            "parent_id": parent_attempt_id,
        }
        item["target_key"] = target_key(item)
        return item


def target_key(item) -> str:
    """The identity of an attempt: what was asked, of whom.

    Used both to keep a scan from repeating itself and as the cache key across scans,
    so it must not include anything that varies between runs. For a URL attempt the
    scraper is deliberately left out: Stash chooses it, and it may choose differently
    next time, so the URL alone is the thing being asked about.
    """
    method = item["method"]
    if method == M_URL:
        return "URL|" + str(item.get("url_key") or item.get("target") or "")
    scraper_id = str((item.get("scraper") or {}).get("id") or "")
    if method in (M_NAME, M_STASHBOX_QUERY):
        from . import normalize
        return "%s|%s|%s" % (method, scraper_id, normalize.canon_text(item.get("target")))
    if method == M_FRAGMENT_INPUT:
        return "%s|%s|%s" % (method, scraper_id, item.get("url_key") or item.get("target") or "")
    return "%s|%s|" % (method, scraper_id)


def source_for(item):
    """The `ScraperSourceInput` for an attempt, or None for a URL scrape."""
    method = item["method"]
    if method == M_URL:
        return None
    scraper = item.get("scraper") or {}
    if method in (M_STASHBOX_FP, M_STASHBOX_QUERY):
        return {"stash_box_endpoint": scraper.get("id")}
    return {"scraper_id": scraper.get("id")}


def scrape_input_for(item, scene_id, snapshot=None):
    """The `ScrapeSingleSceneInput` for an attempt.

    Exactly one of scene_id, scene_input or query is set: Stash's resolver checks them
    in that order and silently ignores the rest, so sending two would hide which one
    was used.
    """
    method = item["method"]
    if method in (M_FRAGMENT_SCENE, M_STASHBOX_FP):
        return {"scene_id": str(scene_id)}
    if method in (M_NAME, M_STASHBOX_QUERY):
        return {"query": str(item.get("target") or "")}
    if method == M_FRAGMENT_INPUT:
        snapshot = snapshot or {}
        payload = {"url": item.get("target")}
        for key in ("title", "code", "details", "director", "date"):
            value = snapshot.get(key)
            if value:
                payload[key] = value
        return {"scene_input": payload}
    raise ValueError("no scrape input for method %r" % method)
