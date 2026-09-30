"""Tests for stashdbTagSync plugin logic."""

import pytest

# These test modules import the plugin's own code, which imports `stashapi`. That is a
# real dependency of the plugin, not something to work around - but it is not installed
# for every checkout, and a plain `ModuleNotFoundError` at import time is a *collection*
# error, which stops pytest before any other suite in the repository has run. Skipping
# is the difference between "these three did not run" and "nothing ran".
pytest.importorskip("stashapi")

from unittest.mock import Mock

from stashdbTagSync import find_stashdb_box


class TestFindStashdbBox:
    def test_returns_key_and_endpoint_for_stashdb_box(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = [
            {"name": "StashDB", "endpoint": "https://stashdb.org/graphql", "api_key": "sdb-key"},
        ]

        result = find_stashdb_box(stash)

        assert result == ("sdb-key", "https://stashdb.org/graphql")

    def test_ignores_non_stashdb_boxes(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = [
            {"name": "FansDB", "endpoint": "https://fansdb.cc/graphql", "api_key": "fdb-key"},
            {"name": "StashDB", "endpoint": "https://stashdb.org/graphql", "api_key": "sdb-key"},
        ]

        result = find_stashdb_box(stash)

        assert result == ("sdb-key", "https://stashdb.org/graphql")

    def test_matches_endpoint_case_insensitively(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = [
            {"name": "StashDB", "endpoint": "https://StashDB.org/graphql", "api_key": "sdb-key"},
        ]

        result = find_stashdb_box(stash)

        assert result == ("sdb-key", "https://StashDB.org/graphql")

    def test_returns_empty_strings_when_no_boxes_configured(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = []

        result = find_stashdb_box(stash)

        assert result == ("", "")

    def test_returns_empty_strings_when_no_stashdb_box_present(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = [
            {"name": "FansDB", "endpoint": "https://fansdb.cc/graphql", "api_key": "fdb-key"},
        ]

        result = find_stashdb_box(stash)

        assert result == ("", "")

    def test_returns_empty_strings_when_box_missing_api_key(self):
        stash = Mock()
        stash.get_stashbox_connections.return_value = [
            {"name": "StashDB", "endpoint": "https://stashdb.org/graphql", "api_key": ""},
        ]

        result = find_stashdb_box(stash)

        assert result == ("", "")

    def test_returns_empty_strings_when_fetch_raises(self):
        stash = Mock()
        stash.get_stashbox_connections.side_effect = Exception("connection refused")

        result = find_stashdb_box(stash)

        assert result == ("", "")
