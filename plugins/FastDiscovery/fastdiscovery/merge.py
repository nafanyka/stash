"""Turning a run's stored results into the review matrix.

The matrix is the product. Stash's own merge dialog compares CURRENT against one
scraper; this compares CURRENT against every stash-box and every URL scraper result at
once, and the only thing that makes that readable is being strict about three rules:

* **one logical value, many sources.** Four sources agreeing on a date is one row entry
  with four source references, never four entries (requirement 18). The comparison key
  decides what "the same" means, and it is never what gets written - the raw value is
  (requirement 34).
* **a row that is empty everywhere does not exist.** If neither the scene nor any
  source has a director, there is no director row (requirement 9, 33). A value on the
  scene alone is still a row: it is what apply would keep.
* **an entity is resolved, never invented.** A performer, tag, studio or group that
  Stash already matched to a local record is offered as that record; one it did not is
  offered as a candidate that does not exist yet and is not selected by default
  (requirements 12-15).

Nothing here writes anything, to the scene or to the database. It is a pure function of
the run's stored results plus the scene as it stands right now, so the review can be
rebuilt at any time and always reflects the current scene.
"""

from __future__ import annotations

import hashlib

from . import fields, urls as urls_module
from .db import repo as R

CURRENT = "current"


def option_id(prefix, key):
    """A stable id for one option, derived from what the option *is*.

    Deliberately not a position. The review is rebuilt from the stored results twice -
    once to show it, once when Apply resolves the selection against the live scene -
    and between the two, an entity can move: the name lookup that decides whether a
    candidate is really an existing record talks to Stash, and a scraper's answer that
    was unmatched at review time can be matched at apply time. With positional ids,
    that reshuffle would silently point a tick at a different performer. Hashing the
    identity instead means an option keeps its id for as long as it is the same thing,
    and an option that genuinely changed identity comes back as an unknown id, which
    Apply refuses rather than guesses at.
    """
    digest = hashlib.sha1(str(key).encode("utf-8", "replace")).hexdigest()[:10]
    return "%s_%s" % (prefix, digest)

# How an entity was identified, best first. Only the first three are evidence; a name
# on its own is a suggestion, and is never enough to merge two entities silently
# (requirement 13).
BY_LOCAL_ID = "local_id"
BY_STASH_ID = "stash_id"
BY_URL = "url"
BY_NAME = "name"
# Not called this, but answers to it. Weaker than a name, and deliberately last: it is
# also the only way to resolve a name Stash will not let anything be created under.
BY_ALIAS = "alias"


def names_of(row):
    """Every name a local record answers to: its own, and its aliases.

    Three shapes, because Stash has three: tags and studios carry `aliases` as a list,
    performers carry `alias_list`, and a group's `aliases` is one comma-separated
    string.
    """
    values = [row.get("name")]
    raw = row.get("aliases")
    if raw is None:
        raw = row.get("alias_list")
    if isinstance(raw, str):
        raw = raw.split(",")
    values.extend(raw or [])
    return [str(one).strip() for one in values if one and str(one).strip()]


def answers_to(row, canon):
    return any(fields.canon_name(one) == canon for one in names_of(row))


def column_id(source_id, ordinal=0):
    return "s%s_%s" % (source_id, ordinal)


