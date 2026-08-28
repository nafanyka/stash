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

from . import fields, urls as urls_module
from .db import repo as R

CURRENT = "current"

# How an entity was identified, best first. Only the first three are evidence; a name
# on its own is a suggestion, and is never enough to merge two entities silently
# (requirement 13).
BY_LOCAL_ID = "local_id"
BY_STASH_ID = "stash_id"
BY_URL = "url"
BY_NAME = "name"


def column_id(source_id, ordinal=0):
    return "s%s_%s" % (source_id, ordinal)


def build(repo, run, scene, schema_fields=None, client=None):
    """The whole review payload for one run.

    `scene` is the scene as Stash has it *now*, not as the run recorded it: the review
    exists to be acted on later, possibly much later, and comparing against a stale
    snapshot would offer to "keep" a value that is no longer there.
    """
    snapshot = fields.scene_snapshot(scene)
    sources = repo.sources_of(run["id"])
    results = repo.results_of(run["id"])

    columns = [{"id": CURRENT, "type": CURRENT, "name": "Current", "source_id": None,
                "url": None, "endpoint": None, "depth": 0, "attribution": "CERTAIN",
                "parent": None, "scraper_id": None, "result_ordinal": 0}]
    by_source = {source["id"]: source for source in sources}
    for result in results:
        source = by_source.get(result["source_id"], {})
        columns.append({
            "id": column_id(result["source_id"], result["ordinal"]),
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
                  "filename": snapshot["filename"], "screenshot": snapshot["screenshot"],
                  "updated_at": snapshot["updated_at"]},
        "columns": columns,
        "sources": [_source_summary(source) for source in sources],
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
            entry = {"id": "v%d" % len(values), "key": key, "display": display,
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
                if entry["role"] == urls_module.ROLE_SCENE:
                    found.append(entry["url"])
        seen_here = []
        for raw in found:
            record = urls_module.normalize(raw)
            if not record:
                continue
            entry = index.get(record["key"])
            if entry is None:
                entry = {"id": "u%d" % len(values), "key": record["key"],
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
                entry = {"id": "x%d" % len(values), "key": key,
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
        entry = {"id": "i0", "key": "current", "kind": "scene",
                 "url": snapshot["screenshot"], "sha256": None, "sources": [CURRENT]}
        candidates.append(entry)
        index["current"] = entry
        cells[CURRENT] = entry["id"]
    else:
        cells[CURRENT] = None

    for column in columns:
        if column["id"] == CURRENT:
            continue
        result = by_result.get(column.get("result_id"))
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
            entry = {"id": "i%d" % len(candidates), "key": key, "kind": kind,
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

    entities = [_entity(field, index, members)
                for index, members in enumerate(_group_entities(occurrences))]
    # Order matters: the name lookup can turn a candidate into an existing record,
    # which is priority-one evidence and can make two groups one, and the ids the
    # duplicate flag points at must be the final ones the UI will see.
    _link_by_name(field, entities, client)
    entities = _merge_by_stored_id(entities)
    entities.sort(key=lambda one: (not one["on_scene"], not one["existing"],
                                   one["name"].casefold()))
    for position, entity in enumerate(entities):
        entity["id"] = "e%d" % position
    _flag_possible_duplicates(entities)

    cells = {}
    for column in columns:
        cells[column["id"]] = [entity["id"] for entity in entities
                               if column["id"] in entity["sources"]]

    single = field.kind == fields.ENTITY
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


def _entity(field, index, members):
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
        "id": "e%d" % index,
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
# process the browser is waiting on, and a scene with sixty scraped tags should not turn
# into sixty GraphQL round trips.
MAX_NAME_LOOKUPS = 40


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
        exact = [row for row in rows
                 if fields.canon_name(row.get("name")) == entity["canon"]]
        if len(exact) == 1:
            entity["stored_id"] = str(exact[0]["id"])
            entity["existing"] = True
            entity["matched_by"] = BY_NAME
        elif len(exact) > 1:
            entity["ambiguous_matches"] = [
                {"id": str(row["id"]), "name": row.get("name"),
                 "disambiguation": row.get("disambiguation")} for row in exact]


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


def summarise(review):
    """The couple of numbers the scene page and the results list show."""
    rows = review["rows"]
    return {
        "columns": len(review["columns"]),
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
