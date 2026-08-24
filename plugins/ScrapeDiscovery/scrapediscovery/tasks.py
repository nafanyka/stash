"""Plugin tasks: the long-running work, as jobs in Stash's own queue.

Anything that scrapes belongs here rather than in ops.py. A task gets a job with a
progress bar and a stop button; an operation blocks the request that asked for it.

Progress: Stash accepts one float per job and nothing else - `Job.subTasks` is not
writable by a plugin - so the fraction goes to the job and the detail a user actually
wants ("scene 18/120, 7 of 42 scrapers, 3 matches, current: IAFD") goes to the log and
into the scan row, which the ScrapeDiscovery page polls.

Cancellation is a process kill, so nothing here tries to clean up on the way out. What
keeps the database consistent is that every attempt is committed as it finishes, and
that any operation opening the database sweeps scans whose process stopped reporting.
"""

from __future__ import annotations

import time

from . import cache, engine as engine_module, executor, logs, settings
from .db import repo as R

# Tasks, named exactly as the manifest declares them.
DISCOVER_SCENES = "Discover scenes"
DISCOVER_TAGGED = "Discover tagged scenes"
DISCOVER_UNRESOLVED = "Discover unresolved scenes"
RETRY_FAILED = "Retry failed discoveries"
SCAN_NEW_SCRAPERS = "Scan with newly installed scrapers"
VACUUM = "Vacuum database"
CLEAR_CACHE = "Clear expired cache"
PRUNE_HISTORY = "Delete old history"
SHOW_SETTINGS = "Show effective settings"

# How many scenes a batch task will take in one job unless told otherwise. A ceiling
# rather than a preference: an unbounded batch over a large library would hold a job
# for hours, and the task can simply be run again.
DEFAULT_BATCH_LIMIT = 200


def run(context, task_name, args):
    """Dispatch a task by the name Stash passes in."""
    handler = TASKS.get(task_name)
    if handler is None:
        return {"ok": False, "error": "unknown task %r" % task_name,
                "tasks": sorted(TASKS)}
    return handler(context, args or {})


# --------------------------------------------------------------- discovery

def _scene_ids_from_args(args):
    raw = args.get("scene_ids") or args.get("scene_id") or ""
    if isinstance(raw, (list, tuple)):
        parts = [str(one) for one in raw]
    else:
        parts = str(raw).replace(";", ",").split(",")
    out = []
    for part in parts:
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _mode(context, args):
    mode = str(args.get("mode") or context.config["defaultMode"]).lower()
    return mode if mode in (settings.NORMAL, settings.DEEP) else settings.NORMAL


def _scan_many(context, scene_ids, mode, trigger, ignore_cache=False,
               expand_urls=False):
    """Scan a list of scenes, reporting progress across the whole list.

    One scene's failure never ends the batch: a scene can have been deleted since the
    list was built, and a structural error on one is still only one scene.
    """
    engine = engine_module.make_engine(context.client, context.repo, context.config)
    total = len(scene_ids)
    done = 0
    summaries = []
    matched = 0
    failures = []
    started = time.monotonic()

    logs.info("%s: %d scene(s), %s scan" % (trigger, total, mode))
    logs.progress(0.0)

    for scene_id in scene_ids:
        def progress_hook(state, _done=done, _total=total):
            # Two levels of progress: where we are in the batch, plus how far into
            # this scene. Stash only takes the one number, so they are combined.
            logs.progress((_done + state.fraction()) / float(_total or 1))
            logs.info("scene %s: %d/%d attempts, %d match, %d error, %d url(s)"
                      % (state.scene_id, state.done + state.cached, state.planned,
                         state.matches, state.errors + state.timeouts, state.urls))

        try:
            summary = engine.scan(scene_id, mode=mode, trigger=trigger,
                                  ignore_cache=ignore_cache, expand_urls=expand_urls,
                                  progress_hook=progress_hook)
            summaries.append(summary)
            if summary["matches"]:
                matched += 1
        except Exception as exc:
            message = executor.describe_error(exc)
            failures.append({"scene_id": scene_id, "error": message})
            logs.error("scene %s: %s" % (scene_id, message))
        done += 1
        logs.progress(done / float(total or 1))

    elapsed = round(time.monotonic() - started, 1)
    logs.info("%s finished: %d scene(s) in %ss, %d with matches, %d failed"
              % (trigger, total, elapsed, matched, len(failures)))
    return {
        "scenes": total, "scanned": len(summaries), "with_matches": matched,
        "failed": failures, "seconds": elapsed,
        "attempts": sum(one["done"] + one["cached"] for one in summaries),
        "matches": sum(one["matches"] for one in summaries),
        "errors": sum(one["errors"] for one in summaries),
        "urls": sum(one["urls"] for one in summaries),
    }


