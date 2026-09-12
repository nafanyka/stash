"""My Performer Body Calculator: the two tasks, run end to end against a fake Stash.

The calculation is tested next door. This is about everything around it - which
performers a task reads, what it writes, and in which order - because that is where the
difference between Add New and Full Update lives, and none of it can be checked by
reading the file.

The plugin is run the way Stash runs it: a copy of the plugin directory, a JSON fragment
on stdin, and `stashapi` replaced by a recording stand-in. Running the real entry point
also means a syntax error or a bad import in any of its modules fails here rather than
in someone's job queue.
"""

from __future__ import annotations

import io
import json
import os
import runpy
import shutil
import sys
import types

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO, "plugins", "MyPerformerBodyCalculator")
MANIFEST = os.path.join(PLUGIN_DIR, "MyPerformerBodyCalculator.yml")


def manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


class FakeStash:
    """Enough of StashInterface to run the plugin, and a record of what it was asked."""

    def __init__(self, performers):
        self.performers = performers
        self.bulk_updates = []
        self.created_tags = []
        self.destroyed = []
        self._next_id = 1000
        self.tags_by_alias = {}
        self.tags_by_name = {}
        self.tags_by_id = {}
        self.alias_updates = []

    # -- reads
    def find_performers(self, fragment=None, **kwargs):
        self.fragment = fragment
        return [dict(p) for p in self.performers]

    def find_tag(self, tag_in, create=False, fragment=None, on_multiple=None):
        if isinstance(tag_in, str):
            return self.tags_by_alias.get(tag_in)
        if isinstance(tag_in, int):                      # a re-read by id
            return self.tags_by_id.get(str(tag_in))
        name = tag_in["name"]
        if name in self.tags_by_name:
            # A tag of this name already exists - which is what happens on a library
            # that has run the original plugin. Stash returns it and adds no alias.
            return self.tags_by_name[name]
        self._next_id += 1
        record = {"id": str(self._next_id), "name": name,
                  "aliases": list(tag_in.get("aliases", []))}
        self.created_tags.append(tag_in)
        self.tags_by_name[name] = record
        self.tags_by_id[record["id"]] = record
        for alias in record["aliases"]:
            self.tags_by_alias[alias] = record
        return record

    def update_tag(self, tag_update):
        record = self.tags_by_id[str(tag_update["id"])]
        record["aliases"] = list(tag_update["aliases"])
        for alias in record["aliases"]:
            self.tags_by_alias[alias] = record
        self.alias_updates.append(tag_update)

    def find_tags(self, f=None, fragment=None, **kwargs):
        self.tag_filter = f
        return [{"id": t["id"]} for t in self.tags_by_alias.values()]

    # -- writes
    def update_performers(self, bulk_input):
        self.bulk_updates.append(bulk_input)
        return [{"id": i} for i in bulk_input.get("ids", [])]

    def destroy_tags(self, ids):
        self.destroyed.extend(ids)


