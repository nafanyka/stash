"""Which sources exist, and which of them can answer for a given URL.

Two jobs:

* hold the installed scene scrapers and the configured stash-boxes;
* decide, for one URL, exactly which scrapers Stash would consider - using Stash's own
  rule, `strings.Contains(url, pattern)` (`pkg/scraper/definition.go`). A plain
  substring test, not a prefix and not a host test. Anything looser would claim
  scrapers Stash would not use; anything stricter would miss ones it would.

That second job exists because of the limitation the whole URL stage is shaped around:
`scrapeSceneURL` picks the handler itself by iterating a Go map, so with several
matching scrapers the winner is non-deterministic and unreported. Computing the set
ourselves is the only way to say honestly which scraper produced a result - or to admit
that we cannot, and to reach the others another way.
"""

from __future__ import annotations

FRAGMENT = "FRAGMENT"
NAME = "NAME"
URL = "URL"

# How a source was invoked. The method is part of a source's identity, because the same
# scraper asked two different ways is two different answers.
M_STASHBOX_FP = "STASHBOX_FP"        # scrapeSingleScene(stash_box_endpoint, scene_id)
M_STASHBOX_QUERY = "STASHBOX_QUERY"  # scrapeSingleScene(stash_box_endpoint, query)
M_URL = "URL"                        # scrapeSceneURL(url)
M_URL_FRAGMENT = "URL_FRAGMENT"      # scrapeSingleScene(scraper_id, scene_input:{url})

# How sure we are about who answered.
CERTAIN = "CERTAIN"
AMBIGUOUS = "AMBIGUOUS"


def from_list_scrapers(rows):
    """`listScrapers(types:[SCENE])` rows as our own records."""
    out = []
    for row in rows or []:
        scene = row.get("scene") or {}
        record = {
            "id": row.get("id") or "",
            "name": row.get("name") or row.get("id") or "",
            "kinds": sorted(set(scene.get("supported_scrapes") or [])),
            "url_patterns": [str(one) for one in (scene.get("urls") or []) if one],
        }
        if record["id"]:
            out.append(record)
    out.sort(key=lambda entry: entry["id"].lower())
    return out


