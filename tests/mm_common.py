"""Shared test fixtures for MyMoover: puts the plugin package and stash_common on the
path, and a FakeClient standing in for a real Stash server.

FakeClient mutates a small in-memory scene/file store and the real filesystem
together, the same way the real GraphQL mutations MyMoover calls would - so
`mover.execute_move` can be exercised against it exactly as it runs against Stash,
with no server needed.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MM_PLUGIN_DIR = os.path.join(ROOT, "plugins", "MyMoover")
SHARED_DIR = os.path.join(ROOT, "common", "python")
for path in (MM_PLUGIN_DIR, SHARED_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


def make_scene(scene_id, title, files):
    """`files` is a list of (file_id, path) pairs; the first is primary."""
    return {
        "id": str(scene_id),
        "title": title,
        "files": [{"id": str(fid), "path": path, "basename": os.path.basename(path)}
                  for fid, path in files],
    }


class FakeClient:
    def __init__(self, scenes=None, roots=None):
        self.scenes = {s["id"]: s for s in (scenes or [])}
        self.roots_value = list(roots or [])
        self.moved_calls = []
        self.deleted_ids = []
        self.primary_calls = []
        self.destroyed_scenes = []
        self.scan_calls = []

    # -- configuration -------------------------------------------------------

    def library_roots(self):
        return self.roots_value

    def plugin_settings(self, plugin_id):
        return {}

    # -- reading ---------------------------------------------------------------

    def find_scenes_for_move(self, scene_ids):
        wanted = {str(i) for i in scene_ids}
        return [scene for scene in self.scenes.values() if scene["id"] in wanted]

    def find_owning_scene(self, path):
        target = os.path.normpath(path)
        for scene in self.scenes.values():
            for file in scene["files"]:
                if os.path.normpath(file["path"]) == target:
                    return scene
        return None

    # -- writing -----------------------------------------------------------

    def move_file(self, file_id, destination_folder):
        self.moved_calls.append((str(file_id), destination_folder))
        for scene in self.scenes.values():
            for file in scene["files"]:
                if str(file["id"]) == str(file_id):
                    new_path = os.path.join(destination_folder, file["basename"])
                    if os.path.exists(new_path):
                        raise RuntimeError("file %s already exists" % new_path)
                    os.rename(file["path"], new_path)
                    file["path"] = new_path
                    return True
        return False

    def delete_files(self, file_ids):
        ids = {str(i) for i in file_ids}
        for scene in self.scenes.values():
            remaining = []
            for file in scene["files"]:
                if str(file["id"]) in ids:
                    if os.path.exists(file["path"]):
                        os.remove(file["path"])
                else:
                    remaining.append(file)
            scene["files"] = remaining
        self.deleted_ids.extend(ids)
        return True

    def set_primary_file(self, scene_id, file_id):
        self.primary_calls.append((str(scene_id), str(file_id)))
        scene = self.scenes[str(scene_id)]
        files = scene["files"]
        index = next(i for i, f in enumerate(files) if f["id"] == str(file_id))
        files.insert(0, files.pop(index))
        return {"id": scene_id}

    def destroy_scene(self, scene_id):
        self.destroyed_scenes.append(str(scene_id))
        scene = self.scenes.pop(str(scene_id), None)
        if scene:
            for file in scene["files"]:
                if os.path.exists(file["path"]):
                    os.remove(file["path"])
        return True

    def scan_paths(self, paths):
        self.scan_calls.append(list(paths))
        return "job-1"
