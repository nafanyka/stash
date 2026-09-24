"""MyMoover: the manifest and the wiring between the frontend and the backend ops.

A manifest naming a file that was renamed, or a frontend `callOp("x", ...)` whose
backend never registers `"x"`, fails silently at runtime in the browser - nothing in
the Python test suite would otherwise catch it.
"""

from __future__ import annotations

import os
import re

import mm_common  # noqa: F401
import yaml

from mymoover import ops

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, os.pardir, "plugins", "MyMoover")
MANIFEST = os.path.join(PLUGIN_DIR, "MyMoover.yml")


def manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def source(*parts):
    with open(os.path.join(PLUGIN_DIR, *parts), encoding="utf-8") as handle:
        return handle.read()


def test_the_filename_is_the_plugin_id():
    assert os.path.basename(MANIFEST) == "MyMoover.yml"


def test_it_runs_synchronously_with_no_job_queue():
    # requirement 20: a bulk move never goes through Stash's job queue.
    assert "tasks" not in manifest()
    assert "hooks" not in manifest()


def test_every_file_the_manifest_names_exists():
    parsed = manifest()
    entries = list(parsed["ui"]["javascript"]) + list(parsed["ui"]["css"])
    entries.append(parsed["exec"][1].replace("{pluginDir}/", ""))
    missing = [one for one in entries if not os.path.isfile(os.path.join(PLUGIN_DIR, one))]
    assert missing == []


def test_it_has_a_version_and_a_description():
    parsed = manifest()
    assert re.match(r"^\d+\.\d+\.\d+$", str(parsed["version"]))
    assert len(parsed["description"]) > 40


def test_settings_match_what_settings_py_reads():
    declared = set(manifest()["settings"])
    assert declared == {"moveSidecars", "sidecarPatterns", "debugLogging"}


def test_only_the_sceneList_patch_point_is_used():
    # SceneListOperations, referenced by a sibling plugin's own notes, does not exist
    # in the actual current Stash source (verified against stashapp/stash v0.31.1 and
    # develop) - SceneList is the real, stable, patchable equivalent. See the plugin
    # README for the full writeup.
    used = set(re.findall(r'api\.patch\.\w+\("([^"]+)"', source("ui", "mymoover.js")))
    assert used == {"SceneList"}


def test_every_frontend_op_call_has_a_backend_handler():
    calls = set(re.findall(r'callOp\("([^"]+)"', source("ui", "mymoover.js")))
    assert calls
    assert calls <= set(ops.HANDLERS)


def test_every_backend_op_is_called_from_the_frontend():
    # The reverse direction: a handler nothing calls is dead code nobody would notice.
    text = source("ui", "mymoover.js")
    for name in ops.HANDLERS:
        assert ('callOp("%s"' % name) in text


def test_nothing_touches_the_dom_directly():
    text = source("ui", "mymoover.js")
    for banned in ("document.querySelector", "document.getElementById",
                   "appendChild", "innerHTML", "MutationObserver"):
        assert banned not in text


def test_loading_twice_is_harmless():
    assert "if (window.MyMoover)" in source("ui", "mymoover.js")
