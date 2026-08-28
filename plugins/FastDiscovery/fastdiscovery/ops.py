"""The plugin's request/response API, reached with `runPluginOperation`.

A Stash plugin cannot serve HTTP, so this is the whole channel between the UI and the
database: the browser calls `runPluginOperation(plugin_id, args)`, Stash runs the plugin
process, waits, and hands back whatever it printed. One process per call, so every
operation here has to be cheap - which is why discovery itself is never done here. It is
queued as a job with `runPluginTask`, where it gets a progress bar and a stop button.

Failure convention: an operation that could not do what was asked returns
`{"ok": false, "error": "..."}` so the page can show the message in place. Only
something that makes the plugin unusable comes back as a plugin-level error.
"""

from __future__ import annotations

import base64

from . import apply as apply_module, logs, merge as merge_module, settings
from .db import migrations, repo as R

TASK_DISCOVER = "Discover scenes"

# Which run statuses each tab of the FastDiscovery page shows.
TABS = {
    "ready": list(R.REVIEWABLE),
    "running": [R.RUNNING],
    "empty": [R.NO_RESULTS],
    "failed": [R.FAILED, R.FAILED_APPLY, R.CANCELLED],
    "done": [R.APPLIED, R.REJECTED],
}


class Context:
    """What every operation needs, built once per plugin invocation."""

    def __init__(self, client, repo, config):
        self.client = client
        self.repo = repo
        self.config = config
        self._schema_fields = None

    def schema_fields(self):
        if self._schema_fields is None:
            from . import stash
            self._schema_fields = sorted(stash.Schema.load(self.client,
                                                           self.repo).field_names)
        return self._schema_fields


def dispatch(context, op, args):
    handler = HANDLERS.get(op)
    if handler is None:
        return {"ok": False, "error": "unknown operation %r" % op,
                "operations": sorted(HANDLERS)}
    try:
        result = handler(context, args or {})
    except Exception as exc:  # an operation failing is the UI's message, not a crash
        from .executor import describe_error, describe_origin
        message = describe_error(exc)
        origin = describe_origin(exc)
        # The location goes in the log rather than in the message the page shows: it is
        # for whoever has to fix it, and one line of it beats a bare exception text that
        # could have come from anywhere.
        logs.error("operation %s failed: %s%s"
                   % (op, message, (" [%s]" % origin) if origin else ""))
        return {"ok": False, "error": message}
    if isinstance(result, dict) and "ok" not in result:
        result["ok"] = True
    return result


# ------------------------------------------------------------------ meta

def op_ping(context, args):
    version = context.client.version() or {}
    return {
        "plugin_version": __import__("fastdiscovery").__version__,
        "schema_version": context.repo.schema_version(),
        "expected_schema_version": migrations.SCHEMA_VERSION,
        "stash_version": version.get("version"),
        "database": context.repo.path,
        "operations": sorted(HANDLERS),
    }


def op_diagnostics(context, args):
    counts = context.repo.counts()
    boxes = context.client.stash_boxes()
    return {
        "database": context.repo.path,
        "database_bytes": counts.pop("bytes", 0),
        "counts": counts,
        "run_statuses": context.repo.status_counts(),
        "schema_version": context.repo.schema_version(),
        "stash_boxes": [{"name": box.get("name"), "endpoint": box.get("endpoint")}
                        for box in boxes],
        "scraped_scene_fields": context.schema_fields(),
        "settings": context.config.as_dict(),
        "settings_problems": context.config.problems,
    }


def op_settings_get(context, args):
    """The settings, their defaults and their descriptions, for the settings page.

    Served from here rather than read from the manifest by the UI, because the manifest
    cannot express a default at all and this module is where they live.
    """
    return {
        "values": context.config.as_dict(),
        "spec": [{"name": name, "type": settings.TYPES[name],
                  "default": settings.DEFAULTS[name],
                  "description": settings.DESCRIPTIONS[name],
                  "limits": settings.LIMITS.get(name)}
                 for name in sorted(settings.SPEC)],
        "problems": context.config.problems,
        # Informational only: the boxes belong to Stash, and FastDiscovery has no
        # second place to configure them (requirement 31).
        "stash_boxes": [{"name": box.get("name"), "endpoint": box.get("endpoint")}
                        for box in context.client.stash_boxes()],
    }