def build(repo, run, scene, schema_fields=None, client=None, rejected=None):
    """The whole review payload for one run.

    `scene` is the scene as Stash has it *now*, not as the run recorded it: the review
    exists to be acted on later, possibly much later, and comparing against a stale
    snapshot would offer to "keep" a value that is no longer there.

    `rejected` is the set of column ids the reviewer struck out. A result that matched
    the wrong scene is wrong about every field at once, so rejecting it drops all of its
    values from every row - and with them their votes, their entities and their images -
    rather than leaving the reviewer to untick twenty things. A column, not a source:
    one source can answer with several results, and the second can be a different film
    entirely while the first is right. The column stays in the table, greyed out,
    because a decision you cannot see is one you cannot take back.
    """
    rejected = {str(one) for one in (rejected or [])}
    snapshot = fields.scene_snapshot(scene)
    sources = repo.sources_of(run["id"])
    results = repo.results_of(run["id"])

    # The scene itself can never be rejected: dropping what the library already has is
    # what unticking a value does, and it is the one thing every other column is
    # compared against.
    columns = [{"id": CURRENT, "type": CURRENT, "name": "Current", "source_id": None,
                "rejected": False,
                "url": None, "endpoint": None, "depth": 0, "attribution": "CERTAIN",
                "parent": None, "scraper_id": None, "result_ordinal": 0}]
    by_source = {source["id"]: source for source in sources}
    for result in results:
        source = by_source.get(result["source_id"], {})
        key = column_id(result["source_id"], result["ordinal"])
        columns.append({
            "id": key,
            "rejected": key in rejected,
            "type": result["source_type"],
            "name": _column_name(source, result),
            "source_id": result["source_id"],
            "result_id": result["id"],
            "result_ordinal": result["ordinal"],
            "url": result["source_url"],
            "endpoint": result["source_endpoint"],
            "scraper_id": result["source_scraper_id"],
            "method": result["source_method"],
            "attribution": result["source_attribution"],
            "depth": result["source_depth"],
            "parent": _parent_column(result, by_source, results),
        })

    payloads = {CURRENT: snapshot["values"]}
    endpoints = {CURRENT: None}
    for result in results:
        key = column_id(result["source_id"], result["ordinal"])
        if key in rejected:
            continue          # its values take no part in any row
        payloads[key] = result["raw"]
        endpoints[key] = result["source_endpoint"]

    known = list(fields.FIELDS) + fields.extra_fields(schema_fields)
    rows = []
    for field in sorted(known, key=lambda one: one.order):
        row = _row(field, columns, payloads, endpoints, snapshot, results, client)
        if row is not None:
            rows.append(row)

    return {
        "run": _run_summary(run),
        "scene": {"id": snapshot["scene_id"], "title": snapshot["display_title"],
                  "filename": snapshot["filename"], "path": snapshot["path"],
                  "screenshot": snapshot["screenshot"],
                  "updated_at": snapshot["updated_at"]},
        "columns": columns,
        "sources": [_source_summary(source) for source in sources],
        "rejected_columns": sorted(rejected),
        "rows": rows,
        "urls_graph": _graph(repo, run, by_source, results),
    }


# ------------------------------------------------------------------ columns

def _column_name(source, result):
    """What the column header says.

    A URL scraper gets its scraper's name; two URLs through the same scraper are two
    columns, distinguished by the URL, because they are two independent answers
    (requirement 5, 52). The UI shows the shortened URL under the name.
    """
    name = source.get("name") or result.get("source_name") or "?"
    # A source that answered with a list - a stash-box name search - contributes one
    # column per answer, numbered, because they are alternatives and not one result.
    if result["ordinal"]:
        return "%s #%d" % (name, result["ordinal"] + 1)
    return name


def _parent_column(result, by_source, results):
    """The column whose result led to this one, for the discovery graph."""
    source = by_source.get(result["source_id"]) or {}
    parent_source = source.get("parent_source_id")
    if not parent_source:
        return None
    for candidate in results:
        if candidate["source_id"] == parent_source:
            return column_id(candidate["source_id"], candidate["ordinal"])
    return None


def _source_summary(source):
    return {
        "id": source["id"], "type": source["type"], "name": source["name"],
        "method": source["method"], "status": source["status"],
        "error": source["error"], "url": source["url"], "endpoint": source["endpoint"],
        "scraper_id": source["scraper_id"], "depth": source["depth"],
        "attribution": source["attribution"], "handlers": source.get("handlers") or [],
        "duration_ms": source["duration_ms"], "result_count": source["result_count"],
    }


def _run_summary(run):
    return {key: run.get(key) for key in
            ("id", "scene_id", "status", "trigger", "started_at", "finished_at",
             "source_count", "ok_source_count", "error_count", "url_count",
             "result_count", "max_depth_reached", "stop_reason", "error", "reviewable",
             "purged")}


# ------------------------------------------------------------------ rows

def _row(field, columns, payloads, endpoints, snapshot, results, client):
    if field.kind == fields.IMAGE:
        return _image_row(field, columns, payloads, snapshot, results)
    if field.kind == fields.STASH_ID:
        return _stash_id_row(field, columns, payloads, endpoints, snapshot)
    if field.kind in (fields.ENTITY, fields.ENTITY_LIST):
        return _entity_row(field, columns, payloads, endpoints, snapshot, client)
    if field.kind == fields.URL_LIST:
        return _url_row(field, columns, payloads)
    return _scalar_row(field, columns, payloads)


def _raw_value(field, column, payloads):
    payload = payloads.get(column["id"]) or {}
    key = field.scene_key if column["id"] == CURRENT else field.result_key
    if not key:
        return None
    value = payload.get(key)
    if value is None and column["id"] != CURRENT and field.name == "urls":
        value = payload.get("url")
    return value


