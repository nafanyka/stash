"""Building the preflight plan (Analyze) - a pure read-only snapshot.

Nothing in this module touches the filesystem or Stash except to *read* it
(requirement 12: no filesystem changes during Analyze). `build_plan` is called again,
unchanged, right before Move actually executes (requirement 22) - the caller compares
the fresh snapshot against what the user was shown and refuses anything that drifted.
"""

from __future__ import annotations

import os

from . import sidecars, validate

STATUS_MOVE = "MOVE"
STATUS_ALREADY_THERE = "ALREADY_THERE"
STATUS_CONFLICT = "CONFLICT"
STATUS_ERROR = "ERROR"


def _same_dir(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _dedupe_files(scenes):
    """scene rows (id, title, files[]) -> {file_id: {path, basename, scene_ids, titles}}

    A file shared by more than one selected Scene (requirement 28) collapses to one
    entry; every scene it belongs to is kept so the UI can show them all.
    """
    files: dict[str, dict] = {}
    for scene in scenes:
        for file in scene.get("files") or []:
            file_id = str(file["id"])
            entry = files.setdefault(file_id, {
                "path": file["path"],
                "basename": file["basename"],
                "scene_ids": [],
                "scene_titles": [],
            })
            entry["scene_ids"].append(str(scene["id"]))
            entry["scene_titles"].append(scene.get("title") or ("Scene " + str(scene["id"])))
    return files


def _plan_sidecar(source_dir, destination_folder, name):
    source_path = os.path.join(source_dir, name)
    target_path = os.path.join(destination_folder, name)
    if _same_dir(source_dir, destination_folder):
        status = STATUS_ALREADY_THERE
    elif os.path.exists(target_path):
        status = STATUS_CONFLICT
    else:
        status = STATUS_MOVE
    return {
        "source_path": source_path,
        "basename": name,
        "target_path": target_path,
        "status": status,
        "overwrite": False,
    }


def _plan_media(client, destination_folder, file_id, info, patterns, dir_cache, move_sidecars):
    source_path = info["path"]
    basename = info["basename"]
    source_dir = os.path.dirname(source_path)
    target_path = os.path.join(destination_folder, basename)

    item = {
        "file_id": file_id,
        "scene_ids": info["scene_ids"],
        "scene_titles": info["scene_titles"],
        "source_path": source_path,
        "basename": basename,
        "target_path": target_path,
        "status": None,
        "error": None,
        "conflict": None,
        "overwrite": False,
        "sidecars": [],
    }

    if not os.path.exists(source_path):
        item["status"] = STATUS_ERROR
        item["error"] = "source file no longer exists on disk"
    elif _same_dir(source_dir, destination_folder):
        item["status"] = STATUS_ALREADY_THERE
    elif os.path.exists(target_path):
        item["status"] = STATUS_CONFLICT
        owner = client.find_owning_scene(target_path)
        item["conflict"] = _describe_conflict(owner, target_path, file_id)
    else:
        item["status"] = STATUS_MOVE

    if move_sidecars and item["status"] != STATUS_ERROR:
        names = sidecars.find_sidecars(source_dir, basename, patterns, dir_cache)
        item["sidecars"] = [_plan_sidecar(source_dir, destination_folder, name)
                             for name in sorted(names)]

    return item


def _describe_conflict(owner_scene, target_path, moving_file_id):
    if not owner_scene:
        return {
            "owner_scene_id": None,
            "owner_scene_title": None,
            "owner_file_id": None,
            "owner_is_only_file": None,
            "on_disk_only": True,
        }
    files = owner_scene.get("files") or []
    owner_file = next(
        (f for f in files if os.path.normpath(f["path"]) == os.path.normpath(target_path)),
        None)
    # The scene that owns the target is itself the one being moved (its file id is the
    # one we are about to place there) - not a real conflict with another entity.
    if owner_file and str(owner_file["id"]) == str(moving_file_id):
        return None
    return {
        "owner_scene_id": str(owner_scene["id"]),
        "owner_scene_title": owner_scene.get("title") or ("Scene " + str(owner_scene["id"])),
        "owner_file_id": str(owner_file["id"]) if owner_file else None,
        "owner_is_only_file": len(files) == 1,
        "on_disk_only": False,
    }


def build_plan(client, roots, plugin_settings, scene_ids, destination_folder):
    """The full preflight snapshot for a selection and a chosen destination."""
    real_destination = validate.require_within_roots(
        destination_folder, roots, "destination folder")

    scenes = client.find_scenes_for_move(scene_ids)
    files = _dedupe_files(scenes)
    dir_cache = sidecars.DirCache()

    items = [
        _plan_media(client, real_destination, file_id, info,
                    plugin_settings.sidecar_patterns, dir_cache, plugin_settings.move_sidecars)
        for file_id, info in files.items()
    ]
    # Deterministic order for a stable UI: by first scene title, then basename.
    items.sort(key=lambda item: (item["scene_titles"][0] if item["scene_titles"] else "",
                                  item["basename"]))

    return {
        "destination_folder": real_destination,
        "scenes": len(scenes),
        "items": items,
    }


def summarize(items, use_result=False):
    """Counts for the modal's summary line, from either `status` (Analyze) or
    `result` (after Move) - same shape either way."""
    key = "result" if use_result else "status"
    media = {"MOVE": 0, "ALREADY_THERE": 0, "CONFLICT": 0, "ERROR": 0,
              "OVERWRITTEN": 0, "SKIPPED": 0, "STATE_CHANGED": 0}
    sidecar_counts = {"MOVE": 0, "ALREADY_THERE": 0, "CONFLICT": 0, "ERROR": 0,
                       "OVERWRITTEN": 0, "SKIPPED": 0, "STATE_CHANGED": 0}
    for item in items:
        value = item.get(key) or item.get("status")
        media[value] = media.get(value, 0) + 1
        for sidecar in item.get("sidecars") or []:
            value = sidecar.get(key) or sidecar.get("status")
            sidecar_counts[value] = sidecar_counts.get(value, 0) + 1
    return {"media": media, "sidecars": sidecar_counts}