def op_settings_set(context, args):
    values = args.get("values")
    if not isinstance(values, dict):
        return {"ok": False, "error": "values must be an object"}
    unknown = [name for name in values if name not in settings.SPEC]
    if unknown:
        return {"ok": False, "error": "unknown setting(s): " + ", ".join(sorted(unknown))}

    # Stash stores plugin settings as strings, numbers and booleans; sending the parsed
    # value back keeps what the page shows and what the engine reads identical.
    parsed = settings.parse({**context.client.plugin_settings(settings.PLUGIN_ID),
                             **values})
    outgoing = {name: parsed[name] for name in values}
    context.client.save_plugin_settings(settings.PLUGIN_ID, outgoing)
    return {"saved": sorted(outgoing), "values": parsed.as_dict(),
            "problems": parsed.problems}


# ------------------------------------------------------------------ runs

def op_scene_status(context, args):
    """Everything the scene page's FastDiscovery tab needs, in one call."""
    scene_id = _scene_id(args)
    context.repo.sweep_stale_runs(context.config["staleRunHours"])
    run = context.repo.latest_run(scene_id)
    job = None
    if run and run["status"] == R.RUNNING and run.get("job_id"):
        job = context.client.find_job(run["job_id"])
    return {
        "scene_id": scene_id,
        "run": _run_brief(run) if run else None,
        "job": job,
        "history": context.repo.applications_for(scene_id, limit=5),
    }


def op_run_start(context, args):
    """Queue discovery for one or more scenes.

    The work goes to Stash's job queue rather than happening here: a run is minutes of
    scraping, and this call is a browser waiting on a plugin process.

    A scene that already has a run waiting for a decision is refused unless the caller
    says `replace`, so the confirmation in requirement 22 cannot be skipped by accident.
    """
    scene_ids = _scene_ids(args)
    if not scene_ids:
        return {"ok": False, "error": "no scene ids given"}
    replace = bool(args.get("replace"))

    context.repo.sweep_stale_runs(context.config["staleRunHours"])
    blocked = []
    for scene_id in scene_ids:
        existing = context.repo.reviewable_run(scene_id)
        if existing and not replace:
            blocked.append({"scene_id": scene_id, "run_id": existing["id"],
                            "status": existing["status"]})
    if blocked and not replace:
        return {"ok": False, "needs_confirmation": True, "blocked": blocked,
                "error": "%d scene(s) already have FastDiscovery results waiting for a "
                         "decision. Running again replaces them." % len(blocked)}

    job_id = context.client.run_plugin_task(
        settings.PLUGIN_ID, TASK_DISCOVER,
        {"task": TASK_DISCOVER, "scene_ids": ",".join(str(one) for one in scene_ids),
         "trigger": str(args.get("trigger") or "ui"), "replace": "1"})
    return {"queued": len(scene_ids), "scene_ids": scene_ids, "job_id": job_id}


def op_run_cancel(context, args):
    """Ask Stash to stop the job behind a run.

    `stopJob` kills the plugin process outright, which is why every source is committed
    as it finishes: whatever had already come back is still there to review, and the
    run row is swept to FAILED by the next operation that runs (requirement 46).
    """
    run = _run(context, args)
    if not run:
        return {"ok": False, "error": "no such run"}
    if not run.get("job_id"):
        return {"ok": False, "error": "this run has no job to stop"}
    stopped = context.client.stop_job(run["job_id"])
    if stopped:
        context.repo.finish_run(run["id"], R.CANCELLED, stop_reason="cancelled")
    return {"stopped": bool(stopped), "run_id": run["id"]}


def op_run_list(context, args):
    """The FastDiscovery page: one row per run, with the scene it belongs to."""
    context.repo.sweep_stale_runs(context.config["staleRunHours"])
    tab = str(args.get("tab") or "ready")
    statuses = TABS.get(tab) if tab != "all" else None
    total, runs = context.repo.list_runs(statuses, page=args.get("page") or 1,
                                         per_page=args.get("per_page") or 25)
    scenes = {}
    for scene in context.client.find_scenes_brief([run["scene_id"] for run in runs]):
        scenes[str(scene["id"])] = {
            "id": scene["id"], "title": scene.get("title"),
            "date": scene.get("date"),
            "studio": (scene.get("studio") or {}).get("name"),
            "screenshot": (scene.get("paths") or {}).get("screenshot"),
            "filename": ((scene.get("files") or [{}])[0] or {}).get("basename"),
        }
    return {
        "total": total, "tab": tab,
        "counts": context.repo.status_counts(),
        "runs": [dict(_run_brief(run), scene=scenes.get(str(run["scene_id"])))
                 for run in runs],
    }


def op_run_delete(context, args):
    """Dismiss a run that has nothing to review, without touching the scene."""
    run = _run(context, args)
    if not run:
        return {"ok": False, "error": "no such run"}
    context.repo.delete_run(run["id"])
    return {"deleted": run["id"]}


