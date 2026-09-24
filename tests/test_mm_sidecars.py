"""sidecars.py: pattern matching, incl. multi-axis funscripts (requirement K), and
that discovery never leaves the media file's own directory."""

from __future__ import annotations

import mm_common  # noqa: F401

from mymoover import sidecars
from mymoover.settings import DEFAULT_SIDECAR_PATTERNS


def _touch(directory, *names):
    for name in names:
        (directory / name).write_text("x", encoding="utf-8")


def test_finds_plain_and_multi_axis_funscripts(tmp_path):
    _touch(tmp_path, "video.mp4", "video.funscript", "video.L0.funscript",
           "video.L1.funscript", "video.R2.funscript", "video.srt")
    cache = sidecars.DirCache()
    found = sidecars.find_sidecars(str(tmp_path), "video.mp4", DEFAULT_SIDECAR_PATTERNS, cache)
    assert set(found) == {
        "video.funscript", "video.L0.funscript", "video.L1.funscript",
        "video.R2.funscript", "video.srt",
    }


def test_does_not_match_a_different_basename(tmp_path):
    _touch(tmp_path, "video.mp4", "other.funscript", "video2.funscript")
    cache = sidecars.DirCache()
    found = sidecars.find_sidecars(str(tmp_path), "video.mp4", DEFAULT_SIDECAR_PATTERNS, cache)
    assert found == []


def test_never_recurses_into_subdirectories(tmp_path):
    _touch(tmp_path, "video.mp4")
    sub = tmp_path / "sub"
    sub.mkdir()
    _touch(sub, "video.funscript")
    cache = sidecars.DirCache()
    found = sidecars.find_sidecars(str(tmp_path), "video.mp4", DEFAULT_SIDECAR_PATTERNS, cache)
    assert found == []


def test_dir_cache_only_scans_once_per_directory(tmp_path, monkeypatch):
    _touch(tmp_path, "a.mp4", "a.srt", "b.mp4", "b.srt")
    cache = sidecars.DirCache()
    calls = []
    real_scandir = __import__("os").scandir

    def counting_scandir(path):
        calls.append(path)
        return real_scandir(path)

    monkeypatch.setattr("os.scandir", counting_scandir)
    sidecars.find_sidecars(str(tmp_path), "a.mp4", DEFAULT_SIDECAR_PATTERNS, cache)
    sidecars.find_sidecars(str(tmp_path), "b.mp4", DEFAULT_SIDECAR_PATTERNS, cache)
    assert len(calls) == 1


def test_custom_pattern_setting_is_respected(tmp_path):
    _touch(tmp_path, "video.mkv", "video.nfo", "video.srt")
    cache = sidecars.DirCache()
    found = sidecars.find_sidecars(str(tmp_path), "video.mkv", [".nfo"], cache)
    assert found == ["video.nfo"]
