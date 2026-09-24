"""ops.dispatch: the runPluginOperation boundary - argument handling, and that every
handler comes back as a plain dict rather than raising."""

from __future__ import annotations

import os

import mm_common  # noqa: F401
from mm_common import FakeClient, make_scene

from mymoover import ops, settings

SETTINGS = settings.parse({})


def _context(tmp_path, scenes=None):
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    client = FakeClient(scenes or [], roots=[str(lib)])
    return ops.Context(client, SETTINGS), client, lib


def test_unknown_op_is_reported_not_raised(tmp_path):
    context, _client, _lib = _context(tmp_path)
    result = ops.dispatch(context, "no.such.op", {})
    assert result["ok"] is False
    assert "no.such.op" in result["error"]


def test_config_reports_roots_and_settings(tmp_path):
    context, _client, lib = _context(tmp_path)
    result = ops.dispatch(context, "config", {})
    assert result["ok"] is True
    assert result["roots"] == [os.path.realpath(str(lib))]
    assert result["settings"]["moveSidecars"] is True


def test_folder_create_rejects_traversal(tmp_path):
    context, _client, lib = _context(tmp_path)
    result = ops.dispatch(context, "folder.create",
                           {"parent_path": str(lib), "name": "../escape"})
    assert result["ok"] is False


def test_folder_create_rejects_outside_root(tmp_path):
    context, _client, lib = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    result = ops.dispatch(context, "folder.create", {"parent_path": str(outside), "name": "x"})
    assert result["ok"] is False


def test_folder_create_makes_the_directory_and_selects_it(tmp_path):
    context, _client, lib = _context(tmp_path)
    result = ops.dispatch(context, "folder.create", {"parent_path": str(lib), "name": "2026"})
    assert result["ok"] is True
    assert result["created"] is True
    assert os.path.isdir(result["path"])
    assert os.path.dirname(result["path"]) == os.path.realpath(str(lib))


def test_folder_create_is_idempotent_for_an_existing_directory(tmp_path):
    context, _client, lib = _context(tmp_path)
    (lib / "already").mkdir()
    result = ops.dispatch(context, "folder.create", {"parent_path": str(lib), "name": "already"})
    assert result["ok"] is True
    assert result["created"] is False


def test_analyze_then_move_round_trip(tmp_path):
    src = tmp_path / "library" / "src"
    src.mkdir(parents=True)
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    context, client, lib = _context(
        tmp_path, [make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))])])
    dest = lib / "dest"
    dest.mkdir()

    analyzed = ops.dispatch(context, "analyze", {"scene_ids": ["1"], "destination_folder": str(dest)})
    assert analyzed["ok"] is True
    assert analyzed["counts"]["media"]["MOVE"] == 1

    moved = ops.dispatch(context, "move", {
        "scene_ids": ["1"], "destination_folder": str(dest), "items": analyzed["items"],
    })
    assert moved["ok"] is True
    assert moved["counts"]["media"]["MOVED"] == 1
    assert (dest / "movie.mp4").exists()


def test_scan_requires_explicit_paths_and_validates_roots(tmp_path):
    context, client, lib = _context(tmp_path)
    result = ops.dispatch(context, "scan", {"paths": []})
    assert result["ok"] is False

    outside = tmp_path / "outside"
    outside.mkdir()
    result = ops.dispatch(context, "scan", {"paths": [str(outside)]})
    assert result["ok"] is False
    assert client.scan_calls == []

    result = ops.dispatch(context, "scan", {"paths": [str(lib)]})
    assert result["ok"] is True
    assert client.scan_calls == [[os.path.realpath(str(lib))]]
