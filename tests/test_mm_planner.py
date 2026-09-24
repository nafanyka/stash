"""planner.build_plan: the dry-run snapshot. No filesystem or Stash writes happen
here - every test only asserts on the classification.

Letters in the docstrings match the scenarios named in the MyMoover design notes.
"""

from __future__ import annotations

import os

import mm_common  # noqa: F401
from mm_common import FakeClient, make_scene

from mymoover import planner, settings

SETTINGS = settings.parse({})


def _lib(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


def test_a_single_scene_single_file_moves(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    client = FakeClient([make_scene(1, "Scene A", [(100, str(src / "movie.mp4"))])],
                         roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    assert plan["scenes"] == 1
    assert len(plan["items"]) == 1
    item = plan["items"][0]
    assert item["status"] == planner.STATUS_MOVE
    assert item["target_path"] == os.path.join(os.path.realpath(str(dest)), "movie.mp4")


def test_b_ten_scenes_in_different_dirs_all_target_one_destination(tmp_path):
    lib = _lib(tmp_path)
    dest = lib / "dest"
    dest.mkdir()
    scenes = []
    for i in range(10):
        src = lib / ("src%d" % i)
        src.mkdir()
        (src / "movie.mp4").write_text("x", encoding="utf-8")
        scenes.append(make_scene(i, "Scene %d" % i, [(i, str(src / "movie.mp4"))]))

    client = FakeClient(scenes, roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, [str(i) for i in range(10)], str(dest))

    assert plan["scenes"] == 10
    assert len(plan["items"]) == 10
    real_dest = os.path.realpath(str(dest))
    for item in plan["items"]:
        assert item["status"] == planner.STATUS_MOVE
        assert os.path.dirname(item["target_path"]) == real_dest


def test_c_scene_with_three_files_moves_all_three(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (src / name).write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    scene = make_scene(1, "Scene A", [
        (1, str(src / "a.mp4")), (2, str(src / "b.mp4")), (3, str(src / "c.mp4"))])
    client = FakeClient([scene], roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    assert len(plan["items"]) == 3
    assert {i["basename"] for i in plan["items"]} == {"a.mp4", "b.mp4", "c.mp4"}
    assert all(i["status"] == planner.STATUS_MOVE for i in plan["items"])


def test_d_file_already_in_destination(tmp_path):
    lib = _lib(tmp_path)
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("x", encoding="utf-8")

    client = FakeClient([make_scene(1, "Scene A", [(1, str(dest / "movie.mp4"))])],
                         roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    assert plan["items"][0]["status"] == planner.STATUS_ALREADY_THERE


def test_e_target_exists_but_not_tracked_by_stash(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("already here, not in stash", encoding="utf-8")

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))])],
                         roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    item = plan["items"][0]
    assert item["status"] == planner.STATUS_CONFLICT
    assert item["conflict"]["on_disk_only"] is True
    assert item["conflict"]["owner_scene_id"] is None


def test_f_target_belongs_to_another_scene(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("owned by scene 456", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        make_scene(456, "Existing Scene", [(999, str(dest / "movie.mp4"))]),
    ], roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    item = plan["items"][0]
    assert item["status"] == planner.STATUS_CONFLICT
    assert item["conflict"]["on_disk_only"] is False
    assert item["conflict"]["owner_scene_id"] == "456"
    assert item["conflict"]["owner_scene_title"] == "Existing Scene"
    assert item["conflict"]["owner_file_id"] == "999"
    assert item["conflict"]["owner_is_only_file"] is True


def test_l_sidecar_conflict_is_independent_of_media_status(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    (src / "movie.funscript").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    # No movie.mp4 at the destination, but the sidecar is already there.
    (dest / "movie.funscript").write_text("existing funscript", encoding="utf-8")

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))])],
                         roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1"], str(dest))

    item = plan["items"][0]
    assert item["status"] == planner.STATUS_MOVE
    sidecar = next(s for s in item["sidecars"] if s["basename"] == "movie.funscript")
    assert sidecar["status"] == planner.STATUS_CONFLICT


def test_q_file_shared_by_two_selected_scenes_appears_once(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    shared_file = (1, str(src / "movie.mp4"))
    client = FakeClient([
        make_scene(1, "Scene A", [shared_file]),
        make_scene(2, "Scene B", [shared_file]),
    ], roots=[str(lib)])
    plan = planner.build_plan(client, [str(lib)], SETTINGS, ["1", "2"], str(dest))

    assert len(plan["items"]) == 1
    assert sorted(plan["items"][0]["scene_ids"]) == ["1", "2"]
