"""Make the FastDiscovery plugin importable, and provide the fixtures its tests share.

The plugin is not an installed package - Stash runs it from its own directory - so the
tests put that directory on the path the same way the entry point does.

FastDiscovery's fake server lives in fd_common.py rather than here, because it is
substantial enough to be worth reading on its own.
"""

from __future__ import annotations

import pytest

from fd_common import FD_PLUGIN_DIR  # noqa: F401  (adds it to sys.path)
from fastdiscovery import settings as fd_settings_module
from fastdiscovery.db import repo as fd_repo_module


@pytest.fixture
def fd_config():
    """Default configuration, as an untouched Stash install would produce."""
    return fd_settings_module.parse({})


@pytest.fixture
def fd_repo(tmp_path):
    """A fresh migrated database per test."""
    store = fd_repo_module.Repo.open(str(tmp_path / "fastdiscovery.sqlite"))
    yield store
    store.close()


@pytest.fixture
def fd_scene():
    """A scene shaped as `findScene` returns it."""
    return {
        "id": "295",
        "title": "Old title",
        "code": None,
        "details": None,
        "director": None,
        "date": None,
        "rating100": None,
        "urls": ["https://sitea.com/scene/1"],
        "organized": False,
        "updated_at": "2026-01-01T00:00:00Z",
        "paths": {"screenshot": "http://localhost:9999/scene/295/screenshot"},
        "studio": None,
        "performers": [{"id": "9", "name": "Angela White"}],
        "tags": [{"id": "3", "name": "Existing Tag"}],
        "groups": [],
        "stash_ids": [],
        "files": [{
            "id": "100", "path": "/media/example_scene_1080p.mp4",
            "basename": "example_scene_1080p.mp4", "size": 1234567,
            "duration": 1800.0, "width": 1920, "height": 1080,
            "fingerprints": [{"type": "oshash", "value": "abc123"},
                             {"type": "phash", "value": "def456"}],
        }],
    }