def run_plugin(tmp_path, mode, performers, monkeypatch):
    """Run the real entry point with stashapi stubbed out, and hand back the fake."""
    work = tmp_path / "MyPerformerBodyCalculator"
    shutil.copytree(PLUGIN_DIR, work)

    fake = FakeStash(performers)

    stashapi = types.ModuleType("stashapi")
    log_mod = types.ModuleType("stashapi.log")

    class Handler:
        def __init__(self, *a, **k):
            pass

        def setLevel(self, *a):
            pass

        # logging.Handler protocol, minimally
        level = 0
        formatter = None

        def handle(self, record):
            pass

        def createLock(self):
            self.lock = None

        def acquire(self):
            pass

        def release(self):
            pass

        def setFormatter(self, *a):
            pass

        def close(self):
            pass

        def removeFilter(self, *a):
            pass

        def addFilter(self, *a):
            pass

        def filter(self, record):
            return True

    log_mod.StashLogHandler = Handler
    app_mod = types.ModuleType("stashapi.stashapp")
    app_mod.StashInterface = lambda connection: fake
    types_mod = types.ModuleType("stashapi.stash_types")

    class OnMultipleMatch:
        RETURN_NONE = "none"

    types_mod.OnMultipleMatch = OnMultipleMatch

    for name, module in [("stashapi", stashapi), ("stashapi.log", log_mod),
                         ("stashapi.stashapp", app_mod),
                         ("stashapi.stash_types", types_mod)]:
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"args": {"mode": mode}, "server_connection": {}})))
    monkeypatch.syspath_prepend(str(work))
    # The plugin's own modules must not be resolved from a previous run's copy.
    for name in ("config", "body_tags", "measurements", "performer_calculator"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    runpy.run_path(str(work / "my_performer_body_calculator.py"), run_name="__main__")
    return fake, work


def performer(pid, name="P", measurements="32D-28-34", processed=None, tags=()):
    custom = {"My Performer Body Calculator": processed} if processed else {}
    return {
        "id": str(pid), "name": name, "measurements": measurements,
        "weight": 55, "height_cm": 165, "ethnicity": "Caucasian", "gender": "FEMALE",
        "custom_fields": custom, "tags": [{"id": t} for t in tags],
    }


def marker_writes(fake):
    return [u for u in fake.bulk_updates if "custom_fields" in u]


def tag_adds(fake):
    return [u for u in fake.bulk_updates
            if (u.get("tag_ids") or {}).get("mode") == "ADD"]


def tag_removals(fake):
    return [u for u in fake.bulk_updates
            if (u.get("tag_ids") or {}).get("mode") == "REMOVE"]


class TestTheManifest:
    def test_the_filename_is_the_plugin_id(self):
        # Stash takes the id from the yml filename, and a clash with the original's id
        # is exactly what this fork has to avoid.
        assert os.path.basename(MANIFEST) == "MyPerformerBodyCalculator.yml"
        assert manifest()["name"] == "My Performer Body Calculator"

    def test_it_runs_its_own_entry_point(self):
        command = manifest()["exec"]
        assert command[0] == "python"
        assert command[1] == "{pluginDir}/my_performer_body_calculator.py"

    def test_the_two_calculation_tasks_are_declared(self):
        names = [t["name"] for t in manifest()["tasks"]]
        assert "Add New Performer Body Calculations" in names
        assert "Full Update Performer Body Calculations" in names

    def test_every_task_mode_is_one_the_entry_point_dispatches(self):
        with open(os.path.join(PLUGIN_DIR, "my_performer_body_calculator.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for task in manifest()["tasks"]:
            mode = task["defaultArgs"]["mode"]
            assert f'"{mode}"' in source, f"task {task['name']!r} has no handler"

    def test_every_python_file_compiles(self):
        # The entry point cannot be imported without a Stash, so this is the cheapest
        # guard against a typo reaching the job queue.
        for name in os.listdir(PLUGIN_DIR):
            if not name.endswith(".py"):
                continue
            path = os.path.join(PLUGIN_DIR, name)
            with open(path, encoding="utf-8") as handle:
                compile(handle.read(), path, "exec")


class TestAddNew:
    def test_it_skips_performers_that_carry_a_current_marker(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "add_new", [
            performer(1, "old", processed="v1"),
            performer(2, "new"),
        ], monkeypatch)

        added = tag_adds(fake)
        assert added, "nothing was tagged"
        for update in added:
            assert update["ids"] == ["2"], "an already-processed performer was touched"

    def test_it_never_removes_anything(self, tmp_path, monkeypatch):
        """The whole point of the task: old performers are left exactly as they are."""
        fake, _ = run_plugin(tmp_path, "add_new", [
            performer(1, "old", processed="v1", tags=["1001"]),
            performer(2, "new"),
        ], monkeypatch)
        assert tag_removals(fake) == []

    def test_it_marks_what_it_processed(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "add_new", [performer(2, "new")], monkeypatch)
        writes = marker_writes(fake)
        assert len(writes) == 1
        assert writes[0]["ids"] == ["2"]
        # `partial`, never `full`: other custom fields on the performer are not ours.
        assert writes[0]["custom_fields"] == {
            "partial": {"My Performer Body Calculator": "v1"}}

    def test_a_marker_from_an_older_version_does_not_count(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "add_new",
                             [performer(1, "stale", processed="v0")], monkeypatch)
        assert marker_writes(fake), "a performer calculated by an older version was skipped"

    @pytest.mark.parametrize("value", ["", None, "1", "yes"])
    def test_anything_that_is_not_the_current_marker_is_unprocessed(
            self, tmp_path, monkeypatch, value):
        fake, _ = run_plugin(tmp_path, "add_new",
                             [performer(1, "x", processed=value)], monkeypatch)
        assert marker_writes(fake)

    def test_nothing_to_do_writes_nothing(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "add_new",
                             [performer(1, "done", processed="v1")], monkeypatch)
        assert fake.bulk_updates == []


class TestMarkersFollowCompletion:
    """A marker means "read and dealt with", not "tagged"."""

    @pytest.mark.parametrize("measurements", ["", "utter nonsense", "88(E)-58-89"])
    def test_data_problems_still_count_as_processed(self, tmp_path, monkeypatch,
                                                    measurements):
        # Blank, unreadable, and fine: all three are answers about the performer, none
        # will change on a retry, and re-reading them on every run for ever is the
        # failure mode a marker exists to prevent.
        fake, _ = run_plugin(tmp_path, "add_new",
                             [performer(1, "x", measurements=measurements)], monkeypatch)
        assert marker_writes(fake), f"{measurements!r} left the performer unmarked"

    def test_a_failed_tag_write_leaves_the_performer_unmarked(self, tmp_path,
                                                              monkeypatch):
        """A technical failure has to be retried, so it must not be recorded as done."""
        original = FakeStash.update_performers

        def fail_on_tag_add(self, bulk_input):
            if (bulk_input.get("tag_ids") or {}).get("mode") == "ADD":
                raise RuntimeError("network went away")
            return original(self, bulk_input)

        monkeypatch.setattr(FakeStash, "update_performers", fail_on_tag_add)
        fake, _ = run_plugin(tmp_path, "add_new", [performer(1, "x")], monkeypatch)
        assert marker_writes(fake) == []


class TestFullUpdate:
    def test_it_removes_the_managed_tags_before_recalculating(self, tmp_path,
                                                              monkeypatch):
        fake, _ = run_plugin(tmp_path, "full_update", [
            performer(1, "a", processed="v1"),
            performer(2, "b"),
        ], monkeypatch)

        removals = tag_removals(fake)
        assert len(removals) == 1, "the reset must be one bulk call over every performer"
        assert sorted(removals[0]["ids"]) == ["1", "2"]
        assert fake.bulk_updates.index(removals[0]) == 0, "reset did not come first"

    def test_the_reset_removes_only_this_plugins_tags(self, tmp_path, monkeypatch):
        """`ids` is exactly the set collected while finding the managed tags.

        A tag the user added by hand, a tag from another plugin, and a tag the original
        Performer Body Calculator manages are all absent from it, so the removal cannot
        reach them.
        """
        fake, _ = run_plugin(tmp_path, "full_update", [performer(1, "a")], monkeypatch)
        removal = tag_removals(fake)[0]
        managed = {t["id"] for t in fake.tags_by_alias.values()}
        assert set(removal["tag_ids"]["ids"]) == managed
        assert removal["tag_ids"]["mode"] == "REMOVE"

    def test_every_managed_tag_carries_this_plugins_alias_and_marker(self, tmp_path,
                                                                     monkeypatch):
        fake, _ = run_plugin(tmp_path, "full_update", [performer(1, "a")], monkeypatch)
        assert fake.created_tags
        for created in fake.created_tags:
            assert any(a.startswith("MPBC:") for a in created["aliases"])
            assert created["description"].startswith("[Managed By: MPBC Plugin]")
            # The original's marks must not appear, or the two plugins would each treat
            # the other's tags as their own.
            assert not any(a.startswith("PBC:") for a in created["aliases"])

    def test_the_reset_clears_the_markers(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "full_update",
                             [performer(1, "a", processed="v1")], monkeypatch)
        removal = tag_removals(fake)[0]
        assert removal["custom_fields"] == {
            "remove": ["My Performer Body Calculator"]}

    def test_it_does_not_destroy_the_tags_themselves(self, tmp_path, monkeypatch):
        """Reset unassigns; `destroy_managed_tags` is the separate task that deletes."""
        fake, _ = run_plugin(tmp_path, "full_update", [performer(1, "a")], monkeypatch)
        assert fake.destroyed == []

    def test_it_recalculates_everyone_including_the_already_processed(self, tmp_path,
                                                                      monkeypatch):
        fake, _ = run_plugin(tmp_path, "full_update", [
            performer(1, "a", processed="v1"),
            performer(2, "b"),
        ], monkeypatch)
        tagged = {pid for update in tag_adds(fake) for pid in update["ids"]}
        assert tagged == {"1", "2"}

    def test_it_marks_everyone_afterwards(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "full_update", [
            performer(1, "a"), performer(2, "b"),
        ], monkeypatch)
        writes = [u for u in marker_writes(fake) if "partial" in u["custom_fields"]]
        assert len(writes) == 1
        assert sorted(writes[0]["ids"]) == ["1", "2"]

    def test_the_original_mode_name_still_works(self, tmp_path, monkeypatch):
        # `run_calculator` is what the original's task sent; a user who copied a task
        # definition across should not get a silent no-op.
        fake, _ = run_plugin(tmp_path, "run_calculator", [performer(1, "a")], monkeypatch)
        assert tag_removals(fake)


class TestDestroyManagedTags:
    def test_it_matches_only_this_plugins_marker(self, tmp_path, monkeypatch):
        fake, _ = run_plugin(tmp_path, "destroy_managed_tags", [], monkeypatch)
        assert "MPBC Plugin" in fake.tag_filter["description"]["value"]
        assert fake.tag_filter["description"]["modifier"] == "MATCHES_REGEX"


class TestConfigBootstrap:
    def test_a_missing_config_is_created_from_the_example(self, tmp_path, monkeypatch):
        """stashapp-tools 0.2.59 has no `ensure_plugin_config_file`, which the original
        calls - so on a fresh install it raises AttributeError before doing anything."""
        fake, work = run_plugin(tmp_path, "add_new", [performer(1, "a")], monkeypatch)
        assert (work / "config.py").exists()


class TestTagsTheOriginalPluginAlreadyMade:
    """A library that has run the original has every tag under the right name already.

    `find_tag(create_input, create=True)` returns such a tag and adds nothing to it, so
    without a second step this plugin's alias never lands, the alias lookup misses on
    every run, and renaming the tag in Stash would lose it for good.
    """

    def test_the_alias_is_added_to_a_tag_that_already_existed(self, tmp_path,
                                                              monkeypatch):
        original = FakeStash.__init__

        def preloaded(self, performers):
            original(self, performers)
            # As the original plugin would have left it: right name, its alias, not ours.
            existing = {"id": "77", "name": "BodyShape.HOURGLASS",
                        "aliases": ["PBC:BodyShape.HOURGLASS"]}
            self.tags_by_name[existing["name"]] = existing
            self.tags_by_id[existing["id"]] = existing
            self.tags_by_alias[existing["aliases"][0]] = existing

        monkeypatch.setattr(FakeStash, "__init__", preloaded)
        fake, _ = run_plugin(tmp_path, "add_new", [performer(1, "a")], monkeypatch)

        assert fake.tags_by_id["77"]["aliases"] == [
            "PBC:BodyShape.HOURGLASS", "MPBC:BodyShape.HOURGLASS"]
        assert fake.alias_updates, "the alias was never claimed"

    def test_the_original_plugins_alias_is_left_in_place(self, tmp_path, monkeypatch):
        # Removing it would take the tag away from the plugin that made it.
        original = FakeStash.__init__

        def preloaded(self, performers):
            original(self, performers)
            existing = {"id": "77", "name": "BodyShape.HOURGLASS",
                        "aliases": ["PBC:BodyShape.HOURGLASS"]}
            self.tags_by_name[existing["name"]] = existing
            self.tags_by_id[existing["id"]] = existing

        monkeypatch.setattr(FakeStash, "__init__", preloaded)
        fake, _ = run_plugin(tmp_path, "add_new", [performer(1, "a")], monkeypatch)
        assert "PBC:BodyShape.HOURGLASS" in fake.tags_by_id["77"]["aliases"]
