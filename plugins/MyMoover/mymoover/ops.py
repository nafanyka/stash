"""Dispatch table for `runPluginOperation` calls from the UI.

Every handler returns a plain dict - `{"ok": True, ...}` or `{"ok": False, "error":
...}` - never raises past this module, so one bad call cannot take the whole plugin
process down with a stack trace Stash would show as a raw GraphQL error.
"""

from __future__ import annotations

import os

from . import mover, planner, validate
from stash_common import log


class Context:
    def __init__(self, client, plugin_settings):
        self.client = client
        self.settings = plugin_settings
        self._roots = None

    def roots(self):
        if self._roots is None:
            self._roots = validate.normalize_roots(self.client.library_roots())
        return self._roots


def _ok(**data):
    data["ok"] = True
    return data


def _fail(message):
    log.error("MyMoover: " + str(message))
    return {"ok": False, "error": str(message)}


def op_config(context, args):
    return _ok(
        roots=context.roots(),
        settings={
            "moveSidecars": context.settings.move_sidecars,
            "sidecarPatterns": list(context.settings.sidecar_patterns),
            "debugLogging": context.settings.debug_logging,
        },
    )


def op_folder_create(context, args):
    parent = str(args.get("parent_path") or "")
    name = validate.validate_new_folder_name(args.get("name"))
    real_parent = validate.require_within_roots(parent, context.roots(), "parent folder")
    new_path = os.path.join(real_parent, name)
    real_new_path = os.path.realpath(new_path)
    # realpath collapses no `..` is possible since `name` was already rejected if it
    # contained one, but this also catches a name that only *resolves* outside the
    # parent through a symlink already sitting in the parent directory.
    validate.require_within_roots(real_new_path, context.roots(), "new folder")

    if os.path.isdir(real_new_path):
        return _ok(path=real_new_path, created=False)
    if os.path.exists(real_new_path):
        return _fail("%s already exists and is not a folder" % real_new_path)
    os.makedirs(real_new_path)
    log.info("MyMoover: created folder %s" % real_new_path)
    return _ok(path=real_new_path, created=True)


def op_analyze(context, args):
    scene_ids = args.get("scene_ids") or []
    destination_folder = str(args.get("destination_folder") or "")
    if not scene_ids:
        return _fail("no scenes selected")
    plan = planner.build_plan(
        context.client, context.roots(), context.settings, scene_ids, destination_folder)
    counts = planner.summarize(plan["items"], use_result=False)
    return _ok(scenes=plan["scenes"], items=plan["items"], counts=counts,
                destination_folder=plan["destination_folder"])


def op_move(context, args):
    scene_ids = args.get("scene_ids") or []
    destination_folder = str(args.get("destination_folder") or "")
    submitted_items = args.get("items") or []
    if not scene_ids:
        return _fail("no scenes selected")
    result = mover.execute_move(
        context.client, context.roots(), context.settings,
        scene_ids, destination_folder, submitted_items)
    return _ok(**result)


def op_scan(context, args):
    paths = [str(p) for p in (args.get("paths") or []) if p]
    if not paths:
        return _fail("no paths to scan")
    roots = context.roots()
    real_paths = [validate.require_within_roots(path, roots, "scan path") for path in paths]
    job_id = context.client.scan_paths(real_paths)
    return _ok(job_id=job_id)


HANDLERS = {
    "config": op_config,
    "folder.create": op_folder_create,
    "analyze": op_analyze,
    "move": op_move,
    "scan": op_scan,
}


def dispatch(context, op_name, args):
    handler = HANDLERS.get(op_name)
    if not handler:
        return _fail("unknown operation %r (known: %s)" % (op_name, ", ".join(sorted(HANDLERS))))
    try:
        return handler(context, args)
    except validate.ValidationError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - the process-level boundary in MyMoover.py
        log.error("MyMoover: %s failed: %s: %s" % (op_name, type(exc).__name__, exc))
        return _fail("%s: %s" % (type(exc).__name__, exc))
