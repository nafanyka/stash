"""validate.py: library-root containment and new-folder-name safety.

Requirement N (path traversal in New Folder) and O (destination outside every
configured root) live here.
"""

from __future__ import annotations

import os

import mm_common  # noqa: F401  (adds the plugin + stash_common to sys.path)
import pytest

from mymoover import validate


def test_within_roots_accepts_a_real_subfolder(tmp_path):
    root = tmp_path / "library"
    sub = root / "studio-a"
    sub.mkdir(parents=True)
    real = validate.require_within_roots(str(sub), [str(root)], "destination")
    assert real == os.path.realpath(str(sub))


def test_destination_outside_every_root_is_rejected(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(validate.ValidationError):
        validate.require_within_roots(str(outside), [str(root)], "destination")


def test_no_configured_roots_is_rejected(tmp_path):
    with pytest.raises(validate.ValidationError):
        validate.require_within_roots(str(tmp_path), [], "destination")


@pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "../escape", "", "   ", "a\x00b"])
def test_new_folder_name_rejects_traversal_and_junk(name):
    with pytest.raises(validate.ValidationError):
        validate.validate_new_folder_name(name)


def test_new_folder_name_accepts_a_plain_name():
    assert validate.validate_new_folder_name(" 2026 ") == "2026"


def test_new_folder_name_rejects_absolute_path(tmp_path):
    absolute = str(tmp_path / "x")
    with pytest.raises(validate.ValidationError):
        validate.validate_new_folder_name(absolute)