def _scalar_row(field, columns, payloads):
    values, index, cells = [], {}, {}
    for column in columns:
        raw = _raw_value(field, column, payloads)
        display = fields.display_scalar(field, raw)
        if display is None:
            cells[column["id"]] = None
            continue
        key = fields.scalar_key(field, raw)
        entry = index.get(key)
        if entry is None:
            entry = {"id": option_id("v", key), "key": key, "display": display,
                     "raw": raw if not isinstance(raw, (dict, list)) else display,
                     "sources": []}
            index[key] = entry
            values.append(entry)
        entry["sources"].append(column["id"])
        cells[column["id"]] = entry["id"]

    if not values:
        return None
    for entry in values:
        entry["is_current"] = CURRENT in entry["sources"]
    return {
        "field": field.name, "kind": field.kind, "label": field.label,
        "writable": field.writable, "note": field.note,
        "values": values, "cells": cells,
        "default": _default_scalar(values),
    }


def _default_scalar(values):
    """What is selected before the user touches anything.

    The scene's own value, always, when it has one: FastDiscovery must never look like
    it improved a field the user had already filled in. When the scene has nothing and
    exactly one answer exists, that answer is pre-selected, because there is no choice
    to make; when several disagree, nothing is, so a value is only ever written because
    somebody picked it (requirement 35).
    """
    current = next((entry for entry in values if entry["is_current"]), None)
    if current is not None:
        return current["id"]
    return values[0]["id"] if len(values) == 1 else None


# The fields of a scraped result that are the scene's own address. A URL a scraper
# mentioned in its `details` prose is a discovery lead - it is followed, and it is in
# the graph - but it is not a claim that the scene lives there, so offering it as a
# scene URL (ticked, by default) would write somebody's "see also" into the library.
URL_FIELDS = ("urls", "url")


def _url_row(field, columns, payloads):
    """URLs are a union, not a choice: every source contributes to one list (req. 16)."""
    values, index, cells = [], {}, {}
    for column in columns:
        payload = payloads.get(column["id"]) or {}
        found = []
        if column["id"] == CURRENT:
            found = list(payload.get("urls") or [])
        else:
            for entry in urls_module.from_result(payload):
                if entry["role"] == urls_module.ROLE_SCENE \
                        and entry["source"] in URL_FIELDS:
                    found.append(entry["url"])
        seen_here = []
        for raw in found:
            record = urls_module.normalize(raw)
            if not record:
                continue
            entry = index.get(record["key"])
            if entry is None:
                entry = {"id": option_id("u", record["key"]), "key": record["key"],
                         "display": record["url"], "raw": record["url"],
                         "host": record["host"], "sources": []}
                index[record["key"]] = entry
                values.append(entry)
            if column["id"] not in entry["sources"]:
                entry["sources"].append(column["id"])
            if entry["id"] not in seen_here:
                seen_here.append(entry["id"])
        cells[column["id"]] = seen_here

    if not values:
        return None
    for entry in values:
        entry["is_current"] = CURRENT in entry["sources"]
    return {
        "field": field.name, "kind": field.kind, "label": field.label,
        "writable": field.writable, "note": field.note,
        "values": values, "cells": cells,
        # Every distinct URL, ticked. They are additive and cheap to be wrong about,
        # and a URL nobody wants is one click away from being unticked.
        "default": [entry["id"] for entry in values],
    }


def _stash_id_row(field, columns, payloads, endpoints, snapshot):
    """Stash IDs, paired with the endpoint that reported them.

    A stash-box result's `remote_site_id` only means anything together with the box it
    came from, which FastDiscovery knows because it invoked that box by endpoint.
    """
    values, index, cells = [], {}, {}
    for column in columns:
        payload = payloads.get(column["id"]) or {}
        found = []
        if column["id"] == CURRENT:
            found = [(entry.get("endpoint"), entry.get("stash_id"))
                     for entry in (payload.get("stash_ids") or [])
                     if isinstance(entry, dict)]
        elif endpoints.get(column["id"]) and payload.get("remote_site_id"):
            found = [(endpoints[column["id"]], payload["remote_site_id"])]

        here = []
        for endpoint, stash_id in found:
            if not endpoint or not stash_id:
                continue
            key = "%s|%s" % (endpoint, stash_id)
            entry = index.get(key)
            if entry is None:
                entry = {"id": option_id("x", key), "key": key,
                         "display": "%s @ %s" % (stash_id, _short_endpoint(endpoint)),
                         "endpoint": endpoint, "stash_id": str(stash_id), "sources": []}
                index[key] = entry
                values.append(entry)
            if column["id"] not in entry["sources"]:
                entry["sources"].append(column["id"])
            here.append(entry["id"])
        cells[column["id"]] = here

    if not values:
        return None
    for entry in values:
        entry["is_current"] = CURRENT in entry["sources"]
    return {
        "field": field.name, "kind": field.kind, "label": field.label,
        "writable": field.writable, "note": field.note,
        "values": values, "cells": cells,
        # A scene can hold exactly one stash id per box - Stash has a unique index on
        # (scene_id, endpoint) - so two ids for the same endpoint are alternatives, not
        # additions. Picking one has to drop the other, or the write fails at the
        # database with a constraint violation nobody can act on.
        "exclusive_by": "endpoint",
        # A stash id is an identity claim about the scene, so only the ones already on
        # it are ticked. Adopting a new one is a deliberate act.
        "default": [entry["id"] for entry in values if entry["is_current"]],
    }