# ------------------------------------------------------------------ review

def op_review_get(context, args):
    """The review matrix for a run, built against the scene as it is right now."""
    run = _run(context, args)
    if not run:
        return {"ok": False, "error": "no FastDiscovery results for this scene"}
    if run["purged"]:
        return {"ok": False, "error": "this run has already been %s; its results were "
                                      "deleted" % run["status"].lower()}
    scene = context.client.find_scene(run["scene_id"])
    if not scene:
        return {"ok": False, "error": "scene %s no longer exists" % run["scene_id"]}

    review = merge_module.build(context.repo, run, scene, context.schema_fields(),
                                context.client, run.get("rejected_sources"))
    review["summary"] = merge_module.summarise(review)
    review["default_selection"] = merge_module.default_selection(review)
    # A selection the user saved earlier wins over the defaults, so a review survives a
    # page reload without losing the choices already made - minus anything a rejected
    # source was the only one offering.
    saved = run.get("selection")
    review["selection"] = (merge_module.sanitise_selection(review, saved) if saved
                           else review["default_selection"])
    return review


def op_reject_source(context, args):
    """Strike a source out of the review, or put it back.

    Stored on the run rather than held in the page: the matrix, the defaults and Apply
    all have to agree about which sources count, and the only way to guarantee that is
    for all three to read it from the same place.
    """
    run = _run(context, args)
    if not run or run["purged"]:
        return {"ok": False, "error": "no results to review"}
    try:
        source_id = int(args.get("source_id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "source_id is required"}

    known = {source["id"] for source in context.repo.sources_of(run["id"])}
    if source_id not in known:
        return {"ok": False, "error": "this run has no source %s" % source_id}

    rejected = set(run.get("rejected_sources") or [])
    if args.get("rejected", True):
        rejected.add(source_id)
    else:
        rejected.discard(source_id)
    context.repo.set_rejected_sources(run["id"], rejected)

    # The selection is re-checked against the matrix the change produced, so a tick left
    # pointing at a value only the rejected source offered does not survive as a
    # dangling id that Apply would refuse.
    refreshed = op_review_get(context, {"run_id": run["id"]})
    if refreshed.get("ok") is not False and refreshed.get("selection") is not None:
        context.repo.set_selection(run["id"], refreshed["selection"])
    return refreshed


def op_review_save(context, args):
    run = _run(context, args)
    if not run:
        return {"ok": False, "error": "no such run"}
    selection = args.get("selection")
    if not isinstance(selection, dict):
        return {"ok": False, "error": "selection must be an object"}
    context.repo.set_selection(run["id"], selection)
    return {"saved": True, "run_id": run["id"]}


def op_review_image(context, args):
    """One image candidate, as a data URI, fetched only when it is actually shown.

    Only for images that arrived as base64 - an image the scraper gave as a URL is
    rendered straight from that URL by the browser and never passes through here
    (requirement 42).
    """
    sha = str(args.get("sha256") or "")
    blob = context.repo.image(sha) if sha else None
    if not blob:
        return {"ok": False, "error": "no such image"}
    return {"sha256": blob["sha256"], "mime": blob["mime"], "bytes": blob["bytes"],
            "data_uri": "data:%s;base64,%s"
                        % (blob["mime"],
                           base64.b64encode(blob["data"]).decode("ascii"))}


# ------------------------------------------------------------------ decisions

def op_apply_preview(context, args):
    run = _run(context, args)
    if not run or run["purged"]:
        return {"ok": False, "error": "no results to apply"}
    scene = context.client.find_scene(run["scene_id"])
    if not scene:
        return {"ok": False, "error": "scene %s no longer exists" % run["scene_id"]}
    return apply_module.preview(context.repo, context.client, run, scene,
                                args.get("selection"), context.schema_fields(),
                                rejected=run.get("rejected_sources"))


def op_apply_commit(context, args):
    run = _run(context, args)
    if not run or run["purged"]:
        return {"ok": False, "error": "no results to apply"}
    scene = context.client.find_scene(run["scene_id"])
    if not scene:
        return {"ok": False, "error": "scene %s no longer exists" % run["scene_id"]}
    try:
        return apply_module.commit(context.repo, context.client, run, scene,
                                   args.get("selection"), context.schema_fields(),
                                   expected_updated_at=args.get("expected_updated_at"),
                                   rejected=run.get("rejected_sources"))
    except apply_module.ApplyError as exc:
        # The run stays reviewable and the payload stays on disk, so Apply can simply
        # be pressed again once whatever went wrong is dealt with (requirement 20).
        return {"ok": False, "error": str(exc), "run_id": run["id"],
                "status": (context.repo.run(run["id"]) or {}).get("status")}


def op_reject(context, args):
    run = _run(context, args)
    if not run:
        return {"ok": False, "error": "no such run"}
    if run["purged"]:
        return {"ok": False, "error": "this run has already been decided"}
    return apply_module.reject(context.repo, run)


# ------------------------------------------------------- the scraper entry point

# What the scraper shim should tell the user. The shim holds no policy of its own.
READY = "ready"      # results are waiting; the review is in the FastDiscovery tab
RUNNING_ = "running"  # a run for this scene is already going
QUEUED = "queued"    # nothing stored, so a run has been started


def op_scraper_entry(context, args):
    """Everything the scraper shim needs, in one call.

    FastDiscovery appears in *Scrape with...* as a convenience, and the shim answers
    every invocation with `null`. That is deliberate and not a limitation worked
    around: a scene scraper's return value is what Stash offers to save, and Identify
    saves it without review, which is the exact opposite of what FastDiscovery is for
    (requirement 28, 57). So this starts a run or reports one, and never hands back a
    merged scene for something else to write.
    """
    scene_id = _scene_id(args)
    context.repo.sweep_stale_runs(context.config["staleRunHours"])

    active = context.repo.active_run(scene_id)
    if active:
        return {"action": RUNNING_, "run_id": active["id"],
                "message": "a FastDiscovery run for this scene is already going"}

    waiting = context.repo.reviewable_run(scene_id)
    if waiting:
        return {"action": READY, "run_id": waiting["id"],
                "sources": waiting["source_count"], "results": waiting["result_count"],
                "message": "FastDiscovery already has %d result(s) from %d source(s) "
                           "for this scene - open the scene's FastDiscovery tab to "
                           "review them" % (waiting["result_count"],
                                            waiting["source_count"])}

    if not args.get("queue", True):
        return {"action": QUEUED, "message": "nothing has been discovered for this "
                                             "scene yet"}
    job_id = context.client.run_plugin_task(
        settings.PLUGIN_ID, TASK_DISCOVER,
        {"task": TASK_DISCOVER, "scene_ids": str(scene_id), "trigger": "scraper",
         "replace": "1"})
    return {"action": QUEUED, "job_id": job_id,
            "message": "FastDiscovery queued for this scene. Watch the job queue, then "
                       "open the scene's FastDiscovery tab to review the results."}


# ------------------------------------------------------------------ maintenance

def op_maintenance(context, args):
    swept = context.repo.sweep_stale_runs(context.config["staleRunHours"])
    orphans = context.repo.purge_orphan_images()
    if args.get("vacuum"):
        context.repo.vacuum()
    return {"stale_runs_failed": swept, "orphan_images_removed": orphans,
            "vacuumed": bool(args.get("vacuum"))}


# ------------------------------------------------------------------ helpers

def _scene_id(args):
    value = str(args.get("scene_id") or "").strip()
    if not value.isdigit():
        raise ValueError("scene_id is required")
    return int(value)


def _scene_ids(args):
    raw = args.get("scene_ids") or args.get("scene_id") or ""
    parts = ([str(one) for one in raw] if isinstance(raw, (list, tuple))
             else str(raw).replace(";", ",").split(","))
    out = []
    for part in parts:
        part = part.strip()
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return out


def _run(context, args):
    if args.get("run_id"):
        return context.repo.run(int(args["run_id"]))
    scene_id = _scene_id(args)
    return (context.repo.reviewable_run(scene_id)
            or context.repo.latest_run(scene_id))


def _run_brief(run):
    return {key: run.get(key) for key in
            ("id", "scene_id", "status", "trigger", "job_id", "started_at",
             "finished_at", "decided_at", "source_count", "ok_source_count",
             "error_count", "url_count", "result_count", "max_depth_reached",
             "stop_reason", "error", "reviewable", "purged", "progress")}


HANDLERS = {
    "ping": op_ping,
    "diagnostics.info": op_diagnostics,
    "settings.get": op_settings_get,
    "settings.set": op_settings_set,
    "scene.status": op_scene_status,
    "run.start": op_run_start,
    "run.cancel": op_run_cancel,
    "run.list": op_run_list,
    "run.delete": op_run_delete,
    "review.get": op_review_get,
    "review.save": op_review_save,
    "review.reject_source": op_reject_source,
    "review.image": op_review_image,
    "apply.preview": op_apply_preview,
    "apply.commit": op_apply_commit,
    "run.reject": op_reject,
    "scraper.entry": op_scraper_entry,
    "maintenance.run": op_maintenance,
}
