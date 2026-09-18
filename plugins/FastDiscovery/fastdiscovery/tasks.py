"""The job-queue entry points.

A task is the only way for a plugin to get work that survives the request that asked
for it, with a progress bar and a stop button of its own. Discovery is minutes of
scraping per scene, so it lives here and nowhere else; the UI's `run.start` operation
does nothing but queue this.

Stash matches a task by the exact string in the manifest, so the names below and the
manifest are checked against each other by a test.
"""

from __future__ import annotations

import time

from . import discovery, executor, logs, performer_discovery, settings
from .db import repo as R

DISCOVER = "Discover scenes"
DISCOVER_PERFORMERS = "Discover performers"
MAINTENANCE = "Maintenance"
SHOW_SETTINGS = "Show effective settings"


def run(context, task_name, args):
    handler = TASKS.get(task_name)
    if handler is None:
        return {"ok": False, "error": "unknown task %r" % task_name,
                "tasks": sorted(TASKS)}
    return handler(context, args or {})


def task_discover(context, args):
    """Discover for the scenes named in the arguments.

    Started from a scene's FastDiscovery tab, from the scene list's plugin menu, or
    from the FastDiscovery page. Running it from Settings -> Tasks without scene ids
    does nothing, and says so: there is no sensible "every scene" default here - a
    library-wide run would be thousands of scenes of scraping nobody asked for.
    """
    from .ops import _scene_ids
    scene_ids = _scene_ids(args)
    if not scene_ids:
        return {"ok": False, "error": "no scene ids given. Start FastDiscovery from a "
                                      "scene, or select scenes in the scene list."}

    context.repo.sweep_stale_runs(context.config["staleRunHours"])
    runner = discovery.make_runner(context.client, context.repo, context.config)
    trigger = str(args.get("trigger") or "task")
    replace = str(args.get("replace") or "1") not in ("0", "false", "no")

    total = len(scene_ids)
    started = time.monotonic()
    summaries, failures = [], []
    logs.info("discovering %d scene(s)" % total)
    logs.progress(0.0)

    for index, scene_id in enumerate(scene_ids):
        def progress_hook(state, _index=index):
            # Two levels of progress - where we are in the batch, and how far into this
            # scene. Stash takes one number, so they are combined; the detail goes to
            # the log and to the run's own heartbeat, which the page reads.
            logs.progress((_index + 0.5) / float(total))
            logs.info("scene %s: %d source(s), %d with results, %d error(s), %d url(s)"
                      % (state.scene_id, state.sources, state.ok, state.errors,
                         state.url_total))

        try:
            summaries.append(runner.run(scene_id, trigger=trigger,
                                        job_id=args.get("job_id"), replace=replace,
                                        progress_hook=progress_hook))
        except discovery.SceneMissing as exc:
            failures.append({"scene_id": scene_id, "error": str(exc)})
            logs.warning(str(exc))
        except Exception as exc:
            # One scene's structural failure is one scene, never the batch's
            # (requirement 45): a library run must not stop on scene 3 of 50.
            message = executor.describe_error(exc)
            failures.append({"scene_id": scene_id, "error": message})
            logs.error("scene %s: %s" % (scene_id, message))
        logs.progress((index + 1) / float(total))

    ready = len([one for one in summaries
                 if one["status"] in (R.READY_FOR_REVIEW, R.READY_WITH_ERRORS)])
    elapsed = round(time.monotonic() - started, 1)
    logs.info("finished: %d scene(s) in %ss, %d ready for review, %d failed"
              % (total, elapsed, ready, len(failures)))
    return {"scenes": total, "ready": ready, "failed": failures, "seconds": elapsed,
            "results": sum(one["results"] for one in summaries),
            "sources": sum(one["sources"] for one in summaries),
            "errors": sum(one["errors"] for one in summaries)}


