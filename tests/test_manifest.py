"""The manifest and the code have to agree.

Stash matches a task by the exact string in the yml, and reads a setting by the exact
key, so a rename on one side and not the other produces a task that cannot run or a
setting that silently does nothing. Neither failure is visible until someone tries it,
which is what these tests are for.
"""

from __future__ import annotations

import os

import yaml

from conftest import PLUGIN_DIR
from scrapediscovery import ops, settings as S, tasks

MANIFEST = os.path.join(PLUGIN_DIR, "ScrapeDiscovery.yml")


def manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
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
        assert command[1].endswith("ScrapeDiscovery.py")
        assert "{pluginDir}" in command[1]


class TestTasks:
    def test_every_declared_task_has_a_handler(self):
        declared = [task["name"] for task in manifest()["tasks"]]
        assert [name for name in declared if name not in tasks.TASKS] == []

    def test_every_handler_is_declared(self):
        declared = {task["name"] for task in manifest()["tasks"]}
        assert [name for name in tasks.TASKS if name not in declared] == []

    def test_each_task_passes_its_own_name_through_default_args(self):
        # The entry point dispatches on args["task"], which is the only thing Stash
        # sends; if defaultArgs disagreed with the name the task would not run.
        for task in manifest()["tasks"]:
            assert (task.get("defaultArgs") or {}).get("task") == task["name"]

    def test_the_task_the_ui_starts_is_a_real_one(self):
        assert ops.TASK_DISCOVER_SCENES == tasks.DISCOVER_SCENES
        assert ops.TASK_DISCOVER_SCENES in tasks.TASKS

    def test_every_task_has_a_description(self):
        for task in manifest()["tasks"]:
            assert task.get("description")


class TestSettings:
    def test_every_declared_setting_exists_in_the_spec(self):
        # A setting Stash shows but the code never reads is a control that does
        # nothing, which is worse than not offering it.
        declared = manifest()["settings"]
        assert [key for key in declared if key not in S.SPEC] == []

    def test_declared_types_match_the_spec(self):
        for key, entry in manifest()["settings"].items():
            assert entry["type"] == S.SPEC[key][0], key

    def test_each_declared_setting_has_a_display_name_and_description(self):
        for key, entry in manifest()["settings"].items():
            assert entry.get("displayName"), key
            assert entry.get("description"), key

    def test_structured_settings_are_not_declared_in_the_manifest(self):
        # Stash only offers STRING, NUMBER and BOOLEAN, so a JSON setting would show
        # up as a raw text box; those live on the ScrapeDiscovery settings page.
        declared = manifest()["settings"]
        json_keys = [key for key, spec in S.SPEC.items() if spec[0] == S.JSON_]
        assert [key for key in json_keys if key in declared] == []


class TestUI:
    def test_the_injected_files_exist(self):
        ui = manifest()["ui"]
        for relative in list(ui.get("javascript") or []) + list(ui.get("css") or []):
            assert os.path.isfile(os.path.join(PLUGIN_DIR, relative)), relative

    def test_no_external_hosts_are_pulled_in(self):
        # Everything is same-origin, which is why the manifest needs no csp block.
        ui = manifest()["ui"]
        for relative in list(ui.get("javascript") or []) + list(ui.get("css") or []):
            assert not str(relative).startswith("http")
        assert "csp" not in ui

    def test_the_page_route_is_not_under_the_server_owned_prefix(self):
        # /plugin/... is mounted by the Stash server, so a client route there 404s on
        # a hard reload. Verified against a live instance.
        with open(os.path.join(PLUGIN_DIR, "ui", "scrapediscovery.js"),
                  encoding="utf-8") as handle:
            source = handle.read()
        assert 'var BASE = "/scrape-discovery"' in source
        assert 'BASE = "/plugin' not in source
