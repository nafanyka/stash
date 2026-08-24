"""What, if anything, independent sources agree on for a scene.

This is not the candidate correlator - that groups every answer into as many candidates
as the evidence supports, and is a later phase. This answers a narrower and more urgent
question: *is there one answer trustworthy enough to hand to Stash's merge dialog?*

It has to be conservative, because the honest answer is usually no. Throwing one scene
at every installed fragment scraper on a real library produced 21 "matches", of which
three were the scene and the rest were not: one scraper returned the server's IP address
as a title, another returned a different film with a similar name, a third returned an
unrelated scene from a site that has never heard of it. Anything that picks a "best"
result without a reason would put those in front of the user as metadata to save.

So a group only qualifies on evidence that cannot be manufactured by a scraper guessing:

* a **fingerprint match** - a stash-box recognised the file itself;
* a **URL already on the scene** - the site's own page for a link the user put there;
* **two independent witnesses**, and independence is counted carefully: two scrapers for
  the same site are one witness, and everything that merely re-read a URL the scene
  already carried is one witness between them, however many scrapers did the re-reading.

Everything else is left for review. Saying "22 answers, none of them confident" is more
useful than being confidently wrong.
"""

from __future__ import annotations

from . import normalize, registry

# Why a group is trusted, strongest first. Kept as data so the reason can be reported.
BY_FINGERPRINT = "a stash-box matched the file's fingerprint"
BY_SCENE_URL = "the site's own page for a URL already on the scene"
BY_AGREEMENT = "independent sources agree"

# Singular fields, in the order a merge dialog wants them.
SINGULAR = ("title", "date", "code", "details", "director")
# Set-like fields, merged additively.
PLURAL = ("performers", "tags")


def source_key(result, scraper_hosts=None):
    """A stable identity for *who was asked*, for counting independent sources.

    Deliberately not the host of the URL that came back. Several sources legitimately
    report the same site's URL - on the test scene, StashDB, theporndb, Timestamp.trade
    and the site's own scraper all returned the same czechvrcasting.com link - and
    keying on that would collapse four independent databases into one witness.

    What should collapse is two scrapers *for the same site*, so a scraper that declares
    exactly one host is keyed by that host. A scraper covering several sites, or none,
    is keyed by its own id, because it is not the site's own voice. Stash-boxes are
    keyed by endpoint: each is a separate database.

    `scraper_hosts` maps scraper id to its declared hosts, from the stored registry -
    no network call needed.
    """
    method = result.get("method")
    identifier = str(result.get("scraper_id") or result.get("scraper_name") or "")

    if method in (registry.M_STASHBOX_FP, registry.M_STASHBOX_QUERY):
        return "stashbox:" + identifier
    if not identifier:
        # Stash auto-routed a URL and does not report the handler; the URL's own host
        # is the best available identity for it.
        target = normalize.normalize_url(result.get("target") or "")
        return ("url:" + target["host"]) if target else "url:unattributed"

    hosts = sorted((scraper_hosts or {}).get(identifier) or ())
    if len(hosts) == 1:
        return "site:" + hosts[0]
    return "scraper:" + identifier


def contributes_evidence(result, snapshot):
    """Whether a result is evidence of identification, or just noise shaped like it.

    A fragment scrape hands the scraper the scene's title, URLs and date, so a scraper
    can produce something that looks like a match without having looked anything up.
    All of these were observed on one real scene:

    * a scraper that returned the URL it had been given, in the `code` field, plus its
      own name as the studio;
    * a scraper that returned a title and nothing else;
    * a scraper that returned the server's IP address as a title.

    A title on its own is never evidence, however plausible it looks - it could have
    come from the filename, from the fragment, or from nowhere. What counts is
    something the scraper could only know by finding a record: a URL the scene did not
    already have, a date, people or tags, a genuine code, a synopsis, or a studio that
    is not simply the scraper's own name.
    """
    normalized = result.get("normalized") or {}
    snapshot = snapshot or {}
    known_urls = {one["key"] for one in (snapshot.get("urls") or [])}

    if normalized.get("date") or normalized.get("director"):
        return True
    if normalized.get("performers") or normalized.get("tags") or normalized.get("groups"):
        return True
    if normalized.get("details") or normalized.get("fingerprints"):
        return True
    if any(one["key"] not in known_urls for one in (normalized.get("urls") or [])):
        return True

    code = normalized.get("code")
    # A code is an identifier, never a URL. One scraper answered with the very link it
    # had been handed, which is the opposite of new information.
    if code and not normalize.is_safe_url(code):
        return True

    studio = (normalized.get("studio") or {}).get("name")
    if studio:
        # Plenty of scrapers hardcode their own site as the studio, which tells us
        # nothing about this scene.
        own_name = result.get("scraper_name") or result.get("scraper_id") or ""
        if normalize.canon_name(studio) != normalize.canon_name(own_name):
            return True

    return False