def task_discover_performers(context, args):
    """Discover for the performers named in the arguments - Fast, or Full on a run.

    Two shapes, told apart by which args are present: `performer_ids` + `mode` starts
    (or replaces) Fast runs for each; `run_id` (no `performer_ids`) tops up one
    existing run with Full. Mixing many performers into one Full call makes no sense -
    Full always names the one run it is topping up - so this task never batches Full
    the way Fast batches performer ids.
    """
    context.repo.sweep_stale_runs(context.config["staleRunHours"])
    runner = performer_discovery.make_runner(context.client, context.repo,
                                             context.config)

    if str(args.get("mode") or "").upper() == "FULL" or args.get("run_id"):
        run_id = int(args["run_id"])
        logs.info("performer run %s: going to Full discovery" % run_id)
        logs.progress(0.0)
        try:
            summary = runner.run_full(run_id,
                                      progress_hook=lambda state:
                                      logs.progress(0.5))
        except Exception as exc:
            message = executor.describe_error(exc)
            logs.error("performer run %s: Full failed - %s" % (run_id, message))
            logs.progress(1.0)
            return {"ok": False, "error": message}
        logs.progress(1.0)
        logs.info("performer run %s: Full finished - %s" % (run_id, summary["status"]))
        return {"run_id": run_id, "status": summary["status"],
                "results": summary["results"], "sources": summary["sources"],
                "errors": summary["errors"], "seconds": summary["seconds"]}

    raw = args.get("performer_ids") or args.get("performer_id") or ""
    parts = ([str(one) for one in raw] if isinstance(raw, (list, tuple))
             else str(raw).replace(";", ",").split(","))
    performer_ids = []
    for part in parts:
        part = part.strip()
        if part.isdigit() and int(part) not in performer_ids:
            performer_ids.append(int(part))
    if not performer_ids:
        return {"ok": False, "error": "no performer ids given. Use the Fast "
                                      "Discovery button on a performer's page."}

    trigger = str(args.get("trigger") or "task")
    replace = str(args.get("replace") or "1") not in ("0", "false", "no")
    total = len(performer_ids)
    started = time.monotonic()
    summaries, failures = [], []
    logs.info("Fast performer discovery: %d performer(s)" % total)
    logs.progress(0.0)

    for index, performer_id in enumerate(performer_ids):
        def progress_hook(state, _index=index):
            logs.progress((_index + 0.5) / float(total))
            logs.info("performer %s: %d source(s), %d with results, %d error(s), "
                      "%d url(s)" % (state.performer_id, state.sources, state.ok,
                                    state.errors, state.url_total))

        try:
            summaries.append(runner.run_fast(performer_id, trigger=trigger,
                                             job_id=args.get("job_id"),
                                             replace=replace,
                                             progress_hook=progress_hook))
        except performer_discovery.PerformerMissing as exc:
            failures.append({"performer_id": performer_id, "error": str(exc)})
            logs.warning(str(exc))
        except Exception as exc:
            message = executor.describe_error(exc)
            failures.append({"performer_id": performer_id, "error": message})
            logs.error("performer %s: %s" % (performer_id, message))
        logs.progress((index + 1) / float(total))

    ready = len([one for one in summaries
                if one["status"] in (R.READY_FOR_REVIEW, R.READY_WITH_ERRORS)])
    elapsed = round(time.monotonic() - started, 1)
    logs.info("finished: %d performer(s) in %ss, %d ready for review, %d failed"
              % (total, elapsed, ready, len(failures)))
    return {"performers": total, "ready": ready, "failed": failures,
            "seconds": elapsed,
            "results": sum(one["results"] for one in summaries),
            "sources": sum(one["sources"] for one in summaries),
            "errors": sum(one["errors"] for one in summaries)}


def task_maintenance(context, args):
    """Sweep runs whose process died, purge dead-end payloads, drop unreferenced
    images, compact the file."""
    swept = context.repo.sweep_stale_runs(context.config["staleRunHours"])
    dead_ends = context.repo.purge_finished_dead_end_runs()
    orphans = context.repo.purge_orphan_images()
    before = context.repo.counts().get("bytes", 0)
    context.repo.vacuum()
    after = context.repo.counts().get("bytes", 0)
    logs.info("maintenance: %d stale run(s) marked failed, %d dead-end run(s) "
              "purged, %d orphan image(s) removed, database %d -> %d bytes"
              % (swept, dead_ends, orphans, before, after))
    return {"stale_runs_failed": swept, "dead_end_runs_purged": dead_ends,
            "orphan_images_removed": orphans, "bytes_before": before,
            "bytes_after": after}


def task_show_settings(context, args):
    """Log the configuration exactly as a run will read it, plus what it can see."""
    boxes = context.client.stash_boxes()
    version = context.client.version() or {}
    logs.info("FastDiscovery %s against Stash %s"
              % (__import__("fastdiscovery").__version__, version.get("version") or "?"))
    logs.info("database: " + context.repo.path)
    logs.info("stash-boxes detected: %d" % len(boxes))
    for box in boxes:
        # Name and endpoint only. The same query returns each box's API key, which is
        # never read and never logged (requirement 43).
        logs.info("  %s  %s" % (box.get("name") or "?", box.get("endpoint")))
    for line in settings.describe(context.config):
        logs.info("  " + line)
    for problem in context.config.problems:
        logs.warning("  " + problem)
    try:
        scrapers = context.client.list_scene_scrapers()
        logs.info("installed scene scrapers: %d" % len(scrapers))
    except Exception as exc:
        logs.warning("could not list scrapers: " + executor.describe_error(exc))
        scrapers = []
    return {"settings": context.config.as_dict(), "problems": context.config.problems,
            "stash_boxes": len(boxes), "scene_scrapers": len(scrapers),
            "database": context.repo.path,
            "runs": context.repo.status_counts()}


TASKS = {
    DISCOVER: task_discover,
    DISCOVER_PERFORMERS: task_discover_performers,
    MAINTENANCE: task_maintenance,
    SHOW_SETTINGS: task_show_settings,
}
