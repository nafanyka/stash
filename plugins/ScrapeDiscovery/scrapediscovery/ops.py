"""The plugin's request/response API, reached with `runPluginOperation`.

A Stash plugin cannot serve HTTP, so this is the whole channel between the UI and the
database: the browser calls `runPluginOperation(plugin_id, args)`, Stash runs the
plugin process, waits, and hands back whatever the process printed as JSON. One process
per call, so every operation here has to be cheap - reads are paged and never open a
raw result unless asked to.

Failure convention: an operation that could not do what was asked returns
`{"ok": false, "error": "..."}` rather than raising, so the page can show the message
in place. Only something that makes the plugin itself unusable - a database that cannot
be opened - comes back as a plugin-level error, which surfaces as a GraphQL error.
"""

from __future__ import annotations

import base64

from . import consensus, logs, normalize, settings
from .db import migrations, repo as R

# Statuses grouped the way the inbox tabs present them.
TABS = {
    "unresolved": [R.UNSCANNED, R.SCANNING],
    "candidates": [R.CANDIDATES],
    "results": [R.RESULTS],
    "resolved": [R.APPLIED, R.DISMISSED],
    "failed": [R.FAILED, R.NO_RESULTS],
}


class Context:
    """What every operation needs, built once per plugin invocation."""

    def __init__(self, client, repo, config):
        self.client = client
        self.repo = repo
        self.config = config


def dispatch(context, op, args):
    handler = HANDLERS.get(op)
    if handler is None:
        return {"ok": False, "error": "unknown operation %r" % op,
                "operations": sorted(HANDLERS)}
    try:
        result = handler(context, args or {})
    except Exception as exc:  # an operation's failure is the UI's message, not a crash
        from .executor import describe_error
        logs.error("operation %s failed: %s" % (op, describe_error(exc)))
        return {"ok": False, "error": describe_error(exc)}
    if isinstance(result, dict) and "ok" not in result:
        result["ok"] = True
    return result


# ------------------------------------------------------------------ meta

def op_ping(context, args):
    version = context.client.version() or {}
    return {
        "plugin_version": __import__("scrapediscovery").__version__,
        "schema_version": context.repo.schema_version(),
        "expected_schema_version": migrations.SCHEMA_VERSION,
        "stash_version": version.get("version"),
        "database": context.repo.path,
        "operations": sorted(HANDLERS),
    }


def op_diagnostics(context, args):
    counts = context.repo.counts()
    return {
        "database": context.repo.path,
        "database_bytes": counts.pop("bytes", 0),
        "counts": counts,
        "schema_version": context.repo.schema_version(),
        "versions": {"norm": migrations.NORM_VERSION,
                     "correlation": migrations.CORRELATION_VERSION,
                     "scoring": migrations.SCORING_VERSION},
        "status_counts": context.repo.status_counts(),
        "scrapers_synced_at": context.repo.meta_get("scrapers_synced_at"),
        "scraper_stats": context.repo.scraper_stats(limit=int(args.get("limit") or 200)),
        "config_problems": context.config.problems,
        "config": settings.describe(context.config),
    }


# -------------------------------------------------------------- settings

def op_settings_get(context, args):
    spec = []
    for key, (kind, default, description) in settings.SPEC.items():
        spec.append({"key": key, "type": kind, "default": default,
                     "description": description, "value": context.config.get(key)})
    return {"settings": spec, "problems": context.config.problems,
            "priorities": list(settings.PRIORITIES)}


def op_settings_set(context, args):
    """Write settings back into Stash's own plugin config.

    Stash is the single store (see settings.py), so the ScrapeDiscovery page and
    Stash's plugin settings panel can never disagree. Values are serialised the way
    Stash's UI would have written them: JSON settings as JSON text, booleans as real
    booleans, numbers as numbers.
    """
    import json

    values = args.get("values") or {}
    if not isinstance(values, dict):
        return {"ok": False, "error": "values must be an object"}

    payload, rejected = {}, []
    for key, value in values.items():
        if key not in settings.SPEC:
            rejected.append(key)
            continue
        kind = settings.SPEC[key][0]
        if kind == settings.JSON_:
            payload[key] = value if isinstance(value, str) else json.dumps(value)
        elif kind == settings.BOOLEAN:
            payload[key] = bool(value)
        elif kind == settings.NUMBER:
            try:
                payload[key] = float(value) if "." in str(value) else int(value)
            except (TypeError, ValueError):
                rejected.append(key)
        else:
            payload[key] = "" if value is None else str(value)

    if payload:
        context.client.save_plugin_settings(settings.PLUGIN_ID, payload)
    fresh = settings.parse(context.client.plugin_settings(settings.PLUGIN_ID))
    return {"saved": sorted(payload), "rejected": rejected,
            "problems": fresh.problems}


# ----------------------------------------------------------------- inbox