def rereads_known_url(result, snapshot):
    """Whether a result is only a re-reading of a URL the scene already carried.

    Several installed scrapers answer a fragment scrape by fetching whichever URL is in
    the fragment. What they return can be perfectly correct - and it is still the same
    evidence as the site's own scraper reading the same page, not a second opinion.
    Counting each as independent is how five copies of one fact become false
    confidence.
    """
    normalized = result.get("normalized") or {}
    urls = normalized.get("urls") or []
    if not urls:
        return False
    known = {one["key"] for one in ((snapshot or {}).get("urls") or [])}
    return all(one["key"] in known for one in urls)


def witness_key(result, snapshot, scraper_hosts=None):
    """The identity of the *evidence* a result represents, not just of its source.

    A fingerprint match stands on its own: it identified the file, whatever URL it
    happened to print alongside. Everything that merely re-read a URL the scene already
    had shares one key, because it is all one piece of evidence. Anything else is keyed
    by its source.
    """
    if result.get("method") == registry.M_STASHBOX_FP:
        return source_key(result, scraper_hosts)
    if rereads_known_url(result, snapshot):
        return "known-url"
    return source_key(result, scraper_hosts)


def _groupable(left, right, threshold):
    """Whether two results are talking about the same scene.

    A shared canonical URL is conclusive. Otherwise the titles have to be close enough
    that a difference is formatting rather than a different film - "No Girls Allowed"
    and "211 - No Girls Allowed" are one scene; "No Girls Allowed" and "No dirty sluts
    allowed" are not, and a looser rule cannot tell them apart.
    """
    left_urls = {one["key"] for one in (left.get("normalized") or {}).get("urls") or []}
    right_urls = {one["key"] for one in (right.get("normalized") or {}).get("urls") or []}
    if left_urls & right_urls:
        return True

    left_title = (left.get("normalized") or {}).get("title")
    right_title = (right.get("normalized") or {}).get("title")
    if not left_title or not right_title:
        return False
    if normalize.similarity(left_title, right_title) < threshold:
        return False

    # A title match with contradicting dates is two releases, not one scene.
    left_date = (left.get("normalized") or {}).get("date")
    right_date = (right.get("normalized") or {}).get("date")
    if left_date and right_date and left_date != right_date:
        return False
    return True


