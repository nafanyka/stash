"""Turning a reviewed selection into exactly one write, and then forgetting everything.

This is the only module in FastDiscovery that changes anything in Stash, and it only
ever runs because somebody pressed Apply. Everything before it - the run, the results,
the review - leaves the scene untouched (requirements 7, 57).

The sequence, and the reasoning behind each step:

1. **rebuild the review against the live scene.** The selection arrives as value ids,
   which only mean something against the matrix they came from, and the scene may have
   changed since it was rendered. Rebuilding both resolves the ids and makes the
   change set true.
2. **drop the no-ops.** A field whose selected value is what the scene already has is
   not written. So the audit says what actually changed, and a stray field cannot be
   rewritten with its own value.
3. **create only what was explicitly ticked.** A performer, tag, studio or group that
   does not exist locally is created only if the user selected that candidate - never
   because it appeared in a result (requirements 12, 14, 15, 49).
4. **one `sceneUpdate`.** Every selected field in a single mutation, so the scene cannot
   end up half-applied. GraphQL gives no transaction across mutations, so the creates
   above happen first and are reported in the audit if the update then fails.
5. **purge on success only.** A failed apply keeps the whole payload and leaves the run
   reviewable, so Apply can simply be pressed again (requirement 20).
"""

from __future__ import annotations

from . import fields, logs, merge as merge_module
from .db import repo as R


class ApplyError(RuntimeError):
    """The selection could not be turned into a write. Nothing was changed."""


def preview(repo, client, run, scene, selection, schema_fields=None):
    """What Apply would do, without doing any of it."""
    review = merge_module.build(repo, run, scene, schema_fields, client)
    plan = _plan(review, selection)
    return {"changes": plan["changes"], "creates": plan["creates"],
            "unchanged": plan["unchanged"], "problems": plan["problems"],
            "scene_updated_at": review["scene"]["updated_at"]}


def commit(repo, client, run, scene, selection, schema_fields=None,
           expected_updated_at=None):
    """Create what was ticked, write the scene once, then drop the payload."""
    review = merge_module.build(repo, run, scene, schema_fields, client)
    live_stamp = review["scene"]["updated_at"]
    if expected_updated_at and live_stamp and str(expected_updated_at) != str(live_stamp):
        # Someone edited the scene between the review being rendered and Apply being
        # pressed. Writing the older intent would silently undo their edit, so the
        # review is handed back refreshed instead.
        raise ApplyError("the scene changed since this review was loaded - reload it "
                         "and check the selection before applying")

    plan = _plan(review, selection)
    if plan["problems"]:
        raise ApplyError("; ".join(plan["problems"]))
    if not plan["changes"] and not plan["creates"]:
        return {"applied": False, "reason": "nothing was selected that would change "
                                            "the scene", "changes": [], "created": {}}

    created = _create_entities(client, plan["creates"])
    values = _scene_update_input(repo, review, plan)
    values["id"] = str(run["scene_id"])

    logs.info("scene %s: applying %s"
              % (run["scene_id"], ", ".join(sorted(one["field"]
                                                   for one in plan["changes"]))))
    logs.debug("scene %s: update touches %s; entities created: %s"
               % (run["scene_id"], sorted(values), _created_summary(created)))

    try:
        client.scene_update(values)
    except Exception as exc:
        from .executor import describe_error
        message = describe_error(exc)
        repo.add_application(run["id"], run["scene_id"], "FAILED",
                             [one["field"] for one in plan["changes"]], created, message)
        repo.set_run_status(run["id"], R.FAILED_APPLY, message)
        raise ApplyError(message)

    repo.add_application(run["id"], run["scene_id"], "APPLIED",
                         [one["field"] for one in plan["changes"]], created)
    repo.set_run_status(run["id"], R.APPLIED)
    repo.purge_run(run["id"])
    return {"applied": True, "changes": plan["changes"], "created": created,
            "fields": sorted(values)}