def op_inbox(context, args):
    context.repo.sweep_stale_scans()
    tab = str(args.get("tab") or "").lower()
    status = args.get("status")
    if tab and tab in TABS:
        status = ",".join(TABS[tab])
    elif tab == "all":
        status = None

    page = context.repo.inbox(
        status=status,
        query=str(args.get("q") or ""),
        min_confidence=args.get("minConfidence"),
        studio=str(args.get("studio") or ""),
        scraper=str(args.get("scraper") or ""),
        has_errors=args.get("hasErrors"),
        sort=str(args.get("sort") or "last_scanned_at"),
        direction=str(args.get("direction") or "desc"),
        page=int(args.get("page") or 1),
        per_page=int(args.get("perPage") or 50),
    )
    page["tabs"] = {name: 0 for name in TABS}
    counts = context.repo.status_counts()
    for name, statuses in TABS.items():
        page["tabs"][name] = sum(counts.get(one, 0) for one in statuses)
    page["status_counts"] = counts
    page["running"] = [
        {"id": scan["id"], "scene_id": scan["scene_id"], "mode": scan["mode"],
         "started_at": scan["started_at"], "progress": scan["progress"]}
        for scan in context.repo.running_scans()
    ]
    return page


# ------------------------------------------------------------------ scene

def op_scene_summary(context, args):
    """The small payload the scene page's tab badge needs."""
    scene_id = int(args.get("scene_id") or 0)
    state = context.repo.scene_state(scene_id) or {}
    candidates = context.repo.candidates_of_scene(scene_id)
    return {
        "scene_id": scene_id,
        "status": state.get("status") or R.UNSCANNED,
        "candidate_count": state.get("candidate_count") or 0,
        "best_confidence": state.get("best_confidence"),
        "result_count": context.repo.result_count_for_scene(scene_id),
        "attempt_count": state.get("attempt_count") or 0,
        "error_count": state.get("error_count") or 0,
        "url_count": state.get("url_count") or 0,
        "last_scanned_at": state.get("last_scanned_at"),
        "scanning": (state.get("status") == R.SCANNING),
        "top_candidates": [
            {"id": one["id"], "confidence": one["confidence"], "level": one["level"],
             "title": (one.get("merged") or {}).get("title")}
            for one in candidates[:3]
        ],
    }


def op_scene_detail(context, args):
    """Everything the scene discovery page shows, without any raw payload.

    Raw results are the largest thing in the database and the page only needs one at a
    time, on request, so they are fetched by `result.raw` instead.
    """
    context.repo.sweep_stale_scans()
    scene_id = int(args.get("scene_id") or 0)
    limit = min(int(args.get("attemptLimit") or 400), 2000)

    scans = context.repo.scans_for(scene_id, limit=int(args.get("scanLimit") or 20))
    attempts = context.repo.attempts_of_scene(scene_id, limit=limit)
    results = context.repo.results_of_scene(scene_id)

    urls = []
    for scan in scans[:3]:
        urls.extend(context.repo.urls_of_scan(scan["id"]))

    scene = None
    if args.get("includeScene", True):
        try:
            # A scene can be deleted after being scanned, and its discovery record is
            # still worth reading. `scene: null` says so; an empty snapshot would look
            # like a scene with every field blank.
            live = context.client.find_scene(scene_id)
            scene = normalize.scene_snapshot(live) if live else None
        except Exception as exc:
            from .executor import describe_error
            logs.warning("scene %s could not be read: %s" % (scene_id, describe_error(exc)))

    return {
        "scene_id": scene_id,
        "state": context.repo.scene_state(scene_id),
        "scene": scene,
        "scans": scans,
        "attempts": attempts,
        "results": [_result_summary(one) for one in results],
        "urls": urls,
        "candidates": context.repo.candidates_of_scene(scene_id),
        "applications": context.repo.applications_of_scene(scene_id),
        "graph": _graph(attempts, urls),
    }


def _result_summary(row):
    """A result as the page lists it: normalised fields, no raw payload."""
    normalized = row.get("normalized") or {}
    return {
        "id": row["id"],
        "attempt_id": row["attempt_id"],
        "ordinal": row["ordinal"],
        "scraper_id": row.get("scraper_id"),
        "scraper_name": row.get("scraper_name"),
        "method": row.get("method"),
        "target": row.get("target"),
        "depth": row.get("depth"),
        "attribution": row.get("attribution"),
        "started_at": row.get("started_at"),
        "has_image": bool(row.get("image_sha256")),
        "image_sha256": row.get("image_sha256"),
        "norm_version": row.get("norm_version"),
        "stale_normalization": (row.get("norm_version") or 0) != migrations.NORM_VERSION,
        "fingerprint": row.get("raw_fingerprint"),
        "normalized": normalized,
    }