def task_discover_scenes(context, args):
    """Scan the scenes named in the arguments. What the Discover button calls."""
    scene_ids = _scene_ids_from_args(args)
    if not scene_ids:
        # Reachable from Settings -> Tasks, where nobody supplied a scene. Say so
        # rather than inventing a target.
        return {"ok": False,
                "error": "no scene_ids given. This task is started from a scene's "
                         "Discovery tab or from the ScrapeDiscovery page; the batch "
                         "tasks are the ones to run from here."}
    return _scan_many(context, scene_ids, _mode(context, args),
                      str(args.get("trigger") or "manual"),
                      ignore_cache=bool(args.get("ignore_cache")))


def _resolve_tag(context, name, create=False):
    if not name:
        return None
    tag = context.client.find_tag_by_name(name)
    if tag:
        return tag
    if not create:
        return None
    logs.info("creating tag %r" % name)
    return context.client.create_tag(name)


def task_discover_tagged(context, args):
    """Scan every scene carrying the configured input tag.

    The tag is not created here: if it does not exist, no scene can be carrying it,
    and creating one silently would hide a typo in the setting.
    """
    name = str(args.get("tag") or context.config["inputTag"] or "").strip()
    if not name:
        return {"ok": False, "error": "no input tag configured"}
    tag = _resolve_tag(context, name)
    if not tag:
        return {"ok": False,
                "error": "no tag named %r exists, so no scene can be tagged with it"
                         % name}

    limit = int(args.get("limit") or DEFAULT_BATCH_LIMIT)
    count, scene_ids = context.client.scene_ids_with_tag([tag["id"]], per_page=limit)
    if not scene_ids:
        logs.info("no scenes carry the tag %r" % name)
        return {"scenes": 0, "tag": name}
    if count > len(scene_ids):
        logs.warning("%d scenes carry %r; this run takes the first %d. Run the task "
                     "again for the rest." % (count, name, len(scene_ids)))

    result = _scan_many(context, [int(one) for one in scene_ids],
                        _mode(context, args), "tag_task")
    result.update({"tag": name, "tagged_total": count})
    return result


def task_discover_unresolved(context, args):
    """Scan scenes ScrapeDiscovery has never got an answer for.

    Never scanned, or scanned and found nothing - the scenes where trying again is the
    only thing that can help. Scenes with candidates waiting for review are left alone.
    """
    limit = int(args.get("limit") or DEFAULT_BATCH_LIMIT)
    scene_ids = context.repo.scenes_by_status(
        [R.UNSCANNED, R.NO_RESULTS, R.RESULTS], limit=limit)
    if not scene_ids:
        return {"ok": True, "scenes": 0,
                "note": "nothing unresolved is on record. Tag the scenes you want "
                        "looked at and run the tagged task, which is what puts them "
                        "on ScrapeDiscovery's list in the first place."}
    return _scan_many(context, scene_ids, _mode(context, args), "retry")


def task_retry_failed(context, args):
    """Re-run scenes whose last scan errored, ignoring the error cache.

    `ignore_cache` matters here: a failed attempt is cached precisely so a normal scan
    does not keep hammering a broken scraper, and this task is the deliberate override.
    """
    limit = int(args.get("limit") or DEFAULT_BATCH_LIMIT)
    scene_ids = context.repo.scenes_by_status([R.FAILED], limit=limit)
    if not scene_ids:
        return {"ok": True, "scenes": 0, "note": "no failed scenes on record"}
    return _scan_many(context, scene_ids, _mode(context, args), "retry",
                      ignore_cache=True)