def _short_endpoint(endpoint):
    text = str(endpoint or "")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.split("/", 1)[0]


# ------------------------------------------------------------------ images

def _image_row(field, columns, payloads, snapshot, results):
    """Every distinct cover on offer, de-duplicated by content where possible.

    Two sources returning the same URL are one candidate; two returning identical bytes
    are one candidate, because the bytes were hashed on the way in (requirement 53).
    Nothing is loaded here: a candidate carries a reference the UI resolves lazily.
    """
    by_result = {result["id"]: result for result in results}
    candidates, index, cells = [], {}, {}

    if snapshot.get("screenshot"):
        entry = {"id": option_id("i", "current"), "key": "current", "kind": "scene",
                 "url": snapshot["screenshot"], "sha256": None, "sources": [CURRENT]}
        candidates.append(entry)
        index["current"] = entry
        cells[CURRENT] = entry["id"]
    else:
        cells[CURRENT] = None

    for column in columns:
        if column["id"] == CURRENT:
            continue
        result = None if column.get("rejected") else by_result.get(column.get("result_id"))
        if result is None:
            cells[column["id"]] = None
            continue
        key = kind = url = sha = None
        if result["image_sha256"]:
            key, kind, sha = "b:" + result["image_sha256"], "blob", result["image_sha256"]
        elif result["image_url"]:
            record = urls_module.normalize(result["image_url"])
            key = "u:" + (record["key"] if record else result["image_url"])
            kind, url = "url", result["image_url"]
        if key is None:
            cells[column["id"]] = None
            continue
        entry = index.get(key)
        if entry is None:
            entry = {"id": option_id("i", key), "key": key, "kind": kind,
                     "url": url, "sha256": sha, "sources": []}
            index[key] = entry
            candidates.append(entry)
        entry["sources"].append(column["id"])
        cells[column["id"]] = entry["id"]

    if not candidates:
        return None
    for entry in candidates:
        entry["is_current"] = CURRENT in entry["sources"]
    return {
        "field": field.name, "kind": field.kind, "label": field.label,
        "writable": field.writable, "note": field.note,
        "values": candidates, "cells": cells,
        "default": next((entry["id"] for entry in candidates if entry["is_current"]),
                        None),
    }


# ------------------------------------------------------------------ entities

def _entity_occurrences(field, columns, payloads, endpoints, snapshot):
    """Every mention of an entity, tagged with the column that made it."""
    out = []
    for column in columns:
        payload = payloads.get(column["id"]) or {}
        key = field.scene_key if column["id"] == CURRENT else field.result_key
        value = payload.get(key) if key else None
        if column["id"] != CURRENT and field.name == "groups" and not value:
            value = payload.get("movies")
        entries = value if isinstance(value, list) else ([value] if value else [])
        for entry in entries:
            record = _entity_record(entry, column, endpoints.get(column["id"]))
            if record is not None:
                out.append(record)
    return out


def _entity_record(entry, column, endpoint):
    if isinstance(entry, str):
        entry = {"name": entry}
    if not isinstance(entry, dict):
        return None
    name = fields.clean(entry.get("name"))
    if not name:
        return None

    # On the scene the local id is `id`; in a scraped result Stash puts the id it
    # matched into `stored_id` (pkg/match/scraped.go). Either way it means the same
    # thing: this is an entity the library already has.
    stored_id = fields.clean(entry.get("stored_id") or entry.get("id"))
    entity_urls = []
    for value in ([entry.get("url")] + list(entry.get("urls") or [])):
        record = urls_module.normalize(value) if value else None
        if record and record["key"] not in entity_urls:
            entity_urls.append(record["key"])

    return {
        "column": column["id"],
        "on_scene": column["id"] == CURRENT,
        "name": name,
        "canon": fields.canon_name(name),
        "stored_id": stored_id,
        "disambiguation": fields.clean(entry.get("disambiguation")),
        "gender": fields.clean(entry.get("gender")),
        "endpoint": endpoint,
        "remote_site_id": fields.clean(entry.get("remote_site_id")),
        "url_keys": entity_urls,
        "urls": [value for value in ([entry.get("url")] + list(entry.get("urls") or []))
                 if urls_module.is_safe(value)],
        "image": entry.get("image") if isinstance(entry.get("image"), str) else None,
        "scene_index": entry.get("scene_index"),
    }


