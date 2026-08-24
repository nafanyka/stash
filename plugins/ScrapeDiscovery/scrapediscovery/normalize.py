"""Turning what a scraper said into something comparable.

Pure functions only: no network, no database, no configuration beyond what is passed
in. That is what lets the reprocessing tasks rebuild every derived row from the stored
raw payloads without asking a website anything again (docs/architecture.md section 6).

Three jobs live here:

* URL normalisation, so "the same URL" is one thing across scrapers and so a scan's
  loop guard has a stable key;
* result normalisation, so titles, dates, durations, names and URLs from thirty
  different scrapers can be compared at all;
* fingerprinting, so an identical answer seen twice is recognised rather than stored
  twice.

`NORM_VERSION` in db/migrations.py is bumped whenever the output of `scene_result`
changes meaning, which is how a reprocess run finds stale rows.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import difflib
import hashlib
import json
import re
import unicodedata
try:  # pragma: no cover - both spellings exist across supported Pythons
    from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
except ImportError:  # pragma: no cover
    raise

# Query parameters that identify the referrer rather than the content. Dropping them
# is what makes two links to the same page compare equal.
TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "ref", "referrer",
    "referer", "source", "src", "aff", "affiliate", "affiliate_id", "campaign",
    "partner", "partnerid", "nats", "ats", "tracking", "trk", "cid", "sid",
)

# Noise a filename carries that a title never does. Ordered longest-first so the
# multi-word markers go before their pieces.
FILENAME_NOISE = (
    "1080p", "2160p", "1440p", "4320p", "720p", "480p", "360p",
    "8k", "6k", "5k", "4k", "2k", "uhd", "fhd", "hd", "sd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "aac", "ac3",
    "mp4", "mkv", "avi", "wmv", "mov", "m4v", "webm", "flv", "ts",
    "web", "webrip", "webdl", "web-dl", "bluray", "brrip", "dvdrip", "hdrip",
    "60fps", "30fps", "vr", "180x180", "360x180", "3dh", "3dv", "lr", "sbs", "ou",
    "oculus", "gearvr", "smartphone", "quest", "psvr", "vive", "index",
    "original", "trailer", "sample", "proper", "repack", "internal", "rarbg", "xxx",
)

_WORD = re.compile(r"[a-z0-9]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\)\]}]+", re.IGNORECASE)
_DATA_URI = re.compile(r"^data:(?P<mime>[\w.+/-]+)?(?:;charset=[\w-]+)?;base64,(?P<data>.*)$",
                       re.IGNORECASE | re.DOTALL)
_SEPARATORS = re.compile(r"[._\-\s]+")

# Anything else is a scheme we will not follow or render.
SAFE_SCHEMES = ("http", "https")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_MIME = ("image/jpeg", "image/jpg", "image/png", "image/webp",
                      "image/gif", "image/avif")


# ---------------------------------------------------------------- fingerprints

def canonical_json(value) -> str:
    """Stable JSON: sorted keys, no incidental whitespace.

    Two payloads that differ only in key order or spacing must fingerprint the same,
    otherwise the same answer looks new every time a scraper reorders its output.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------ text

def strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(ch))


def canon_text(value) -> str:
    """Casefolded, accent-free, punctuation-free, single-spaced."""
    if value is None:
        return ""
    text = strip_accents(str(value)).lower()
    return " ".join(_WORD.findall(text))


def canon_name(value) -> str:
    """A performer, studio or tag name reduced to what two sources can agree on.

    Spaces come out as well as punctuation, which `canon_text` keeps. Two sources
    genuinely disagree about them: the same studio arrived as "18VR" from Stash and
    "18 VR" from the scraper that owns the site. Names are only ever compared for
    equality here, never tokenised, so squashing them loses nothing and stops a space
    from reading as a contradiction.
    """
    return canon_text(value).replace(" ", "")


def title_from_filename(basename: str) -> str:
    """A searchable title from a filename.

    Deliberately conservative: separators become spaces, a trailing extension and the
    obvious technical markers go, and nothing else is guessed. Over-cleaning invents a
    title the site never used, which is worse for a name search than leaving a stray
    token in.
    """
    if not basename:
        return ""
    name = str(basename).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        stem, _, extension = name.rpartition(".")
        if stem and len(extension) <= 4:
            name = stem
    words = _SEPARATORS.split(name)
    kept = []
    for word in words:
        if not word:
            continue
        bare = canon_text(word)
        if not bare:
            continue
        if bare in FILENAME_NOISE:
            continue
        # Resolution-ish leftovers such as "1920x1080".
        if re.fullmatch(r"\d{3,4}x\d{3,4}", bare):
            continue
        kept.append(word.strip())
    return " ".join(kept).strip()


