"""Executing a Move: the only module that changes anything.

Flow per media file, in this order (requirement 24):

 1. Re-derive the CURRENT truth with a fresh `planner.build_plan` call - this is the
    "immediately before the destructive operation" recheck requirement 22 asks for.
    The `overwrite` decisions the user made are carried over from what the frontend
    submitted (which is the Analyze snapshot, annotated with checkbox state); nothing
    else about that submitted snapshot is trusted.
 2. If the item is still a conflict the user approved overwriting, verify the fresh
    conflict owner is the *same* one the user was shown - a mismatch means the
    filesystem or Stash changed underneath the decision, and the item is marked
    STATE_CHANGED and skipped rather than blindly overwritten (requirement 22).
 3. Clear the conflict through Stash's own mutations (never SQL) if approved, then
    call `moveFiles` - the one operation that moves the media file and updates
    `File.path` together.
 4. Only once the media file itself is confirmed moved (or was already in place) are
    its sidecars touched, so a video never ends up separated from files it needs
    (requirement 24's "не оставлять Stash в заведомо неконсистентном состоянии").

One item failing never stops the batch (requirement 23): every media file is fully
independent, wrapped in its own try/except.
"""

from __future__ import annotations

import os
import shutil

from . import planner, validate
from stash_common import log

RESULT_MOVED = "MOVED"
RESULT_ALREADY_THERE = "ALREADY_THERE"
RESULT_OVERWRITTEN = "OVERWRITTEN"
RESULT_SKIPPED = "SKIPPED"
RESULT_ERROR = "ERROR"
RESULT_STATE_CHANGED = "STATE_CHANGED"

_MOVED_OK = (RESULT_MOVED, RESULT_ALREADY_THERE, RESULT_OVERWRITTEN)


def safe_move(src: str, dst: str) -> None:
    """Mirrors Stash's own `fsutil.SafeMove`: rename, and only on failure (which is
    how a cross-device move surfaces on every supported OS) fall back to copy, verify
    the size, then remove the source (requirement 21)."""
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass
    shutil.copy2(src, dst)
    if os.path.getsize(dst) != os.path.getsize(src):
        try:
            os.remove(dst)
        except OSError:
            pass
        raise OSError("copy verification failed (size mismatch) for %s" % dst)
    os.remove(src)


def _cleanup_conflicting_file(client, owner_scene, target_path):
    """Frees `target_path` of a Stash-tracked File through the sanctioned sequence
    (requirement 15): demote-then-destroy for a non-primary-safe case, or destroy the
    whole Scene only when it has no other file to fall back to."""
    files = owner_scene.get("files") or []
    conflicting = next(
        (f for f in files if os.path.normpath(f["path"]) == os.path.normpath(target_path)),
        None)
    if not conflicting:
        raise RuntimeError("conflicting file disappeared from Stash between checks")

    log.info("MyMoover: clearing conflict at %s - owned by scene #%s (%s), file #%s"
              % (target_path, owner_scene["id"], owner_scene.get("title"), conflicting["id"]))

    if len(files) == 1:
        log.info("MyMoover: scene #%s has no other file; destroying the scene"
                  % owner_scene["id"])
        client.destroy_scene(owner_scene["id"])
        return

    is_primary = files[0]["id"] == conflicting["id"]
    if is_primary:
        other = next(f for f in files if f["id"] != conflicting["id"])
        log.info("MyMoover: reassigning scene #%s primary file to #%s before removing #%s"
                  % (owner_scene["id"], other["id"], conflicting["id"]))
        client.set_primary_file(owner_scene["id"], other["id"])
    client.delete_files([conflicting["id"]])


def _resolve_media_conflict(client, fresh_item, submitted_conflict):
    """True if cleared and safe to move into; False (with the item already marked) if
    not - either the user did not approve it, or the state no longer matches."""
    fresh_conflict = fresh_item["conflict"]
    if not fresh_item.get("overwrite"):
        fresh_item["result"] = RESULT_SKIPPED
        return False

    mismatch = (
        not submitted_conflict
        or bool(fresh_conflict.get("on_disk_only")) != bool(submitted_conflict.get("on_disk_only"))
        or fresh_conflict.get("owner_scene_id") != submitted_conflict.get("owner_scene_id")
    )
    if mismatch:
        fresh_item["result"] = RESULT_STATE_CHANGED
        fresh_item["error"] = "target changed since Analyze; not overwritten"
        log.warning("MyMoover: state changed at %s, skipping overwrite" % fresh_item["target_path"])
        return False

    try:
        if fresh_conflict.get("on_disk_only"):
            if os.path.exists(fresh_item["target_path"]):
                os.remove(fresh_item["target_path"])
        else:
            owner_scene = client.find_owning_scene(fresh_item["target_path"])
            if not owner_scene or str(owner_scene["id"]) != fresh_conflict.get("owner_scene_id"):
                fresh_item["result"] = RESULT_STATE_CHANGED
                fresh_item["error"] = "target's owning scene changed since Analyze"
                return False
            _cleanup_conflicting_file(client, owner_scene, fresh_item["target_path"])
    except Exception as exc:  # noqa: BLE001 - one bad conflict must not stop the batch
        fresh_item["result"] = RESULT_ERROR
        fresh_item["error"] = "could not clear conflict: %s" % exc
        log.error("MyMoover: " + fresh_item["error"])
        return False

    if os.path.exists(fresh_item["target_path"]):
        fresh_item["result"] = RESULT_ERROR
        fresh_item["error"] = "target still exists after clearing the conflict"
        return False
    return True