class Registry:
    """The installed scrapers and configured stash-boxes, with the config applied."""

    def __init__(self, scrapers, stash_boxes, config):
        self.config = config
        self.scrapers = [entry for entry in (scrapers or [])
                         if config.may_invoke(entry.get("id"), entry.get("name"))]
        self.by_id = {entry["id"]: entry for entry in self.scrapers}
        self.stash_boxes = [box for box in (stash_boxes or []) if box.get("endpoint")]

    # -- stash-boxes -------------------------------------------------------

    def box_sources(self, search_term=None):
        """One source per configured stash-box, plus a name search if asked for.

        Every box, every time. FastDiscovery never stops after the first box that
        matches - that is the single most important difference from Stash's own
        Identify, which returns as soon as a source answers (requirement 3).
        """
        out = []
        for box in self.stash_boxes:
            out.append({
                "type": "stashbox",
                "method": M_STASHBOX_FP,
                "name": box.get("name") or box["endpoint"],
                "endpoint": box["endpoint"],
                "attribution": CERTAIN,
                "depth": 0,
            })
        if search_term and self.config["stashboxNameSearch"]:
            for box in self.stash_boxes:
                out.append({
                    "type": "stashbox",
                    "method": M_STASHBOX_QUERY,
                    "name": (box.get("name") or box["endpoint"]) + " (search)",
                    "endpoint": box["endpoint"],
                    "target": str(search_term),
                    "attribution": CERTAIN,
                    "depth": 0,
                })
        return out

    # -- url routing -------------------------------------------------------

    def handlers_for(self, url):
        """Every installed scraper Stash would consider for this URL.

        Patterns are compared case-insensitively against a lowercased URL: that is how
        the community index writes both, and it avoids missing a host a scraper spelled
        in capitals.
        """
        if not url:
            return []
        needle = str(url).lower()
        found = []
        for entry in self.scrapers:
            for pattern in entry["url_patterns"]:
                if pattern and str(pattern).lower() in needle:
                    found.append(entry)
                    break
        found.sort(key=lambda entry: entry["id"].lower())
        return found

    def url_sources(self, url_record, depth, parent_source_id=None):
        """Every source to run for one URL, and every scraper we could not reach.

        Returns (sources, unreachable). The shape of this function *is* the workaround
        for the API limitation, so it is worth stating plainly:

        * one matching scraper  -> `scrapeSceneURL`, and the answer is certainly its
          answer, because Stash had nothing else to choose;
        * several matching      -> `scrapeSceneURL` still runs, but its result can only
          be attributed to "one of these", and is recorded that way. On top of it,
          every matching scraper that also declares FRAGMENT is called directly with
          the URL as a synthetic fragment, which does reach that specific scraper and
          gives it a column of its own;
        * a matching scraper that is URL-only and lost the ambiguous draw cannot be
          reached at all through any public Stash API. It is returned in `unreachable`
          so the run records it and the UI can say so, rather than pretending the URL
          was fully covered.
        """
        url = url_record["url"]
        handlers = self.handlers_for(url)
        if not handlers:
            return [], []

        certain = len(handlers) == 1
        base = {
            "type": "url_scraper",
            "method": M_URL,
            "url": url,
            "url_key": url_record["key"],
            "host": url_record.get("host") or "",
            "depth": int(depth),
            "parent_source_id": parent_source_id,
            "handlers": [entry["id"] for entry in handlers],
        }
        if certain:
            base.update({"scraper_id": handlers[0]["id"], "name": handlers[0]["name"],
                         "attribution": CERTAIN})
        else:
            base.update({"scraper_id": None,
                         "name": "auto (%s)" % ", ".join(entry["name"]
                                                         for entry in handlers[:4]),
                         "attribution": AMBIGUOUS})
        sources = [base]
        unreachable = []

        if certain:
            return sources, unreachable

        for entry in handlers:
            if FRAGMENT in entry["kinds"] and self.config["aimAmbiguousUrls"]:
                sources.append({
                    "type": "url_scraper",
                    "method": M_URL_FRAGMENT,
                    "url": url,
                    "url_key": url_record["key"],
                    "host": url_record.get("host") or "",
                    "depth": int(depth),
                    "parent_source_id": parent_source_id,
                    "scraper_id": entry["id"],
                    "name": entry["name"],
                    "attribution": CERTAIN,
                    "handlers": [entry["id"]],
                })
            elif FRAGMENT not in entry["kinds"]:
                unreachable.append({
                    "scraper_id": entry["id"], "name": entry["name"], "url": url,
                    "reason": "several scrapers match this URL and this one only "
                              "supports URL scraping, which Stash cannot aim",
                })
        return sources, unreachable


def source_key(source):
    """The identity of a source within a run: who was asked, how, about what.

    This is the loop guard. It is a (scraper, url) pair for URL work - requirement 4 -
    and includes the method, because the same scraper reached two different ways is two
    different answers and both are worth having.
    """
    method = source["method"]
    if method in (M_STASHBOX_FP, M_STASHBOX_QUERY):
        return "%s|%s|%s" % (method, source.get("endpoint") or "",
                             source.get("target") or "")
    return "%s|%s|%s" % (method, source.get("scraper_id") or "*",
                         source.get("url_key") or source.get("url") or "")


def graphql_source(source):
    """The `ScraperSourceInput` for a source, or None for a plain URL scrape."""
    method = source["method"]
    if method in (M_STASHBOX_FP, M_STASHBOX_QUERY):
        return {"stash_box_endpoint": source["endpoint"]}
    if method == M_URL_FRAGMENT:
        return {"scraper_id": source["scraper_id"]}
    return None


def graphql_input(source, scene_id):
    """The `ScrapeSingleSceneInput` for a source.

    Exactly one of scene_id / query / scene_input, because Stash's resolver checks them
    in that order and silently ignores the rest.
    """
    method = source["method"]
    if method == M_STASHBOX_FP:
        # Stash's own fingerprint lookup against that box: oshash, phash and duration
        # are read from the scene's files by the server. FastDiscovery does not compute,
        # send or match a fingerprint itself (requirement 3).
        return {"scene_id": str(scene_id)}
    if method == M_STASHBOX_QUERY:
        return {"query": str(source.get("target") or "")}
    if method == M_URL_FRAGMENT:
        return {"scene_input": {"url": source["url"], "urls": [source["url"]]}}
    raise ValueError("no scrape input for method %r" % method)