def similarity(left, right) -> float:
    """0-1 similarity between two titles.

    The maximum of a sequence ratio and a token-set ratio. The sequence ratio catches
    small edits, the token-set ratio catches reordering and one side carrying extra
    words - "Scene Title" against "Studio - Scene Title (2024)" should score high.
    """
    a, b = canon_text(left), canon_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()

    left_tokens, right_tokens = set(a.split()), set(b.split())
    shared = left_tokens & right_tokens
    if not shared:
        return sequence
    # Jaccard would punish the longer side for context it legitimately carries, so
    # measure containment of the smaller token set instead, then temper it.
    containment = len(shared) / float(min(len(left_tokens), len(right_tokens)))
    jaccard = len(shared) / float(len(left_tokens | right_tokens))
    # Capped below 1: the same words in a different order is a strong signal but not
    # the same title, and only an exact canonical match should be able to score 1.0.
    token_score = 0.97 * (containment + jaccard) / 2.0
    return max(sequence, token_score)


# ------------------------------------------------------------------- scalars

def parse_date(value):
    """A scraper's date as YYYY-MM-DD, or None.

    Scrapers are inconsistent here, so several layouts are accepted, but nothing is
    invented: a string that does not parse cleanly returns None rather than a guess,
    because a wrong date is a scoring signal pointing the wrong way. The one genuinely
    ambiguous case, a slash date such as 07/04/2025, is read day-first - the majority
    convention among the scrapers in the community index.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    # An ISO date, possibly with a time after it, is by far the common case.
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ]|$)", text)
    if match:
        try:
            return datetime.date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None

    formats = ("%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
               "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%Y%m%d")
    for pattern in formats:
        try:
            return datetime.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue

    # Last resort: an ISO date embedded in a longer string.
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return datetime.date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
    return None


def parse_duration(value):
    """Seconds as an int, from an int, a float, "1:23:45" or "83 min"."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            return None
        seconds = 0.0
        for number in numbers:
            seconds = seconds * 60.0 + number
        return int(round(seconds)) if seconds > 0 else None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(h|hour|hours|m|min|mins|minute|minutes|s|sec|secs|second|seconds)?$",
                     text, re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    factor = 3600.0 if unit.startswith("h") else 60.0 if unit.startswith("m") else 1.0
    seconds = amount * factor
    return int(round(seconds)) if seconds > 0 else None


def clean_string(value):
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    return text or None


# ---------------------------------------------------------------------- urls