def group(results, threshold=0.86):
    """Partition results into groups that describe the same scene.

    Union-find, so a chain of pairwise agreements ends up as one group; deliberately
    only ever merged on the strong rule above.
    """
    parent = list(range(len(results)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            if _groupable(results[i], results[j], threshold):
                union(i, j)

    grouped = {}
    for index, result in enumerate(results):
        grouped.setdefault(find(index), []).append(result)
    return list(grouped.values())


def _trust(members, snapshot, scraper_hosts=None):
    """(reason, independent_witness_count) for a group, or (None, n) if untrusted."""
    scene_url_keys = {one["key"] for one in ((snapshot or {}).get("urls") or [])}
    independent = len({witness_key(one, snapshot, scraper_hosts) for one in members})

    for one in members:
        if one.get("method") in (registry.M_STASHBOX_FP,):
            return BY_FINGERPRINT, independent
    for one in members:
        if one.get("method") == registry.M_URL:
            keys = {url["key"] for url in (one.get("normalized") or {}).get("urls") or []}
            target = normalize.normalize_url(one.get("target") or "")
            if target:
                keys.add(target["key"])
            if keys & set(scene_url_keys or ()):
                return BY_SCENE_URL, independent
    if independent >= 2:
        return BY_AGREEMENT, independent
    return None, independent


# How much a result's provenance is worth when values disagree. A fingerprint match
# identified the file itself; a name search found something with a similar name.
METHOD_RANK = {
    registry.M_STASHBOX_FP: 0,
    registry.M_URL: 1,
    registry.M_FRAGMENT_INPUT: 2,
    registry.M_FRAGMENT_SCENE: 3,
    registry.M_STASHBOX_QUERY: 4,
    registry.M_NAME: 5,
}


def _rank_of(member):
    return METHOD_RANK.get(member.get("method"), 9)


def _vote(members, field):
    """The value for a singular field, plus who said it.

    Most sources win. A tie is broken by provenance and then by brevity, which is not
    arbitrary: on the test scene two sources offered a `code` of "211" and two offered
    the page's whole HTML title, "Czech VR Casting 211 No Girls Allowed - Czech VR
    Casting Porn Videos". Counting votes alone picked whichever happened to be first.
    """
    tally = []
    for member in members:
        value = (member.get("normalized") or {}).get(field)
        if field == "studio":
            value = (value or {}).get("name") if isinstance(value, dict) else value
        if value in (None, ""):
            continue
        key = normalize.canon_text(value) if isinstance(value, str) else str(value)
        for entry in tally:
            if entry["key"] == key:
                entry["sources"].append(member)
                break
        else:
            tally.append({"key": key, "value": value, "sources": [member]})

    if not tally:
        return None
    tally.sort(key=lambda entry: (
        -len(entry["sources"]),
        min(_rank_of(one) for one in entry["sources"]),
        len(str(entry["value"])),
    ))
    best = tally[0]
    return {
        "value": best["value"],
        "sources": [_source_label(one) for one in best["sources"]],
        "agreement": len(best["sources"]),
        "alternatives": [
            {"value": entry["value"], "agreement": len(entry["sources"])}
            for entry in tally[1:]
        ],
    }


def _source_label(result):
    label = result.get("scraper_name") or result.get("scraper_id")
    if label:
        return str(label)
    target = normalize.normalize_url(result.get("target") or "")
    return target["host"] if target else "auto-routed"


def _union(members, field):
    """Every distinct value for a set-like field, with who supplied each."""
    out = []
    for member in members:
        for entry in ((member.get("normalized") or {}).get(field) or []):
            canon = entry.get("canon") or normalize.canon_name(entry.get("name"))
            if not canon:
                continue
            for existing in out:
                if existing["canon"] == canon:
                    existing["sources"].append(_source_label(member))
                    break
            else:
                out.append({"name": entry.get("name"), "canon": canon,
                            "stored_id": entry.get("stored_id"),
                            "sources": [_source_label(member)]})
    return out


def best(results, snapshot=None, threshold=0.86, scraper_hosts=None):
    """The one answer worth offering, or None.

    Returns a dict describing what was agreed, why it is trusted, and which source
    supplied each value - so the reason can be logged and shown, rather than the user
    having to take it on faith.
    """
    snapshot = snapshot or {}

    usable, ignored = [], []
    for one in results:
        normalized = one.get("normalized") or {}
        if not (normalized.get("title") or normalized.get("urls")):
            continue
        if contributes_evidence(one, snapshot):
            usable.append(one)
        else:
            ignored.append(one)
    if not usable:
        return None

    groups = group(usable, threshold)
    scored = []
    for members in groups:
        reason, independent = _trust(members, snapshot, scraper_hosts)
        if not reason:
            continue
        rank = (BY_FINGERPRINT, BY_SCENE_URL, BY_AGREEMENT).index(reason)
        scored.append((rank, -independent, -len(members), members, reason, independent))

    if not scored:
        return None
    scored.sort(key=lambda entry: entry[:3])
    _rank, _neg_independent, _neg_size, members, reason, independent = scored[0]

    fields = {}
    for field in SINGULAR + ("studio",):
        voted = _vote(members, field)
        if voted:
            fields[field] = voted
    for field in PLURAL:
        values = _union(members, field)
        if values:
            fields[field] = values

    urls, seen = [], set()
    for member in members:
        for entry in ((member.get("normalized") or {}).get("urls") or []):
            if entry["key"] not in seen:
                seen.add(entry["key"])
                urls.append(entry)

    image_source = next((one for one in members if one.get("image_sha256")), None)

    return {
        "reason": reason,
        "independent_sources": independent,
        "result_ids": [one["id"] for one in members],
        "sources": sorted({_source_label(one) for one in members}),
        "witnesses": sorted({witness_key(one, snapshot, scraper_hosts) for one in members}),
        "fields": fields,
        "urls": urls,
        "image_sha256": (image_source or {}).get("image_sha256"),
        "discarded_groups": len(groups) - 1,
        "considered": len(usable),
        "without_evidence": len(ignored),
    }


def to_scraped_scene(agreed, image_data_uri=None):
    """A consensus as the `ScrapedScene` JSON a Stash script scraper returns.

    Only the fields Stash's own model defines: anything else would be ignored, and
    guessing at extra keys is how a scraper starts silently dropping data.
    """
    if not agreed:
        return None
    fields = agreed.get("fields") or {}
    payload = {}

    for field in SINGULAR:
        entry = fields.get(field)
        if entry and entry.get("value"):
            payload[field] = entry["value"]

    studio = fields.get("studio")
    if studio and studio.get("value"):
        payload["studio"] = {"name": studio["value"]}

    for field, key in (("performers", "performers"), ("tags", "tags")):
        values = fields.get(field) or []
        if values:
            payload[key] = [{"name": one["name"]} for one in values if one.get("name")]

    urls = [one["url"] for one in (agreed.get("urls") or []) if one.get("url")]
    if urls:
        payload["urls"] = urls
        # Stash still reads the deprecated singular field in places, so set both.
        payload["url"] = urls[0]

    if image_data_uri:
        payload["image"] = image_data_uri

    return payload or None