def _identity_keys(occurrence):
    """The evidence that two mentions are the same entity, strongest first."""
    keys = []
    if occurrence["stored_id"]:
        keys.append((BY_LOCAL_ID, "local:%s" % occurrence["stored_id"]))
    if occurrence["endpoint"] and occurrence["remote_site_id"]:
        keys.append((BY_STASH_ID, "stash:%s|%s" % (occurrence["endpoint"],
                                                   occurrence["remote_site_id"])))
    for url_key in occurrence["url_keys"]:
        keys.append((BY_URL, "url:%s" % url_key))
    return keys


def _group_entities(occurrences):
    """Merge mentions into entities, conservatively.

    Union-find over the strong keys only: a shared local id, a shared (endpoint,
    stash_id), or a shared canonical URL. Two mentions that agree on nothing but a name
    are *not* merged - names collide, and silently fusing two people is a worse failure
    than showing one twice (requirement 13). Mentions with no strong key at all are
    grouped by name among themselves, because there they are all the same evidence-free
    suggestion, and showing "Some New Person" five times helps nobody.
    """
    parent = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    nodes = list(range(len(occurrences)))
    for node in nodes:
        parent[node] = node

    by_key = {}
    for node, occurrence in enumerate(occurrences):
        strong = _identity_keys(occurrence)
        for _kind, key in strong:
            if key in by_key:
                union(by_key[key], node)
            else:
                by_key[key] = node
        if not strong:
            weak = "weak:%s|%s" % (occurrence["canon"],
                                   fields.canon_text(occurrence["disambiguation"]))
            if weak in by_key:
                union(by_key[weak], node)
            else:
                by_key[weak] = node

    grouped = {}
    for node, occurrence in enumerate(occurrences):
        grouped.setdefault(find(node), []).append(occurrence)
    return list(grouped.values())


def _entity_row(field, columns, payloads, endpoints, snapshot, client):
    occurrences = _entity_occurrences(field, columns, payloads, endpoints, snapshot)
    if not occurrences:
        return None

    entities = [_entity(field, members)
                for members in _group_entities(occurrences)]
    # Order matters: the name lookup can turn a candidate into an existing record,
    # which is priority-one evidence and can make two groups one, and the ids the
    # duplicate flag points at must be the final ones the UI will see.
    _link_by_name(field, entities, client)
    entities = _merge_by_stored_id(entities)
    # Display order only. The ids were fixed by identity above and are not touched
    # here, so a selection made before this row was last built still resolves.
    entities.sort(key=lambda one: (not one["on_scene"], not one["existing"],
                                   one["name"].casefold()))
    _flag_possible_duplicates(entities)

    single = field.kind == fields.ENTITY

    # A cell's shape follows the row's: a single-choice row maps a column to one option
    # id or None, exactly like a scalar or an image row, and a list row maps it to the
    # list of options that column contributed. Handing a one-element list to a
    # single-choice row would put a list where the selection expects an id, which is
    # only noticed later, when Apply cannot resolve it.
    cells = {}
    for column in columns:
        here = [entity["id"] for entity in entities
                if column["id"] in entity["sources"]]
        # A scene has one studio and a scraper reports one, so `here` holds at most one
        # entry for a single-choice row; taking the first is a guard, not a policy.
        cells[column["id"]] = (here[0] if here else None) if single else here

    if single:
        default = next((entity["id"] for entity in entities if entity["on_scene"]), None)
        if default is None and len(entities) == 1 and entities[0]["existing"]:
            default = entities[0]["id"]
    else:
        # On the scene already, or a record the library already has: ticked. A
        # candidate that would have to be created: not, ever, by default
        # (requirements 12, 14, 35, 49).
        default = [entity["id"] for entity in entities
                   if entity["on_scene"] or entity["existing"]]

    return {
        "field": field.name, "kind": field.kind, "label": field.label,
        "writable": field.writable, "note": field.note, "entity": field.entity,
        "values": entities, "cells": cells, "default": default,
    }