def _graph(attempts, urls):
    """Parent/child edges, so the page can show how a result was reached."""
    nodes, edges = [], []
    for attempt in attempts:
        nodes.append({
            "id": "attempt-%s" % attempt["id"],
            "kind": "attempt",
            "label": attempt.get("scraper_name") or attempt["method"],
            "method": attempt["method"],
            "status": attempt["status"],
            "depth": attempt["depth"],
            "target": attempt.get("target"),
        })
        if attempt.get("parent_id"):
            edges.append({"from": "attempt-%s" % attempt["parent_id"],
                          "to": "attempt-%s" % attempt["id"], "via": "url"})
        else:
            edges.append({"from": "scene", "to": "attempt-%s" % attempt["id"],
                          "via": attempt["method"]})
    for url in urls:
        nodes.append({"id": "url-%s" % url["id"], "kind": "url", "label": url["url"],
                      "state": url["state"], "depth": url["depth"],
                      "handlers": url.get("handlers") or []})
    return {"nodes": nodes, "edges": edges}


def op_result_raw(context, args):
    """One stored raw payload, on request. Images stay as blob references."""
    result_id = int(args.get("result_id") or 0)
    row = context.repo.result_with_context(result_id)
    if not row:
        return {"ok": False, "error": "no result %s" % result_id}
    return {"result": row}


def op_image(context, args):
    """A stored cover as a data URI.

    Scrapers hand images over inline; they are kept out of the raw JSON and served
    from here so a list of thirty results does not carry thirty JPEGs.
    """
    sha = str(args.get("sha256") or "")
    blob = context.repo.blob(sha)
    if not blob:
        return {"ok": False, "error": "no image %s" % sha[:16]}
    return {"sha256": sha, "mime": blob["mime"], "bytes": blob["bytes"],
            "data_uri": "data:%s;base64,%s"
                        % (blob["mime"] or "image/jpeg",
                           base64.b64encode(blob["data"]).decode("ascii"))}


# ------------------------------------------------------------------ scans

def op_scan_start(context, args):
    """Queue a scan as a Stash job.

    Deliberately not run here: this operation blocks the browser's request until the
    plugin process exits, and a scan is minutes of work. As a job it gets Stash's
    progress bar, its stop button, and its place in the queue.
    """
    scene_ids = args.get("scene_ids")
    if not scene_ids:
        single = args.get("scene_id")
        scene_ids = [single] if single else []
    scene_ids = [str(one) for one in scene_ids if str(one or "").strip()]
    if not scene_ids:
        return {"ok": False, "error": "scene_id or scene_ids is required"}

    mode = str(args.get("mode") or context.config["defaultMode"]).lower()
    if mode not in (settings.NORMAL, settings.DEEP):
        return {"ok": False, "error": "mode must be normal or deep"}

    task_args = {"scene_ids": ",".join(scene_ids), "mode": mode,
                 "trigger": str(args.get("trigger") or "manual")}
    if args.get("ignoreCache"):
        task_args["ignore_cache"] = True
    job_id = context.client.run_plugin_task(
        settings.PLUGIN_ID, TASK_DISCOVER_SCENES, task_args)
    # The scene is deliberately *not* marked as scanning here. A queued job has not
    # started - this server had 1290 jobs ahead of it - and writing the status
    # optimistically leaves a scene stuck claiming to scan if the job never runs, with
    # no scan row for the sweep to repair. The scan sets the status when it begins;
    # until then the job id is what the caller watches. Queuing twice is harmless
    # anyway: the second scan finds everything cached and finishes in milliseconds.
    return {"job_id": job_id, "scene_ids": scene_ids, "mode": mode}


def op_scan_status(context, args):
    job_id = args.get("job_id")
    job = context.client.find_job(job_id) if job_id else None
    scene_id = args.get("scene_id")
    scans = context.repo.scans_for(int(scene_id), limit=3) if scene_id else []
    return {"job": job, "scans": scans}


def op_scan_cancel(context, args):
    job_id = args.get("job_id")
    if not job_id:
        return {"ok": False, "error": "job_id is required"}
    stopped = context.client.stop_job(job_id)
    # The killed process cannot write its own final status, so sweep for it.
    context.repo.sweep_stale_scans()
    return {"stopped": stopped}


# The task name in the manifest. Kept here because ops.py is what asks for it.
TASK_DISCOVER_SCENES = "Discover scenes"


# ------------------------------------------------------- the scraper shim's op

# What the shim should do, decided here so the shim itself holds no policy.
RETURNED = "returned"        # hand this scene to Stash's merge dialog
NO_CONSENSUS = "no_consensus"  # answers exist, none of them trustworthy
QUEUED = "queued"            # nothing stored; a scan has been started
RUNNING = "running"          # a scan for this scene is already going