def task_scan_new_scrapers(context, args):
    """Scan unresolved scenes with scrapers they have not been tried with.

    "Not tried" means the scraper's fingerprint - name, capabilities, URL patterns -
    has never been recorded against that scene, so a newly installed scraper counts
    and so does one whose configuration changed. Scenes are only revisited if they are
    still unresolved: a scene with candidates does not need a wider net.
    """
    engine = engine_module.make_engine(context.client, context.repo, context.config)
    registry = engine.ensure_registry()
    known = {entry["id"]: entry["fingerprint"] for entry in registry.enabled()}

    limit = int(args.get("limit") or DEFAULT_BATCH_LIMIT)
    candidates = context.repo.scenes_by_status(
        [R.UNSCANNED, R.NO_RESULTS, R.RESULTS, R.FAILED], limit=limit * 4)

    selected = []
    for scene_id in candidates:
        tried = context.repo.scrapers_tried(scene_id)
        fresh = [scraper_id for scraper_id, fingerprint in known.items()
                 if tried.get(scraper_id) != fingerprint]
        if fresh:
            selected.append((scene_id, len(fresh)))
        if len(selected) >= limit:
            break

    if not selected:
        return {"ok": True, "scenes": 0,
                "note": "every unresolved scene has already been tried with every "
                        "installed scraper in its current form"}
    logs.info("%d scene(s) have untried scrapers; most untried: %d"
              % (len(selected), max(count for _id, count in selected)))
    result = _scan_many(context, [scene_id for scene_id, _count in selected],
                        _mode(context, args), "new_scraper")
    result["untried_scrapers"] = {str(scene_id): count for scene_id, count in selected}
    return result


# ------------------------------------------------------------- maintenance

def task_vacuum(context, args):
    before = context.repo.counts()["bytes"]
    orphans = context.repo.prune_orphan_blobs()
    context.repo.vacuum()
    after = context.repo.counts()["bytes"]
    logs.info("vacuum: %d -> %d bytes, %d orphaned image(s) removed"
              % (before, after, orphans))
    return {"before_bytes": before, "after_bytes": after, "orphan_blobs": orphans}


def task_clear_cache(context, args):
    """Drop expired attempts that hold no results.

    Only the "do not ask again yet" rows go. An attempt that returned something owns
    results, and those are the raw material every later stage rebuilds from, so
    expiring one would throw discovery information away; `Delete old history` is the
    task that removes those, deliberately and with their whole scan for context.
    """
    removed = context.repo.clear_expired_cache(cache.Policy(context.config).as_callable())
    logs.info("cleared %d expired cache attempt(s)" % removed)
    return {"removed": removed}


def task_prune_history(context, args):
    days = args.get("days")
    days = float(days) if days not in (None, "") else context.config["historyRetentionDays"]
    if not days:
        return {"ok": False,
                "error": "historyRetentionDays is 0, which means keep everything. "
                         "Set it, or pass days to this task, to prune."}
    removed = context.repo.prune_history(days)
    orphans = context.repo.prune_orphan_blobs()
    logs.info("pruned %d scan(s) older than %s days, %d orphaned image(s)"
              % (removed["scans"], days, orphans))
    return {"removed_scans": removed["scans"], "orphan_blobs": orphans, "days": days}


def task_show_settings(context, args):
    """Report the effective configuration, and complain about anything invalid."""
    logs.info("effective configuration:")
    for line in settings.describe(context.config):
        logs.info("  " + line)
    for problem in context.config.problems:
        logs.warning("  " + problem)

    engine = engine_module.make_engine(context.client, context.repo, context.config)
    registry = engine.ensure_registry()
    enabled = registry.enabled()
    logs.info("scrapers: %d installed, %d enabled (%d fragment, %d name, %d url)"
              % (len(registry.scrapers), len(enabled),
                 len(registry.with_kind("FRAGMENT")), len(registry.with_kind("NAME")),
                 len(registry.with_kind("URL"))))
    logs.info("stash-boxes: %s"
              % (", ".join(box.get("name") or box["endpoint"]
                           for box in registry.stash_boxes) or "(none configured)"))
    counts = context.repo.counts()
    logs.info("database: %s (%.1f MB, schema v%s)"
              % (context.repo.path, counts["bytes"] / 1048576.0,
                 context.repo.schema_version()))
    return {"problems": context.config.problems,
            "scrapers": len(registry.scrapers), "enabled": len(enabled),
            "database": context.repo.path}


TASKS = {
    DISCOVER_SCENES: task_discover_scenes,
    DISCOVER_TAGGED: task_discover_tagged,
    DISCOVER_UNRESOLVED: task_discover_unresolved,
    RETRY_FAILED: task_retry_failed,
    SCAN_NEW_SCRAPERS: task_scan_new_scrapers,
    VACUUM: task_vacuum,
    CLEAR_CACHE: task_clear_cache,
    PRUNE_HISTORY: task_prune_history,
    SHOW_SETTINGS: task_show_settings,
}