def _process_media(client, real_destination, fresh_item, submitted_item, affected_dirs):
    submitted_conflict = (submitted_item or {}).get("conflict")
    fresh_item["overwrite"] = bool((submitted_item or {}).get("overwrite"))
    status = fresh_item["status"]
    overwritten = False

    if status == planner.STATUS_ERROR:
        fresh_item["result"] = RESULT_ERROR
    elif status == planner.STATUS_ALREADY_THERE:
        fresh_item["result"] = RESULT_ALREADY_THERE
    elif status == planner.STATUS_CONFLICT:
        cleared = _resolve_media_conflict(client, fresh_item, submitted_conflict)
        if not cleared:
            return
        overwritten = True
        status = planner.STATUS_MOVE  # fall through to the actual move below
    if status == planner.STATUS_MOVE and fresh_item.get("result") is None:
        if os.path.exists(fresh_item["target_path"]):
            fresh_item["result"] = RESULT_STATE_CHANGED
            fresh_item["error"] = "a file appeared at the destination since Analyze"
            log.warning("MyMoover: " + fresh_item["error"] + " (%s)" % fresh_item["target_path"])
            return
        try:
            ok = client.move_file(fresh_item["file_id"], real_destination)
        except Exception as exc:  # noqa: BLE001
            fresh_item["result"] = RESULT_ERROR
            fresh_item["error"] = str(exc)
            log.error("MyMoover: moveFiles failed for %s: %s" % (fresh_item["source_path"], exc))
            return
        if not ok:
            fresh_item["result"] = RESULT_ERROR
            fresh_item["error"] = "moveFiles returned false"
            return
        fresh_item["result"] = RESULT_OVERWRITTEN if overwritten else RESULT_MOVED
        log.info("MyMoover: moved %s -> %s" % (fresh_item["source_path"], fresh_item["target_path"]))
        affected_dirs.add(os.path.dirname(fresh_item["source_path"]))
        affected_dirs.add(real_destination)


def _process_sidecar(sidecar, real_destination, affected_dirs):
    source, target = sidecar["source_path"], sidecar["target_path"]
    if not os.path.exists(source):
        sidecar["result"] = RESULT_ERROR
        sidecar["error"] = "source sidecar no longer exists"
        return
    same_dir = os.path.realpath(os.path.dirname(source)) == os.path.realpath(real_destination)
    if same_dir:
        sidecar["result"] = RESULT_ALREADY_THERE
        return
    exists = os.path.exists(target)
    if exists and not sidecar.get("overwrite"):
        sidecar["result"] = RESULT_SKIPPED
        return
    try:
        if exists:
            os.remove(target)
        safe_move(source, target)
    except OSError as exc:
        sidecar["result"] = RESULT_ERROR
        sidecar["error"] = str(exc)
        log.error("MyMoover: sidecar move failed for %s: %s" % (source, exc))
        return
    sidecar["result"] = RESULT_OVERWRITTEN if exists else RESULT_MOVED
    affected_dirs.add(os.path.dirname(source))
    affected_dirs.add(real_destination)


def execute_move(client, roots, plugin_settings, scene_ids, destination_folder, submitted_items):
    """Re-plans from scratch, then moves everything that is still safe to move.

    `submitted_items` is exactly what an `analyze` call returned, with `overwrite`
    set by the user on each conflict - see the module docstring for how it is used
    only as a set of *decisions*, never as trusted current state.
    """
    real_destination = validate.require_within_roots(
        destination_folder, roots, "destination folder")

    fresh = planner.build_plan(client, roots, plugin_settings, scene_ids, real_destination)
    submitted_by_id = {str(item.get("file_id")): item for item in (submitted_items or [])}
    affected_dirs: set[str] = set()

    for fresh_item in fresh["items"]:
        submitted_item = submitted_by_id.get(str(fresh_item["file_id"]))
        submitted_sidecars = {
            s["basename"]: s for s in (submitted_item or {}).get("sidecars") or []
        }
        try:
            _process_media(client, real_destination, fresh_item, submitted_item, affected_dirs)
        except Exception as exc:  # noqa: BLE001 - isolate this item's failure only
            fresh_item["result"] = RESULT_ERROR
            fresh_item["error"] = "unexpected error: %s" % exc
            log.error("MyMoover: unexpected error moving %s: %s"
                      % (fresh_item["source_path"], exc))

        media_ok = fresh_item.get("result") in _MOVED_OK
        for sidecar in fresh_item["sidecars"]:
            if not media_ok:
                sidecar["result"] = RESULT_SKIPPED
                sidecar["error"] = "media file was not moved"
                continue
            sidecar["overwrite"] = bool(
                submitted_sidecars.get(sidecar["basename"], {}).get("overwrite"))
            try:
                _process_sidecar(sidecar, real_destination, affected_dirs)
            except Exception as exc:  # noqa: BLE001
                sidecar["result"] = RESULT_ERROR
                sidecar["error"] = "unexpected error: %s" % exc
                log.error("MyMoover: unexpected error moving sidecar %s: %s"
                          % (sidecar["source_path"], exc))

    counts = planner.summarize(fresh["items"], use_result=True)
    errors = []
    for item in fresh["items"]:
        if item.get("result") in (RESULT_ERROR, RESULT_STATE_CHANGED):
            errors.append({"path": item["source_path"], "message": item.get("error")})
        for sidecar in item["sidecars"]:
            if sidecar.get("result") in (RESULT_ERROR, RESULT_STATE_CHANGED):
                errors.append({"path": sidecar["source_path"], "message": sidecar.get("error")})

    return {
        "scenes": fresh["scenes"],
        "items": fresh["items"],
        "counts": counts,
        "errors": errors,
        "affected_dirs": sorted(affected_dirs),
    }