def reject(repo, run):
    """No write at all, the payload dropped, one audit row kept (requirement 21)."""
    repo.add_application(run["id"], run["scene_id"], "REJECTED")
    repo.set_run_status(run["id"], R.REJECTED)
    repo.purge_run(run["id"])
    return {"rejected": True, "run_id": run["id"]}


# ------------------------------------------------------------------ planning

def _plan(review, selection):
    """Resolve the selection into changes, creations, and anything that made no sense."""
    selection = selection if isinstance(selection, dict) else {}
    changes, creates, unchanged, problems = [], [], [], []

    for row in review["rows"]:
        if not row["writable"]:
            continue
        if row["field"] not in selection:
            continue           # untouched fields are left exactly as they are
        chosen = selection[row["field"]]
        by_id = {value["id"]: value for value in row["values"]}

        if row["kind"] in (fields.SCALAR, fields.IMAGE, fields.ENTITY):
            if chosen is None:
                continue
            value = by_id.get(chosen)
            if value is None:
                problems.append("%s: no such option %r" % (row["field"], chosen))
                continue
            if value.get("is_current") or value.get("on_scene"):
                unchanged.append(row["field"])
                continue
            changes.append({"field": row["field"], "kind": row["kind"],
                            "value_id": value["id"],
                            "display": _display_of(row, value)})
            if row["kind"] == fields.ENTITY and not value.get("stored_id"):
                creates.append({"field": row["field"], "kind": row["entity"],
                                "entity": value})
            continue

        if not isinstance(chosen, list):
            # An absent or null list is "the user did not touch this row", not "empty
            # it". Clearing a list is done by sending an empty list, which is what the
            # UI sends when every box is unticked.
            problems.append("%s: expected a list of choices" % row["field"])
            continue
        wanted = list(chosen)
        missing = [one for one in wanted if one not in by_id]
        if missing:
            problems.append("%s: no such option(s) %s"
                            % (row["field"], ", ".join(str(one) for one in missing)))
            continue
        picked = [by_id[one] for one in wanted]
        present = [value["id"] for value in row["values"]
                   if value.get("is_current") or value.get("on_scene")]
        if sorted(wanted) == sorted(present):
            unchanged.append(row["field"])
            continue
        added = [value for value in picked
                 if not (value.get("is_current") or value.get("on_scene"))]
        removed = [by_id[one] for one in present if one not in wanted]
        changes.append({
            "field": row["field"], "kind": row["kind"],
            "value_ids": wanted,
            "added": [_display_of(row, value) for value in added],
            "removed": [_display_of(row, value) for value in removed],
        })
        for value in picked:
            if row["kind"] == fields.ENTITY_LIST and not value.get("stored_id"):
                creates.append({"field": row["field"], "kind": row["entity"],
                                "entity": value})

    return {"changes": changes, "creates": creates, "unchanged": unchanged,
            "problems": problems}


def _display_of(row, value):
    if row["kind"] in (fields.ENTITY, fields.ENTITY_LIST):
        return value["name"]
    if row["kind"] == fields.IMAGE:
        return value.get("url") or ("image " + str(value.get("sha256"))[:12])
    return value.get("display")


# ------------------------------------------------------------------ creation

def _create_entities(client, creates):
    """Create the ticked candidates, and hand back their new ids.

    A candidate selected twice - the same new performer under two fields - is created
    once. Nothing else is created, ever.
    """
    created = {"performer": [], "tag": [], "studio": [], "group": []}
    seen = {}
    for entry in creates:
        entity = entry["entity"]
        kind = entry["kind"]
        marker = (kind, entity["canon"], entity.get("disambiguation") or "")
        if marker in seen:
            entity["stored_id"] = seen[marker]
            continue
        record = _create_one(client, kind, entity)
        if record is None:
            raise ApplyError("could not create %s %r" % (kind, entity["name"]))
        entity["stored_id"] = str(record["id"])
        seen[marker] = entity["stored_id"]
        created.setdefault(kind, []).append({"id": entity["stored_id"],
                                             "name": entity["name"]})
        logs.info("created %s %s (id %s)" % (kind, entity["name"], entity["stored_id"]))
    return {kind: rows for kind, rows in created.items() if rows}


