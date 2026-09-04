"""The scene field model: what exists, how it merges, and how two values compare.

One table drives everything - the review matrix, the default selection, and the write
at the end - so supporting a field ScrapedScene grows later is adding a row here, not
threading a new special case through three modules (requirement 32).

`kind` says how a field merges, which is the only distinction that matters:

    SCALAR      one value wins            title, date, details, code, director
    ENTITY      one entity wins           studio
    ENTITY_LIST union of entities         performers, tags, groups
    URL_LIST    union of URLs             urls
    STASH_ID    union of (endpoint, id)   stash_ids
    IMAGE       one image wins            image -> cover_image

Two representations of every value, and the difference is load-bearing (req. 34):
`raw` is exactly what the source said and is what gets written on apply; `key` is a
normalised comparison key used only to decide that two sources said the same thing.
"""

from __future__ import annotations

import datetime
import re
import unicodedata

SCALAR = "scalar"
ENTITY = "entity"
ENTITY_LIST = "entity_list"
URL_LIST = "url_list"
STASH_ID = "stash_id_list"
IMAGE = "image"

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def clean(value):
    """A trimmed string, or None. Empty and whitespace-only collapse to None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = _WS.sub(" ", str(value)).strip()
    return text or None


def strip_accents(text):
    return "".join(char for char in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(char))


def canon_text(value):
    """Casefolded, punctuation-free, whitespace-collapsed - for comparison only."""
    text = clean(value)
    if not text:
        return ""
    text = strip_accents(text).casefold()
    return _WS.sub(" ", _PUNCT.sub(" ", text)).strip()


def canon_name(value):
    """Comparison key for an entity name.

    Same as canon_text, kept separate so the two can diverge without a surprise:
    people's names take different liberties from titles - initials, ampersands,
    bracketed disambiguation.
    """
    text = canon_text(value)
    return _WS.sub(" ", text.replace("&", " and ")).strip()


_DATE_PATTERNS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
    "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y%m%d",
)


def parse_date(value):
    """A date as YYYY-MM-DD, or None. Scrapers spell dates half a dozen ways."""
    text = clean(value)
    if not text:
        return None
    text = text.split("T")[0].strip()
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if match:
        try:
            return datetime.date(int(match.group(1)), int(match.group(2)),
                                 int(match.group(3))).isoformat()
        except ValueError:
            return None
    return None


class Field:
    """One row of the review matrix.

    `scene_key`  where the value sits on a Stash scene (findScene shape)
    `result_key` where it sits in a ScrapedScene payload
    `update_key` the SceneUpdateInput field it is written to, or None for read-only
    """

    def __init__(self, name, kind, label, scene_key=None, result_key=None,
                 update_key=None, order=0, entity=None, note=""):
        self.name = name
        self.kind = kind
        self.label = label
        self.scene_key = scene_key
        self.result_key = result_key if result_key is not None else name
        self.update_key = update_key
        self.order = order
        self.entity = entity          # performer / tag / studio / group
        self.note = note

    @property
    def writable(self):
        return self.update_key is not None

    @property
    def is_list(self):
        return self.kind in (ENTITY_LIST, URL_LIST, STASH_ID)

    def to_json(self):
        return {"name": self.name, "kind": self.kind, "label": self.label,
                "writable": self.writable, "entity": self.entity, "note": self.note,
                "order": self.order}


# The order here is the order of rows in the review table.
FIELDS = (
    Field("title", SCALAR, "Title", "title", "title", "title", 10),
    Field("date", SCALAR, "Date", "date", "date", "date", 20),
    Field("studio", ENTITY, "Studio", "studio", "studio", "studio_id", 30,
          entity="studio"),
    Field("code", SCALAR, "Code", "code", "code", "code", 40),
    Field("director", SCALAR, "Director", "director", "director", "director", 50),
    Field("details", SCALAR, "Details", "details", "details", "details", 60),
    Field("performers", ENTITY_LIST, "Performers", "performers", "performers",
          "performer_ids", 70, entity="performer"),
    Field("tags", ENTITY_LIST, "Tags", "tags", "tags", "tag_ids", 80, entity="tag"),
    Field("groups", ENTITY_LIST, "Groups", "groups", "groups", "groups", 90,
          entity="group"),
    Field("urls", URL_LIST, "URLs", "urls", "urls", "urls", 100),
    Field("image", IMAGE, "Image", None, "image", "cover_image", 110,
          note="Scenes have one cover, so exactly one candidate is written."),
    Field("stash_ids", STASH_ID, "Stash IDs", "stash_ids", None, "stash_ids", 120,
          note="Recorded by the stash-box that matched the scene."),
    # Present on the scene but not in ScrapedScene: no scraper can offer one, so the
    # row only ever shows what is already there. Kept in the table rather than hidden
    # so the review stays a complete picture of what apply will write.
    Field("rating100", SCALAR, "Rating", "rating100", None, "rating100", 130,
          note="Stash's scrape API has no rating field, so only the current value "
               "can exist."),
)

BY_NAME = {field.name: field for field in FIELDS}
BY_RESULT_KEY = {field.result_key: field for field in FIELDS if field.result_key}

# ScrapedScene fields deliberately not shown as rows of their own.
IGNORED_RESULT_KEYS = {
    "url",              # the deprecated singular; folded into urls
    "movies",           # the deprecated spelling of groups; folded into groups
    "file",             # file metadata about their copy, not ours
    "fingerprints",     # evidence, not metadata; shown on the source, not as a field
    "remote_site_id",   # folded into stash_ids together with the box's endpoint
    # A scene's duration comes from its own file and can never be written, so a row for
    # it would be a row nobody can act on. The sources still report it and it is still
    # in the stored results; it is simply not part of the review.
    "duration",
    "__typename",
}


def extra_fields(schema_field_names):
    """Rows for ScrapedScene fields this version of FastDiscovery does not know.

    A newer Stash that adds a scene field starts showing it as a read-only scalar row
    the day it appears, with its values and provenance intact, instead of silently
    dropping it. Making it writable is then one line in FIELDS.
    """
    known = set(BY_RESULT_KEY) | IGNORED_RESULT_KEYS
    out = []
    for index, name in enumerate(sorted(schema_field_names or ())):
        if name in known:
            continue
        out.append(Field(name, SCALAR, name.replace("_", " ").title(),
                         None, name, None, 900 + index,
                         note="Not known to this version of FastDiscovery: shown for "
                              "review, not written."))
    return out


def scalar_key(field, value):
    """The comparison key for a scalar value: equal keys mean the same answer."""
    if value is None:
        return ""
    if field.name == "date":
        return parse_date(value) or canon_text(value)
    if field.name == "details":
        # Details differ by a trailing newline across half the scrapers; comparing the
        # canonical form keeps that from looking like two different descriptions.
        return canon_text(value)[:4000]
    return canon_text(value)


def display_scalar(field, value):
    """What the table shows for a scalar. The raw value, only ever reformatted."""
    text = clean(value)
    if text is None:
        return None
    if field.name == "date":
        return parse_date(value) or text
    return text


# --------------------------------------------------------------- the scene side

# Noise a filename carries that a title never does, longest markers first so the
# multi-word ones go before their pieces.
FILENAME_NOISE = (
    "2160p", "1440p", "1080p", "720p", "480p", "360p", "4320p",
    "uhd", "fhd", "hd", "sd", "8k", "6k", "4k", "2k",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx", "aac", "ac3",
    "mp4", "mkv", "avi", "wmv", "mov", "m4v", "webm", "flv",
    "webrip", "web-dl", "webdl", "bluray", "brrip", "dvdrip", "hdrip",
    "60fps", "30fps", "vr", "sbs", "oculus", "quest", "psvr",
    "xxx", "rarbg", "proper", "repack", "internal", "original", "sample",
)


def title_from_filename(basename):
    """A searchable title out of a file name, or "" if nothing survives.

    Only used as the query for a stash-box name search, and only when the scene has no
    title of its own - which is exactly the case where a search is worth running.
    """
    text = clean(basename) or ""
    if not text:
        return ""
    text = text.rsplit(".", 1)[0] if "." in text[-5:] else text
    text = re.sub(r"[._\-\[\]()]+", " ", text)
    words = [word for word in text.split()
             if word.casefold() not in FILENAME_NOISE and not re.fullmatch(r"\d{1,4}p", word.casefold())]
    return _WS.sub(" ", " ".join(words)).strip()


def local_url(value):
    """A Stash-served URL, reduced to a path the browser can resolve itself.

    `paths.screenshot` is absolute, and Stash builds it from the base URL of whoever
    asked - which here is this plugin, talking to localhost. Handing that to a browser
    on another machine gives it an address only the server can reach, and the cover
    silently fails to load. The path survives the move; the host is the part that was
    never ours to state.
    """
    text = clean(value)
    if not text:
        return None
    for prefix in ("http://", "https://"):
        if text.lower().startswith(prefix):
            rest = text[len(prefix):]
            slash = rest.find("/")
            return rest[slash:] if slash >= 0 else "/"
    return text


def scene_snapshot(scene):
    """What Stash already knows about a scene, in the shape the review compares against.

    `values` is keyed by field name and holds the raw current value, so the CURRENT
    column is built exactly like every other column. The rest is what the run itself
    needs: the URLs to seed discovery with, and something to search a stash-box for.
    """
    from . import urls as urls_module

    scene = scene or {}
    files = scene.get("files") or []
    primary = files[0] if files else {}
    basename = clean(primary.get("basename")) or ""

    seen, scene_urls = set(), []
    for value in (scene.get("urls") or []):
        record = urls_module.normalize(value)
        if record and record["key"] not in seen:
            seen.add(record["key"])
            scene_urls.append(record)

    title = clean(scene.get("title"))
    filename_title = title_from_filename(basename)
    groups = []
    for entry in (scene.get("groups") or []):
        if isinstance(entry, dict) and isinstance(entry.get("group"), dict):
            groups.append(dict(entry["group"], scene_index=entry.get("scene_index")))

    return {
        "scene_id": str(scene.get("id") or ""),
        "title": title,
        "filename": basename,
        "path": clean(primary.get("path")) or "",
        "display_title": title or basename or ("scene " + str(scene.get("id") or "?")),
        "search_term": title or filename_title,
        "updated_at": scene.get("updated_at"),
        "urls": scene_urls,
        "screenshot": local_url((scene.get("paths") or {}).get("screenshot")
                                if isinstance(scene.get("paths"), dict) else None),
        "values": {
            "title": title,
            "date": clean(scene.get("date")),
            "code": clean(scene.get("code")),
            "director": clean(scene.get("director")),
            "details": clean(scene.get("details")),
            "rating100": scene.get("rating100"),
            "duration": primary.get("duration"),
            "studio": scene.get("studio"),
            "performers": scene.get("performers") or [],
            "tags": scene.get("tags") or [],
            "groups": groups,
            "urls": [record["url"] for record in scene_urls],
            "stash_ids": scene.get("stash_ids") or [],
        },
    }