def _entity_identity(members):
    """The key an entity keeps for as long as it is the same entity.

    Built only from the stored results and the scene - never from the live name lookup,
    which can turn a candidate into an existing record between one build of the review
    and the next. That is exactly the moment an id must not move.
    """
    strong = sorted({key for member in members for _kind, key in _identity_keys(member)})
    if strong:
        return "|".join(strong)
    first = members[0]
    return "name:%s|%s" % (first["canon"], fields.canon_text(first["disambiguation"]))


def _entity(field, members):
    on_scene = any(member["on_scene"] for member in members)
    stored_id = next((member["stored_id"] for member in members
                      if member["stored_id"]), None)
    # The display name comes from the scene when it has one - the library's own
    # spelling wins over a scraper's - and otherwise from the first source that gave one.
    name = next((member["name"] for member in members if member["on_scene"]),
                members[0]["name"])

    remote, seen_remote = [], set()
    for member in members:
        if member["endpoint"] and member["remote_site_id"]:
            key = (member["endpoint"], member["remote_site_id"])
            if key not in seen_remote:
                seen_remote.add(key)
                remote.append({"endpoint": member["endpoint"],
                               "stash_id": member["remote_site_id"]})

    entity_urls = []
    for member in members:
        for value in member["urls"]:
            if value not in entity_urls:
                entity_urls.append(value)

    identity = BY_NAME
    if stored_id:
        identity = BY_LOCAL_ID
    elif remote:
        identity = BY_STASH_ID
    elif any(member["url_keys"] for member in members):
        identity = BY_URL

    return {
        "id": option_id("e", _entity_identity(members)),
        "merged_ids": [],
        "name": name,
        "canon": members[0]["canon"],
        "kind": field.entity,
        "stored_id": stored_id,
        "existing": bool(stored_id),
        "matched_by": identity,
        "on_scene": on_scene,
        "disambiguation": next((member["disambiguation"] for member in members
                                if member["disambiguation"]), None),
        "gender": next((member["gender"] for member in members
                        if member["gender"]), None),
        "image": next((member["image"] for member in members if member["image"]), None),
        "urls": entity_urls,
        "remote_ids": remote,
        "scene_index": next((member["scene_index"] for member in members
                             if member.get("scene_index") is not None), None),
        "aliases": sorted({member["name"] for member in members
                           if member["name"] != name}),
        "sources": sorted({member["column"] for member in members}),
        "possible_match": None,
    }


_LOOKUP = {
    "performer": "find_performers_by_names",
    "tag": "find_tags_by_names",
    "studio": "find_studios_by_names",
    "group": "find_groups_by_names",
}

# A ceiling on the name lookups one review may cost. The review is served by a plugin
# process the browser is waiting on, and a scene with three hundred scraped tags should
# not turn into three hundred GraphQL round trips.
#
# This is a *labelling* budget, not a correctness one: an entity past the ceiling is
# shown as a candidate even though the library may already have it. Apply looks every
# candidate up again, without a ceiling, immediately before creating it, so a stale
# label costs a wrong word in the table and never a duplicate.
MAX_NAME_LOOKUPS = 100


def find_local_entity(client, kind, name, canon=None):
    """The local record that answers to a name, but only when there is exactly one.

    The same rule Stash applies when it fills in `stored_id` (`pkg/match/scraped.go`):
    an exact match, and a unique one. Two records sharing a name is not a match - it is
    a question only the user can answer - so this returns None and the entity stays a
    candidate.

    Aliases count, because they count to Stash: it will not create a tag whose name is
    another tag's alias, so a name only an alias owns has exactly one honest resolution,
    and it is that record.
    """
    method = getattr(client, _LOOKUP.get(kind, ""), None) if client else None
    if method is None or not name:
        return None
    canon = canon if canon is not None else fields.canon_name(name)
    try:
        found = method([name]) or {}
    except Exception:
        # A lookup failing must never be the reason a review or an apply falls over.
        return None
    exact = [row for row in (found.get(name) or []) if answers_to(row, canon)]
    return exact[0] if len(exact) == 1 else None


