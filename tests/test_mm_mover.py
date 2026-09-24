"""mover.execute_move: the only module allowed to touch a filesystem or Stash.

Every test re-runs `planner.build_plan` itself to get a realistic "Analyze" snapshot
(exactly what the frontend would submit back, with `overwrite` set on it), then calls
`execute_move` and checks both the reported result and the actual filesystem/FakeClient
state - a wrong report with the right disk state (or vice versa) should fail a test.
"""

from __future__ import annotations

import os

import mm_common  # noqa: F401
from mm_common import FakeClient, make_scene

from mymoover import mover, planner, settings

SETTINGS = settings.parse({})


def _lib(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


def _analyze(client, roots, scene_ids, destination):
    return planner.build_plan(client, roots, SETTINGS, scene_ids, destination)


def test_g_unchecked_conflict_is_skipped(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("new content", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("owned by 456", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        make_scene(456, "Existing", [(999, str(dest / "movie.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    # overwrite left False - the default for every conflict.

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert result["items"][0]["result"] == mover.RESULT_SKIPPED
    assert os.path.exists(src / "movie.mp4")            # source untouched
    assert (dest / "movie.mp4").read_text(encoding="utf-8") == "owned by 456"
    assert client.destroyed_scenes == []
    assert "456" in client.scenes


def test_h_checked_conflict_is_overwritten(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("new content", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("stale content", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        make_scene(456, "Existing", [(999, str(dest / "movie.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    plan["items"][0]["overwrite"] = True

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert result["items"][0]["result"] == mover.RESULT_OVERWRITTEN
    assert not os.path.exists(src / "movie.mp4")
    assert (dest / "movie.mp4").read_text(encoding="utf-8") == "new content"


def test_i_conflicting_scenes_only_file_destroys_the_scene(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("new", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("old", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        make_scene(456, "Existing", [(999, str(dest / "movie.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    plan["items"][0]["overwrite"] = True

    mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert client.destroyed_scenes == ["456"]
    assert "456" not in client.scenes


def test_j_conflicting_scenes_other_files_survive(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("new", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("old primary", encoding="utf-8")
    (dest / "other.mp4").write_text("secondary", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        # movie.mp4 (999) is primary (listed first); other.mp4 (998) is secondary.
        make_scene(456, "Existing", [(999, str(dest / "movie.mp4")), (998, str(dest / "other.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    plan["items"][0]["overwrite"] = True

    mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert client.destroyed_scenes == []
    assert "456" in client.scenes
    remaining = client.scenes["456"]["files"]
    assert [f["id"] for f in remaining] == ["998"]
    assert client.primary_calls == [("456", "998")]
    assert "999" in client.deleted_ids
    assert (dest / "movie.mp4").read_text(encoding="utf-8") == "new"


def test_k_multi_axis_funscripts_move_with_the_video(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    for name in ("video.mp4", "video.funscript", "video.L0.funscript", "video.L1.funscript"):
        (src / name).write_text(name, encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "video.mp4"))])], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert result["items"][0]["result"] == mover.RESULT_MOVED
    for name in ("video.funscript", "video.L0.funscript", "video.L1.funscript"):
        assert (dest / name).exists()
        assert not (src / name).exists()


def test_l_sidecar_conflict_decided_independently_of_media(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "video.mp4").write_text("video", encoding="utf-8")
    (src / "video.funscript").write_text("new script", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "video.funscript").write_text("old script", encoding="utf-8")

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "video.mp4"))])], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    item = plan["items"][0]
    assert item["status"] == planner.STATUS_MOVE
    sidecar = next(s for s in item["sidecars"] if s["basename"] == "video.funscript")
    sidecar["overwrite"] = True

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert result["items"][0]["result"] == mover.RESULT_MOVED
    moved_sidecar = next(s for s in result["items"][0]["sidecars"] if s["basename"] == "video.funscript")
    assert moved_sidecar["result"] == mover.RESULT_OVERWRITTEN
    assert (dest / "video.funscript").read_text(encoding="utf-8") == "new script"


def test_m_empty_source_directory_is_not_deleted(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))])], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert os.path.isdir(src)
    assert list(os.scandir(src)) == []


def test_p_state_changed_since_analyze_blocks_blind_overwrite(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("new", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()
    (dest / "movie.mp4").write_text("owned by 456 at analyze time", encoding="utf-8")

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))]),
        make_scene(456, "Original owner", [(999, str(dest / "movie.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    assert plan["items"][0]["conflict"]["owner_scene_id"] == "456"
    plan["items"][0]["overwrite"] = True

    # The world changes after Analyze: scene 456 is gone, a *different* scene now
    # owns that exact path (e.g. someone else moved a file there in the meantime).
    client.scenes.pop("456")
    client.scenes["789"] = make_scene(789, "A different scene", [(111, str(dest / "movie.mp4"))])

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    assert result["items"][0]["result"] == mover.RESULT_STATE_CHANGED
    assert os.path.exists(src / "movie.mp4")   # nothing was touched
    assert (dest / "movie.mp4").read_text(encoding="utf-8") == "owned by 456 at analyze time"
    assert "789" in client.scenes


def test_r_one_files_permission_error_does_not_stop_the_others(tmp_path, monkeypatch):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "a.mp4").write_text("a", encoding="utf-8")
    (src / "b.mp4").write_text("b", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    client = FakeClient([
        make_scene(1, "Scene A", [(1, str(src / "a.mp4")), (2, str(src / "b.mp4"))]),
    ], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))

    real_move = client.move_file

    def flaky_move(file_id, destination_folder):
        if str(file_id) == "1":
            raise PermissionError("denied")
        return real_move(file_id, destination_folder)

    monkeypatch.setattr(client, "move_file", flaky_move)

    result = mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    by_basename = {i["basename"]: i for i in result["items"]}
    assert by_basename["a.mp4"]["result"] == mover.RESULT_ERROR
    assert by_basename["b.mp4"]["result"] == mover.RESULT_MOVED
    assert (dest / "b.mp4").exists()


def test_s_cross_device_move_falls_back_to_copy_and_delete(tmp_path, monkeypatch):
    src = tmp_path / "a.txt"
    src.write_text("payload", encoding="utf-8")
    dst = tmp_path / "b.txt"

    def failing_rename(*_args, **_kwargs):
        raise OSError(18, "Invalid cross-device link")  # EXDEV

    monkeypatch.setattr(os, "rename", failing_rename)
    mover.safe_move(str(src), str(dst))

    assert not src.exists()
    assert dst.read_text(encoding="utf-8") == "payload"


def test_t_scene_metadata_and_association_survive_a_move(tmp_path):
    lib = _lib(tmp_path)
    src = lib / "src"
    src.mkdir()
    (src / "movie.mp4").write_text("x", encoding="utf-8")
    dest = lib / "dest"
    dest.mkdir()

    client = FakeClient([make_scene(1, "Scene A", [(1, str(src / "movie.mp4"))])], roots=[str(lib)])
    plan = _analyze(client, [str(lib)], ["1"], str(dest))
    mover.execute_move(client, [str(lib)], SETTINGS, ["1"], str(dest), plan["items"])

    scene = client.scenes["1"]
    assert scene["title"] == "Scene A"                       # untouched metadata
    assert scene["files"][0]["id"] == "1"                     # same File, same Scene
    assert os.path.normpath(scene["files"][0]["path"]) == os.path.normpath(str(dest / "movie.mp4"))


def test_op_scan_is_never_triggered_automatically(tmp_path):
    """MyMoover always updates File.path through moveFiles itself (see stash.py's
    module docstring), so there is no code path in mover.py where a scan is needed to
    reconcile a moved media file - `scan_paths` exists only for the manual, opt-in
    button described in requirement 18, wired at the ops layer (test_mm_ops.py)."""
    import inspect

    source = inspect.getsource(mover)
    assert "scan_paths" not in source