def _create_one(client, kind, entity):
    values = {"name": entity["name"]}
    urls = [url for url in (entity.get("urls") or []) if url][:10]
    stash_ids = [{"endpoint": one["endpoint"], "stash_id": one["stash_id"]}
                 for one in (entity.get("remote_ids") or [])]

    if kind == "performer":
        if entity.get("disambiguation"):
            values["disambiguation"] = entity["disambiguation"]
        if urls:
            values["urls"] = urls
        if stash_ids:
            values["stash_ids"] = stash_ids
        if entity.get("image"):
            values["image"] = entity["image"]
        return client.create_performer(values)
    if kind == "studio":
        if urls:
            values["urls"] = urls
        if stash_ids:
            values["stash_ids"] = stash_ids
        if entity.get("image"):
            values["image"] = entity["image"]
        return client.create_studio(values)
    if kind == "group":
        return client.create_group(values)
    # Tags carry nothing but a name in a scraped result, and a tag with an invented
    # description would be worse than one without.
    return client.create_tag(values)


# ------------------------------------------------------------------ the write

def _scene_update_input(repo, review, plan):
    """The `SceneUpdateInput` for the whole change set.

    Set-like fields are sent as the exact set the review showed as ticked, which is the
    reviewed intent - the user saw what is on the scene now and what would be added, and
    unticking something is how they remove it. Fields not in the change set are absent
    from the input, so `sceneUpdate` leaves them alone.
    """
    rows = {row["field"]: row for row in review["rows"]}
    values = {}

    for change in plan["changes"]:
        row = rows[change["field"]]
        field = fields.BY_NAME.get(change["field"])
        key = field.update_key if field else row["field"]
        by_id = {value["id"]: value for value in row["values"]}

        if row["kind"] == fields.SCALAR:
            values[key] = _scalar_for_update(change["field"],
                                             by_id[change["value_id"]]["raw"])
        elif row["kind"] == fields.IMAGE:
            image = _image_for_update(repo, by_id[change["value_id"]])
            if image is None:
                raise ApplyError("the selected image is no longer available")
            values[key] = image
        elif row["kind"] == fields.ENTITY:
            values[key] = str(by_id[change["value_id"]]["stored_id"])
        elif row["kind"] == fields.ENTITY_LIST:
            picked = [by_id[one] for one in change["value_ids"]]
            if row["field"] == "groups":
                values[key] = [{"group_id": str(entity["stored_id"]),
                                "scene_index": entity.get("scene_index")}
                               for entity in picked if entity.get("stored_id")]
            else:
                values[key] = [str(entity["stored_id"]) for entity in picked
                               if entity.get("stored_id")]
        elif row["kind"] == fields.URL_LIST:
            values[key] = [by_id[one]["raw"] for one in change["value_ids"]]
        elif row["kind"] == fields.STASH_ID:
            values[key] = [{"endpoint": by_id[one]["endpoint"],
                            "stash_id": by_id[one]["stash_id"]}
                           for one in change["value_ids"]]
    return values


def _scalar_for_update(name, raw):
    if name == "date":
        return fields.parse_date(raw) or fields.clean(raw)
    if name == "rating100":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return fields.clean(raw)


def _image_for_update(repo, value):
    """What `cover_image` gets.

    Stash accepts either a URL or a base64 data URI and fetches the URL itself
    (`utils.ProcessImageInput`), so an image that arrived as a URL is passed on as one -
    FastDiscovery never downloads a cover. An image that arrived as a data URI is
    rebuilt from the blob it was stored as, byte for byte.
    """
    if value.get("kind") in ("url", "scene"):
        return value.get("url")
    blob = repo.image(value.get("sha256")) if value.get("sha256") else None
    if not blob:
        return None
    import base64
    return "data:%s;base64,%s" % (blob["mime"],
                                  base64.b64encode(blob["data"]).decode("ascii"))


def _created_summary(created):
    return {kind: len(rows) for kind, rows in (created or {}).items()}