def _link_by_name(field, entities, client):
    """Ask Stash whether an unmatched candidate is really an existing record.

    Stash sets `stored_id` itself when a scrape matched a local entity by stash id,
    name or alias, and only when the match was unique (`pkg/match/scraped.go`). But a
    URL scraper that returned a bare name gets no such treatment, so the same performer
    can arrive matched from one source and unmatched from another. Asking by exact name
    closes that gap using Stash's own index, and only ever *offers* the link: a unique
    hit becomes an existing entity, an ambiguous one stays a candidate with the
    alternatives attached, so nothing is fused on a name alone.
    """
    if client is None or not field.entity:
        return
    method = getattr(client, _LOOKUP.get(field.entity, ""), None)
    if method is None:
        return
    unmatched = [entity for entity in entities if not entity["existing"]]
    if not unmatched:
        return
    names = []
    for entity in unmatched[:MAX_NAME_LOOKUPS]:
        if entity["name"] not in names:
            names.append(entity["name"])
    try:
        found = method(names) or {}
    except Exception:  # a lookup failing must not cost the user the review
        return
    for entity in unmatched:
        rows = found.get(entity["name"]) or []
        exact = [row for row in rows if answers_to(row, entity["canon"])]
        if len(exact) == 1:
            _link_to(entity, exact[0])
        elif len(exact) > 1:
            entity["ambiguous_matches"] = [
                {"id": str(row["id"]), "name": row.get("name"),
                 "disambiguation": row.get("disambiguation")} for row in exact]
        elif entity["name"] not in found:
            # Past the lookup ceiling: not asked about, so not known to be missing.
            entity["unchecked"] = True


def _link_to(entity, row):
    """Bind a candidate to the local record that turned out to own its name.

    When the record owns the name as an *alias*, the library's own spelling wins and
    the scraped name becomes one more alias of the option. Anything else would show the
    reviewer one name and put a differently-named record on the scene.
    """
    entity["stored_id"] = str(row["id"])
    entity["existing"] = True
    by_name = fields.canon_name(row.get("name")) == entity["canon"]
    entity["matched_by"] = BY_NAME if by_name else BY_ALIAS
    if by_name or not row.get("name"):
        return
    entity["alias_of"] = entity["name"]
    entity["aliases"] = sorted({name for name in
                                entity["aliases"] + [entity["name"]]
                                if name != row["name"]})
    entity["name"] = row["name"]


def _merge_by_stored_id(entities):
    """Fold together entities that turn out to be the same local record.

    Grouping runs before Stash is asked about the unmatched names, so a performer the
    scene already has and the same performer arriving as a bare name start as two
    groups. Once the lookup gives the second one a local id, they are the same record
    by the strongest rule there is, and showing them twice would invite the user to add
    somebody who is already there.
    """
    by_stored_id, out = {}, []
    for entity in entities:
        stored_id = entity.get("stored_id")
        if not stored_id:
            out.append(entity)
            continue
        twin = by_stored_id.get(stored_id)
        if twin is None:
            by_stored_id[stored_id] = entity
            out.append(entity)
            continue
        # The absorbed entity's id is remembered, so a tick made when the two were
        # still separate still points at something after they became one.
        twin["merged_ids"] = sorted(set(twin["merged_ids"] + entity["merged_ids"]
                                        + [entity["id"]]))
        twin["on_scene"] = twin["on_scene"] or entity["on_scene"]
        twin["sources"] = sorted(set(twin["sources"]) | set(entity["sources"]))
        twin["aliases"] = sorted({name for name in twin["aliases"] + entity["aliases"]
                                  + [entity["name"]] if name != twin["name"]})
        twin["urls"] = twin["urls"] + [url for url in entity["urls"]
                                       if url not in twin["urls"]]
        for remote in entity["remote_ids"]:
            if remote not in twin["remote_ids"]:
                twin["remote_ids"].append(remote)
        twin["image"] = twin["image"] or entity["image"]
        twin["disambiguation"] = twin["disambiguation"] or entity["disambiguation"]
        twin["gender"] = twin["gender"] or entity["gender"]
    return out


def _flag_possible_duplicates(entities):
    """Say when a candidate looks like an entity that is already in the list.

    Shown, never acted on: the user gets "this may be the same as X" rather than
    FastDiscovery quietly deciding it is (requirement 13).
    """
    by_name = {}
    for entity in entities:
        if entity["existing"]:
            by_name.setdefault(entity["canon"], entity)
    for entity in entities:
        if entity["existing"]:
            continue
        twin = by_name.get(entity["canon"])
        if twin is not None and twin["id"] != entity["id"]:
            entity["possible_match"] = {"id": twin["id"], "name": twin["name"],
                                        "stored_id": twin["stored_id"]}


# ------------------------------------------------------------------ graph

