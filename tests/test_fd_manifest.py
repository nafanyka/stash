"""The manifests and the code have to agree.

Stash matches a task by the exact string in the yml and reads a setting by the exact
key, so a rename on one side and not the other produces a task that cannot run or a
setting that silently does nothing. Neither failure is visible until somebody tries it.
"""

from __future__ import annotations

import os
import re

import yaml
from fd_common import FD_PLUGIN_DIR  # noqa: F401  (its import adds FastDiscovery to sys.path)

from fastdiscovery import ops, settings as S, tasks

MANIFEST = os.path.join(FD_PLUGIN_DIR, "FastDiscovery.yml")
SCRAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(FD_PLUGIN_DIR)),
                           "scrapers", "FastDiscovery")
SCRAPER_MANIFEST = os.path.join(SCRAPER_DIR, "FastDiscovery.yml")
SCRAPER_SOURCE = os.path.join(SCRAPER_DIR, "FastDiscovery.py")


def manifest(path=MANIFEST):
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class TestIdentity:
    def test_the_plugin_id_matches_the_manifest_filename(self):
        # Stash derives the id from the filename, and the settings lookup uses the id.
        assert S.PLUGIN_ID + ".yml" == os.path.basename(MANIFEST)

    def test_the_interface_is_raw_because_that_is_what_the_entry_point_speaks(self):
        assert manifest()["interface"] == "raw"

    def test_the_exec_line_points_at_the_entry_point(self):
        command = manifest()["exec"]
        assert command[0] == "python"
        assert command[1].endswith("FastDiscovery.py")
        assert "{pluginDir}" in command[1]

    def test_the_ui_files_exist(self):
        for kind in ("javascript", "css"):
            for relative in manifest()["ui"][kind]:
                assert os.path.isfile(os.path.join(FD_PLUGIN_DIR, relative)), relative


class TestTasks:
    def test_every_declared_task_has_a_handler(self):
        declared = [task["name"] for task in manifest()["tasks"]]
        assert [name for name in declared if name not in tasks.TASKS] == []

    def test_every_handler_is_declared(self):
        declared = {task["name"] for task in manifest()["tasks"]}
        assert [name for name in tasks.TASKS if name not in declared] == []

    def test_each_task_passes_its_own_name_through_default_args(self):
        # The entry point dispatches on args["task"], which is the only thing Stash
        # sends; if defaultArgs disagreed with the name, the task would not run.
        for task in manifest()["tasks"]:
            assert (task.get("defaultArgs") or {}).get("task") == task["name"]

    def test_the_task_the_ui_starts_is_a_real_one(self):
        assert ops.TASK_DISCOVER == tasks.DISCOVER
        assert ops.TASK_DISCOVER in tasks.TASKS

    def test_every_task_has_a_description(self):
        assert all(task.get("description") for task in manifest()["tasks"])


class TestSettings:
    def test_every_declared_setting_is_one_the_code_knows(self):
        declared = manifest()["settings"]
        assert [name for name in declared if name not in S.SPEC] == []

    def test_declared_types_match_the_code(self):
        for name, entry in manifest()["settings"].items():
            assert entry["type"] == S.TYPES[name], name

    def test_every_setting_is_declared_so_it_is_visible_in_stash(self):
        declared = set(manifest()["settings"])
        assert [name for name in S.SPEC if name not in declared] == []

    def test_no_setting_holds_a_stash_box_credential(self):
        # Requirement 2: the endpoints and their API keys belong to Stash.
        text = open(MANIFEST, encoding="utf-8").read().lower()
        assert "api key" not in text.replace("api keys", "")
        for name in manifest()["settings"]:
            assert "endpoint" not in name.lower()


class TestScraperShim:
    def test_the_scraper_is_named_after_its_folder(self):
        assert manifest(SCRAPER_MANIFEST)["name"] == "FastDiscovery"

    def test_it_registers_as_a_scene_fragment_scraper(self):
        config = manifest(SCRAPER_MANIFEST)
        assert config["sceneByFragment"]["action"] == "script"
        assert config["sceneByFragment"]["script"][:2] == ["python", "FastDiscovery.py"]

    def test_the_build_can_read_its_version_and_description(self):
        text = open(SCRAPER_MANIFEST, encoding="utf-8").read()
        assert re.search(r"^#\s*version:\s*\S+", text, re.MULTILINE)
        assert re.search(r"^#\s*description:\s*\S+", text, re.MULTILINE)

    def test_the_shim_never_returns_a_scene(self):
        # The one property that keeps FastDiscovery out of Identify's way: whatever
        # happens, the scraper's answer is null, so nothing can be written unreviewed.
        source = open(SCRAPER_SOURCE, encoding="utf-8").read()
        printed = re.findall(r"^\s*print\((.+)\)\s*$", source, re.MULTILINE)
        assert printed
        assert all(call.strip() == '"null"' for call in printed), printed

    def test_the_shim_holds_no_discovery_logic(self):
        source = open(SCRAPER_SOURCE, encoding="utf-8").read()
        for forbidden in ("scrapeSingleScene", "scrapeSceneURL", "import sqlite3",
                          "fastdiscovery"):
            assert forbidden not in source, forbidden

    def test_the_shim_is_on_the_never_invoke_list(self):
        # Otherwise a run could invoke the shim, which starts a run.
        assert "fastdiscovery" in S.NEVER_INVOKE