def _scraper_hosts(context):
    """scraper id -> declared hosts, from the stored registry.

    Read from ScrapeDiscovery's own tables rather than by calling `listScrapers`: this
    runs while a user waits for a scrape menu, and the live instance has 780 scrapers.
    """
    from . import registry as registry_module
    known = context.repo.known_scrapers()
    empty = registry_module.Registry([], context.config)
    return {record["id"]: empty.hosts_of(record) for record in known.values()}


def op_scraper_entry(context, args):
    """Everything the scraper shim needs, in one call.

    The shim is a thin entry point on purpose (see docs/architecture.md section 2), so
    the decision of what to hand back - a scene, nothing, or a queued job - is made
    here, against the same engine and database everything else uses.
    """
    scene_id = int(args.get("scene_id") or 0)
    if not scene_id:
        return {"ok": False, "error": "scene_id is required"}

    context.repo.sweep_stale_scans()
    state = context.repo.scene_state(scene_id) or {}
    if state.get("status") == R.SCANNING:
        # Say so rather than starting a second scan of the same scene.
        return {"action": RUNNING, "scene": None,
                "message": "a discovery scan for this scene is already running"}

    results = context.repo.results_of_scene(scene_id)
    if results:
        scans = context.repo.scans_for(scene_id, limit=1)
        snapshot = (scans[0]["scene_snapshot"] if scans else None) or {}
        if args.get("refreshScene", True):
            try:
                live = context.client.find_scene(scene_id)
                if live:
                    snapshot = normalize.scene_snapshot(live)
            except Exception as exc:  # fall back to what the scan recorded
                from .executor import describe_error
                logs.debug("could not re-read scene %s: %s"
                           % (scene_id, describe_error(exc)))

        agreed = consensus.best(
            results, snapshot,
            threshold=float(context.config["titleMergeThreshold"]),
            scraper_hosts=_scraper_hosts(context))
        if agreed:
            image = None
            if agreed.get("image_sha256") and args.get("includeImage", True):
                blob = context.repo.blob(agreed["image_sha256"])
                if blob:
                    image = normalize.rebuild_data_uri({"$blob": blob["sha256"]}, blob)
            return {
                "action": RETURNED,
                "scene": consensus.to_scraped_scene(agreed, image),
                "consensus": {
                    "reason": agreed["reason"],
                    "independent_sources": agreed["independent_sources"],
                    "sources": agreed["sources"],
                    "witnesses": agreed["witnesses"],
                    "considered": agreed["considered"],
                    "without_evidence": agreed["without_evidence"],
                    "discarded_groups": agreed["discarded_groups"],
                },
            }
        return {"action": NO_CONSENSUS, "scene": None,
                "results": len(results),
                "message": "%d stored answer(s), none of them corroborated well enough "
                           "to offer as metadata - open ScrapeDiscovery to review them"
                           % len(results)}

    if not args.get("queue", True):
        return {"action": NO_CONSENSUS, "scene": None, "results": 0,
                "message": "nothing has been discovered for this scene yet"}

    mode = str(args.get("mode") or context.config["defaultMode"]).lower()
    if mode not in (settings.NORMAL, settings.DEEP):
        mode = settings.NORMAL
    job_id = context.client.run_plugin_task(
        settings.PLUGIN_ID, TASK_DISCOVER_SCENES,
        {"scene_ids": str(scene_id), "mode": mode, "trigger": "scraper"})
    return {"action": QUEUED, "scene": None, "job_id": job_id, "mode": mode,
            "message": "discovery queued (%s scan) - watch the job queue, then open "
                       "ScrapeDiscovery" % mode}


def op_consensus(context, args):
    """The consensus for a scene, for the UI to show alongside the raw answers."""
    scene_id = int(args.get("scene_id") or 0)
    results = context.repo.results_of_scene(scene_id)
    if not results:
        return {"consensus": None, "results": 0}
    scans = context.repo.scans_for(scene_id, limit=1)
    snapshot = (scans[0]["scene_snapshot"] if scans else None) or {}
    agreed = consensus.best(
        results, snapshot,
        threshold=float(context.config["titleMergeThreshold"]),
        scraper_hosts=_scraper_hosts(context))
    return {"consensus": agreed, "results": len(results),
            "preview": consensus.to_scraped_scene(agreed) if agreed else None}


HANDLERS = {
    "ping": op_ping,
    "diagnostics.info": op_diagnostics,
    "settings.get": op_settings_get,
    "settings.set": op_settings_set,
    "inbox.list": op_inbox,
    "scene.summary": op_scene_summary,
    "scene.detail": op_scene_detail,
    "result.raw": op_result_raw,
    "image.get": op_image,
    "scan.start": op_scan_start,
    "scan.status": op_scan_status,
    "scan.cancel": op_scan_cancel,
    "scraper.entry": op_scraper_entry,
    "consensus.get": op_consensus,
}