def _graph(repo, run, by_source, results):
    """Where every URL came from, flat enough to render as a list or a tree."""
    result_source = {result["id"]: result["source_id"] for result in results}
    out = []
    for record in repo.urls_of(run["id"]):
        parent_source = result_source.get(record["discovered_by_result_id"])
        out.append({
            "url": record["url"], "normalized": record["normalized"],
            "host": record["host"], "depth": record["depth"], "role": record["role"],
            "state": record["state"], "note": record["note"],
            "origin": record["origin"],
            "handlers": record.get("handler_ids") or [],
            "found_by": (by_source.get(parent_source) or {}).get("name") if parent_source
            else "scene",
        })
    return out


# ------------------------------------------------------------------ selection

def default_selection(review):
    """The selection the review starts with, as apply expects to receive it."""
    return {row["field"]: row["default"] for row in review["rows"] if row["writable"]}


def sanitise_selection(review, selection):
    """A selection kept honest against the matrix it is about to be applied to.

    Rejecting a source removes whatever only it said, so a tick can be left pointing at
    an option that is no longer on offer. Dropping those quietly is right - the user
    took the value away by taking its source away - but a scalar left with nothing
    selected would silently write nothing, so it falls back to that row's default.
    """
    selection = selection if isinstance(selection, dict) else {}
    out = {}
    for row in review["rows"]:
        if row["field"] not in selection:
            continue
        known = {value["id"] for value in row["values"]}
        for value in row["values"]:
            known.update(value.get("merged_ids") or [])
        chosen = selection[row["field"]]
        if isinstance(chosen, list):
            out[row["field"]] = [one for one in chosen if one in known]
        elif chosen in known:
            out[row["field"]] = chosen
        else:
            out[row["field"]] = row["default"] if not isinstance(row["default"], list)                 else []
    return out


def selection_signatures(review, selection):
    """What is ticked, described by *what* it is rather than by the id it was ticked as.

    An option id is a hash of the option's identity, and for an entity that identity is
    built from every source that mentioned it. Striking one column out therefore changes
    the id of an entity the two remaining columns still agree on, and carrying the
    selection across by id alone would untick it - taking away a choice the reviewer
    made and never revisited. A signature describes the value, not the evidence for it,
    so it survives the rebuild.
    """
    selection = selection if isinstance(selection, dict) else {}
    out = {}
    for row in review["rows"]:
        if row["field"] not in selection:
            continue
        by_id = {}
        for value in row["values"]:
            by_id[value["id"]] = value
            for absorbed in value.get("merged_ids") or []:
                by_id.setdefault(absorbed, value)
        chosen = selection[row["field"]]
        if isinstance(chosen, list):
            out[row["field"]] = [_signature(row, by_id[one]) for one in chosen
                                 if one in by_id]
        elif chosen in by_id:
            out[row["field"]] = _signature(row, by_id[chosen])
    return out


def carry_selection(review, signatures):
    """Signatures turned back into the option ids of *this* build of the review.

    A signature with nothing to match is dropped, which is the right answer: whatever it
    described was only ever offered by a source that is no longer counted.
    """
    out = {}
    for row in review["rows"]:
        if row["field"] not in signatures:
            continue
        here = {}
        for value in row["values"]:
            here.setdefault(_signature(row, value), value["id"])
        wanted = signatures[row["field"]]
        if isinstance(wanted, list):
            out[row["field"]] = [here[one] for one in wanted if one in here]
        elif wanted in here:
            out[row["field"]] = here[wanted]
    return out


def _signature(row, value):
    """What an option is, in a form that does not depend on who said it.

    Entities are described by name rather than by local id on purpose: the id can be
    filled in by the name lookup between one build and the next, and a signature that
    moved when that happened would defeat the whole point.
    """
    if row["kind"] in (fields.ENTITY, fields.ENTITY_LIST):
        return "entity:%s|%s" % (value.get("canon"),
                                 fields.canon_text(value.get("disambiguation")))
    return "value:%s" % value.get("key")


def summarise(review):
    """The couple of numbers the scene page and the results list show."""
    rows = review["rows"]
    return {
        "columns": len([one for one in review["columns"] if not one.get("rejected")]),
        "rejected_columns": len([one for one in review["columns"]
                                 if one.get("rejected")]),
        "sources": len(review["sources"]),
        "failed_sources": len([one for one in review["sources"]
                               if one["status"] in (R.S_ERROR, R.S_TIMEOUT)]),
        "unreachable_sources": len([one for one in review["sources"]
                                    if one["status"] == R.S_UNREACHABLE]),
        "fields": len(rows),
        "urls": len(review["urls_graph"]),
        "new_entities": sum(len([entity for entity in row["values"]
                                 if not entity.get("existing")])
                            for row in rows
                            if row["kind"] in (fields.ENTITY, fields.ENTITY_LIST)),
    }