def is_safe_url(value) -> bool:
    """Whether a discovered URL may be followed or rendered as a link.

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


def normalize_url(value):
    """(normalized, key, host) for a URL, or None if it is not one we will follow.

    `normalized` stays a usable URL. `key` is the loop-guard and dedup key, with the
    scheme and any "www." dropped so http/https and www/non-www are one thing. Path
    case is preserved: plenty of sites route on a case-sensitive slug, so folding it
    would merge two different pages.
    """
    if not is_safe_url(value):
        return None
    parts = urlsplit(str(value).strip())

    host = parts.hostname or ""
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]

    port = ""
    if parts.port and not ((parts.scheme == "http" and parts.port == 80) or
                           (parts.scheme == "https" and parts.port == 443)):
        port = ":%d" % parts.port

    path = parts.path or "/"
    # Collapse duplicated slashes and a single trailing one; "/a//b/" and "/a/b" are
    # the same page everywhere in practice.
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    path = quote(unquote(path), safe="/:@!$&'()*+,;=~-._")

    kept = [(key, value_) for key, value_ in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMS]
    kept.sort()
    query = urlencode(kept)

    normalized = urlunsplit((parts.scheme.lower(), host + port, path, query, ""))
    key = host + port + path + (("?" + query) if query else "")
    return {"normalized": normalized, "key": key, "host": host}


def urls_in_text(text):
    """Every http(s) URL inside a blob of text, in order, de-duplicated.

    Scrapers put source links in `details` often enough that ignoring them would lose
    real leads.
    """
    if not text:
        return []
    seen, out = set(), []
    for match in _URL_IN_TEXT.findall(str(text)):
        candidate = match.rstrip(".,;:!?")
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


# ROLE tells the engine what a discovered URL is *for*. Only a scene-role URL is worth
# handing to a scene URL scraper; a performer's homepage is not a scene page, and
# following it with a scene scraper would burn attempts for nothing.
ROLE_SCENE = "scene"
ROLE_RELATED = "related"


def extract_urls(raw):
    """Every URL a scraped scene mentions, tagged with its role and where it came from.

    Returns a list of {url, role, source} in a stable order: the scene's own URLs
    first, then the ones embedded in text, then related entities.
    """
    found, seen = [], set()

    def add(value, role, source):
        if not is_safe_url(value):
            return
        text = str(value).strip()
        if text in seen:
            return
        seen.add(text)
        found.append({"url": text, "role": role, "source": source})

    if not isinstance(raw, dict):
        return found

    add(raw.get("url"), ROLE_SCENE, "url")
    for value in (raw.get("urls") or []):
        add(value, ROLE_SCENE, "urls")
    for value in urls_in_text(raw.get("details")):
        add(value, ROLE_SCENE, "details")

    studio = raw.get("studio") or {}
    if isinstance(studio, dict):
        add(studio.get("url"), ROLE_RELATED, "studio.url")
        for value in (studio.get("urls") or []):
            add(value, ROLE_RELATED, "studio.urls")
    for key in ("performers", "tags", "movies", "groups"):
        for entry in (raw.get(key) or []):
            if not isinstance(entry, dict):
                continue
            add(entry.get("url"), ROLE_RELATED, key + ".url")
            for value in (entry.get("urls") or []):
                add(value, ROLE_RELATED, key + ".urls")
    return found


# --------------------------------------------------------------------- images

def split_data_uri(value):
    """(mime, bytes) for a base64 data URI, or None.

    Scrapers hand back covers inline, a couple of hundred kilobytes each. Pulling them
    out of the payload before storage is what keeps the database from being mostly
    duplicated JPEGs. Anything that is not a plausible image is refused: this is
    untrusted input, and it ends up in an <img src>.
    """
    if not value or not isinstance(value, str):
        return None
    match = _DATA_URI.match(value.strip())
    if not match:
        return None
    mime = (match.group("mime") or "").lower() or "image/jpeg"
    if mime not in ALLOWED_IMAGE_MIME:
        return None
    payload = re.sub(r"\s+", "", match.group("data") or "")
    if not payload or len(payload) > MAX_IMAGE_BYTES * 4 // 3 + 8:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return {"mime": mime, "data": data, "sha256": hashlib.sha256(data).hexdigest()}


def externalize_images(raw):
    """(payload with images replaced by references, [images]).

    The reference keeps the mime type and hash, so the original data URI can be
    rebuilt byte for byte from the blobs table - the raw result stays raw.
    """
    images = []

    def walk(node, path):
        if isinstance(node, dict):
            return {key: walk(value, path + "." + str(key)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, "%s[%d]" % (path, index)) for index, value in enumerate(node)]
        if isinstance(node, str) and node.startswith("data:"):
            blob = split_data_uri(node)
            if blob:
                images.append(blob)
                return {"$blob": blob["sha256"], "mime": blob["mime"],
                        "bytes": len(blob["data"])}
            # An unparseable data URI is not kept: it cannot be rendered and could be
            # megabytes of nothing.
            return {"$blob": None, "dropped": "unsupported data uri"}
        return node

    return walk(raw, "$"), images


def rebuild_data_uri(reference, blob):
    """The inverse of externalize_images, for one reference."""
    if not reference or not blob:
        return None
    return "data:%s;base64,%s" % (blob.get("mime") or "image/jpeg",
                                 base64.b64encode(blob["data"]).decode("ascii"))


# --------------------------------------------------------------- scene results

def _entity(entry, kind):
    if not isinstance(entry, dict):
        text = clean_string(entry)
        return {"name": text, "canon": canon_name(text), "stored_id": None} if text else None
    name = clean_string(entry.get("name"))
    if not name:
        return None
    out = {
        "name": name,
        "canon": canon_name(name),
        "stored_id": clean_string(entry.get("stored_id")),
    }
    if kind == "performer":
        for key in ("gender", "disambiguation", "birthdate", "country"):
            value = clean_string(entry.get(key))
            if value:
                out[key] = value
    if kind == "studio":
        parent = entry.get("parent") or {}
        if isinstance(parent, dict) and clean_string(parent.get("name")):
            out["parent"] = clean_string(parent.get("name"))
    if entry.get("remote_site_id"):
        out["remote_site_id"] = clean_string(entry.get("remote_site_id"))
    return out


def _entities(values, kind):
    out, seen = [], set()
    for entry in (values or []):
        item = _entity(entry, kind)
        if not item or item["canon"] in seen:
            continue
        seen.add(item["canon"])
        out.append(item)
    return out


def scene_result(raw):
    """One `ScrapedScene` payload as a comparable dict.

    Every field is either a normalised scalar or a list of {name, canon, stored_id},
    plus a `urls` list of normalised URL records. Missing is None rather than "", so
    "the scraper said nothing" and "the scraper said empty" stay distinguishable.
    """
    raw = raw if isinstance(raw, dict) else {}

    urls, seen = [], set()
    for entry in extract_urls(raw):
        if entry["role"] != ROLE_SCENE:
            continue
        parsed = normalize_url(entry["url"])
        if not parsed or parsed["key"] in seen:
            continue
        seen.add(parsed["key"])
        urls.append({"url": entry["url"], "normalized": parsed["normalized"],
                     "key": parsed["key"], "host": parsed["host"]})

    title = clean_string(raw.get("title"))
    studio = _entity(raw.get("studio"), "studio")
    file_info = raw.get("file") or {}
    duration = parse_duration(raw.get("duration"))
    if duration is None and isinstance(file_info, dict):
        duration = parse_duration(file_info.get("duration"))

    fingerprints = []
    for entry in (raw.get("fingerprints") or []):
        if not isinstance(entry, dict):
            continue
        algorithm = clean_string(entry.get("algorithm"))
        value = clean_string(entry.get("hash"))
        if algorithm and value:
            fingerprints.append({"algorithm": algorithm.lower(), "hash": value.lower(),
                                 "duration": parse_duration(entry.get("duration"))})

    return {
        "title": title,
        "title_canon": canon_text(title),
        "code": clean_string(raw.get("code")),
        "details": clean_string(raw.get("details")),
        "director": clean_string(raw.get("director")),
        "date": parse_date(raw.get("date")),
        "duration": duration,
        "studio": studio,
        "performers": _entities(raw.get("performers"), "performer"),
        "tags": _entities(raw.get("tags"), "tag"),
        "groups": _entities(raw.get("groups") or raw.get("movies"), "group"),
        "urls": urls,
        "remote_site_id": clean_string(raw.get("remote_site_id")),
        "fingerprints": fingerprints,
        "has_image": bool(raw.get("image")),
    }


def is_empty_result(normalized) -> bool:
    """Whether a normalised result carries anything worth keeping as a candidate.

    Some scrapers answer a fragment scrape with an object whose every field is null
    rather than with nothing at all; that is a NO_MATCH wearing a hat.
    """
    if not normalized:
        return True
    for key in ("title", "code", "date", "details", "director", "remote_site_id"):
        if normalized.get(key):
            return False
    for key in ("performers", "tags", "groups", "urls", "fingerprints"):
        if normalized.get(key):
            return False
    if normalized.get("studio") or normalized.get("duration") or normalized.get("has_image"):
        return False
    return True


def scene_snapshot(scene):
    """The Stash scene, normalised the same way, so the two sides are comparable."""
    scene = scene or {}
    files = scene.get("files") or []
    primary = files[0] if files else {}
    basename = clean_string(primary.get("basename")) or ""
    path = clean_string(primary.get("path")) or clean_string(scene.get("path")) or ""

    urls, seen = [], set()
    for value in (scene.get("urls") or []):
        parsed = normalize_url(value)
        if not parsed or parsed["key"] in seen:
            continue
        seen.add(parsed["key"])
        urls.append({"url": value, "normalized": parsed["normalized"],
                     "key": parsed["key"], "host": parsed["host"]})

    fingerprints = []
    for entry in (primary.get("fingerprints") or []):
        if isinstance(entry, dict) and entry.get("type") and entry.get("value"):
            fingerprints.append({"algorithm": str(entry["type"]).lower(),
                                 "hash": str(entry["value"]).lower(), "duration": None})

    title = clean_string(scene.get("title"))
    filename_title = title_from_filename(basename)
    return {
        "scene_id": str(scene.get("id") or ""),
        "title": title,
        "title_canon": canon_text(title),
        "filename": basename,
        "filename_title": filename_title,
        "filename_title_canon": canon_text(filename_title),
        "path": path,
        "code": clean_string(scene.get("code")),
        "details": clean_string(scene.get("details")),
        "director": clean_string(scene.get("director")),
        "date": parse_date(scene.get("date")),
        "duration": parse_duration(primary.get("duration")),
        "width": primary.get("width"),
        "height": primary.get("height"),
        "size": primary.get("size"),
        "organized": bool(scene.get("organized")),
        "studio": _entity(scene.get("studio"), "studio"),
        "performers": _entities(scene.get("performers"), "performer"),
        "tags": _entities(scene.get("tags"), "tag"),
        "groups": _entities([entry.get("group") for entry in (scene.get("groups") or [])
                             if isinstance(entry, dict)], "group"),
        "urls": urls,
        "fingerprints": fingerprints,
        "stash_ids": [
            {"endpoint": clean_string(entry.get("endpoint")),
             "stash_id": clean_string(entry.get("stash_id"))}
            for entry in (scene.get("stash_ids") or []) if isinstance(entry, dict)
        ],
        # What a name search should look for: the title if there is one, else the
        # filename cleaned up. An unidentified scene rarely has a usable title, which
        # is the whole reason this fallback exists.
        "search_term": title or filename_title,
        "display_title": title or basename or ("scene " + str(scene.get("id") or "?")),
    }
