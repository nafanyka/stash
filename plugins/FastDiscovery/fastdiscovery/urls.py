"""URL normalisation, and the key the loop guard is built on.

Two forms per URL, and the difference matters:

* `url` - what the scraper actually said. This is what gets written to the scene if
  the user selects it. Never replaced by a tidied-up version (requirement 34).
* `key`  - a comparison key: scheme dropped, host lowercased, `www.` dropped, default
  port dropped, fragment dropped, a trailing slash collapsed, and the handful of
  parameters that identify the referrer rather than the content removed.

The normalisation is deliberately shallow (requirement 6). Path case is preserved -
plenty of sites route on a case-sensitive slug - and no query parameter is dropped
unless it is on the tracking list, because on a lot of sites the query *is* the page
(`view_video.php?viewkey=...`).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

SAFE_SCHEMES = ("http", "https")

# Parameters that say who sent you, not what you are looking at.
TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "ref", "referrer", "referer", "aff", "affiliate", "affiliate_id",
    "partner", "partnerid", "nats", "ats", "utm_reader", "trk",
)

_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\)\]}]+", re.IGNORECASE)


def is_safe(value) -> bool:
    """Whether a URL may be followed, stored or rendered as a link.

    Scraper output is untrusted, so anything that is not plain http(s) - javascript:,
    data:, file: - is refused rather than sanitised.
    """
    if not value:
        return False
    try:
        parts = urlsplit(str(value).strip())
    except ValueError:
        return False
    return parts.scheme.lower() in SAFE_SCHEMES and bool(parts.netloc)


def normalize(value):
    """{url, normalized, key, host} for a URL, or None if it is not one we will follow."""
    if not is_safe(value):
        return None
    original = str(value).strip()
    parts = urlsplit(original)

    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]

    port = ""
    if parts.port and not ((parts.scheme == "http" and parts.port == 80) or
                           (parts.scheme == "https" and parts.port == 443)):
        port = ":%d" % parts.port

    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    try:
        path = quote(unquote(path), safe="/:@!$&'()*+,;=~-._")
    except (UnicodeDecodeError, ValueError):
        pass

    kept = [(name, item) for name, item
            in parse_qsl(parts.query, keep_blank_values=True)
            if name.lower() not in TRACKING_PARAMS]
    kept.sort()
    query = urlencode(kept)

    normalized = urlunsplit((parts.scheme.lower(), host + port, path, query, ""))
    key = host + port + path + (("?" + query) if query else "")
    return {"url": original, "normalized": normalized, "key": key, "host": host}


def in_text(text):
    """Every http(s) URL inside a blob of text, in order, de-duplicated.

    Scrapers put source links in `details` often enough that ignoring them loses real
    leads, and a URL in prose is exactly the kind of thing the recursion is for.
    """
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_IN_TEXT.findall(str(text)):
        candidate = match.rstrip(".,;:!?)")
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


# What a URL found in a payload is *for*. Only a scene-role URL is worth handing to a
# scene scraper: a performer's homepage is not a scene page, and following it with a
# scene scraper burns an attempt for nothing. Related URLs are still recorded, so the
# discovery graph is honest about where a lead came from.
ROLE_SCENE = "scene"
ROLE_RELATED = "related"


def from_result(raw):
    """[{url, role, source}] for one ScrapedScene payload, in a stable order.

    `source` names the field the URL sat in, which is what makes the discovery graph
    readable later ("this lead came out of StashDB's details text").
    """
    raw = raw if isinstance(raw, dict) else {}
    found, seen = [], set()

    def add(value, role, source):
        if not is_safe(value):
            return
        text = str(value).strip()
        if (text, role) in seen:
            return
        seen.add((text, role))
        found.append({"url": text, "role": role, "source": source})

    for value in (raw.get("urls") or []):
        add(value, ROLE_SCENE, "urls")
    add(raw.get("url"), ROLE_SCENE, "url")
    for value in in_text(raw.get("details")):
        add(value, ROLE_SCENE, "details")

    studio = raw.get("studio")
    if isinstance(studio, dict):
        add(studio.get("url"), ROLE_RELATED, "studio.url")
        for value in (studio.get("urls") or []):
            add(value, ROLE_RELATED, "studio.urls")
    for performer in (raw.get("performers") or []):
        if not isinstance(performer, dict):
            continue
        add(performer.get("url"), ROLE_RELATED, "performer.url")
        for value in (performer.get("urls") or []):
            add(value, ROLE_RELATED, "performer.urls")
    for group in (raw.get("groups") or []) + (raw.get("movies") or []):
        if not isinstance(group, dict):
            continue
        add(group.get("url"), ROLE_RELATED, "group.url")
        for value in (group.get("urls") or []):
            add(value, ROLE_RELATED, "group.urls")

    return found
