"""The Stash API surface MyMoover uses - the only module here that contains GraphQL.

Every operation was checked against the actual stashapp/stash schema and Go resolvers
(tag v0.31.1, cross-checked against `develop`):

* `moveFiles` does the physical move AND updates `File.path` in one server-side
  transaction (validated against the configured library roots, folder hierarchy
  auto-created, cross-device moves handled via `fsutil.SafeMove`), so it is used for
  every media file - never a hand-rolled filesystem move plus a manual path edit.
* `moveFiles` refuses outright if the destination already exists (`os.Stat` first,
  `"file %s already exists"`) - it never silently overwrites. A conflict must be
  cleared through `destroyFiles`/`deleteFiles`/`sceneUpdate`/`sceneDestroy` first.
* Sidecar files (funscripts, subtitles, ...) are not File-entities in Stash's schema
  at all, so they are moved by this plugin directly on disk - never through
  `moveFiles`, and never by editing anything in Stash's database.
* A Scene's primary file is reliably `files[0]`: the DB has a partial unique index
  (`unique_index_scenes_files_on_primary`) allowing at most one primary row per scene,
  and the resolver backing `Scene.files` always prepends that row. There is no
  GraphQL-exposed `is_primary`, so this ordering is the only, and a safe, read signal.
"""

from __future__ import annotations

from stash_common import graphql

SCENE_MOVE_FIELDS = "id title files { id path basename }"


class Client:
    def __init__(self, url, api_key=None, cookie=None):
        self.url = url
        self.api_key = api_key
        self.cookie = cookie

    def _call(self, query, variables=None, timeout=30):
        return graphql.call(self.url, query, variables, self.api_key, timeout, self.cookie)

    def _try_call(self, query, variables=None, timeout=15):
        return graphql.try_call(self.url, query, variables, self.api_key, timeout, self.cookie)

    # -- configuration -------------------------------------------------------

    def plugin_settings(self, plugin_id):
        data = self._try_call("query { configuration { plugins } }")
        plugins = ((data or {}).get("configuration") or {}).get("plugins") or {}
        values = plugins.get(plugin_id)
        return values if isinstance(values, dict) else {}

    def library_roots(self):
        """Every configured `stashes` path - the only allowed destination roots."""
        data = self._try_call(
            "query { configuration { general { stashes { path } } } }")
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        return [row["path"] for row in (general.get("stashes") or []) if row.get("path")]

    # -- reading scenes/files --------------------------------------------------

    def find_scenes_for_move(self, scene_ids):
        """Scenes and every one of their files, in one round trip (requirement 30)."""
        ids = [str(one) for one in (scene_ids or [])]
        if not ids:
            return []
        data = self._try_call(
            "query($ids: [ID!]) { findScenes(ids: $ids, filter: {per_page: -1})"
            " { scenes { %s } } }" % SCENE_MOVE_FIELDS,
            {"ids": ids}, timeout=60)
        return ((data or {}).get("findScenes") or {}).get("scenes") or []

    def find_owning_scene(self, path):
        """The Scene (if any) that has a file at exactly this path, with all its files.

        `files[0]` of the result is that scene's primary file - see the module
        docstring for why that ordering can be trusted.
        """
        data = self._try_call(
            "query($path: String!) { findScenes(scene_filter: {path: {value: $path,"
            " modifier: EQUALS}}, filter: {per_page: 1}) { scenes { %s } } }"
            % SCENE_MOVE_FIELDS,
            {"path": path}, timeout=20)
        scenes = ((data or {}).get("findScenes") or {}).get("scenes") or []
        return scenes[0] if scenes else None

    # -- writing ---------------------------------------------------------------

    def move_file(self, file_id, destination_folder):
        """moveFiles for exactly one file, keeping its existing basename."""
        data = self._call(
            "mutation($ids: [ID!]!, $folder: String!) { moveFiles(input:"
            " { ids: $ids, destination_folder: $folder }) }",
            {"ids": [str(file_id)], "folder": destination_folder}, timeout=120)
        return bool(data.get("moveFiles"))

    def delete_files(self, file_ids):
        """`deleteFiles`: removes the DB row AND the physical file (or its trash)."""
        ids = [str(one) for one in file_ids]
        if not ids:
            return True
        data = self._call(
            "mutation($ids: [ID!]!) { deleteFiles(ids: $ids) }",
            {"ids": ids}, timeout=60)
        return bool(data.get("deleteFiles"))

    def set_primary_file(self, scene_id, file_id):
        data = self._call(
            "mutation($id: ID!, $fileId: ID!) { sceneUpdate(input:"
            " { id: $id, primary_file_id: $fileId }) { id } }",
            {"id": str(scene_id), "fileId": str(file_id)}, timeout=30)
        return data.get("sceneUpdate")

    def destroy_scene(self, scene_id):
        """Used only when a conflicting file is the primary AND only file of its
        Scene - the one case with no way to free the path without removing the Scene
        (requirement 15 step I). `delete_file` removes the physical file,
        `destroy_file_entry` removes the File/Folder rows too.
        """
        data = self._call(
            "mutation($id: ID!) { sceneDestroy(input: { id: $id, delete_file: true,"
            " destroy_file_entry: true }) }",
            {"id": str(scene_id)}, timeout=60)
        return bool(data.get("sceneDestroy"))

    def scan_paths(self, paths):
        """A targeted rescan, never triggered automatically - only from a button the
        user presses after a Move (requirements 18/19/26)."""
        data = self._call(
            "mutation($paths: [String!]) { metadataScan(input: { paths: $paths }) }",
            {"paths": list(paths or [])}, timeout=30)
        return data.get("metadataScan")
