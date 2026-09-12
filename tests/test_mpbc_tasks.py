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

    def find_performer(self, performer, create=False, fragment=None, on_multiple=None):
        for one in self.performers:
            if str(one["id"]) == str(performer):
                return dict(one)
        return None

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
        seen, out = set(), []
        for tag in list(self.tags_by_alias.values()) + list(self.tags_by_name.values()):
            if tag["id"] in seen:
                continue
            seen.add(tag["id"])
            out.append({"id": tag["id"]})
        return out

    # -- writes
    def update_performers(self, bulk_input):
        self.bulk_updates.append(bulk_input)
        return [{"id": i} for i in bulk_input.get("ids", [])]

    def destroy_tags(self, ids):
        self.destroyed.extend(ids)


def run_plugin(tmp_path, mode, performers, monkeypatch, hook_context=None,
               fake=None):
    """Run the real entry point with stashapi stubbed out, and hand back the fake."""
    work = tmp_path / "MyPerformerBodyCalculator"
    if not work.exists():
        shutil.copytree(PLUGIN_DIR, work)

    fake = fake or FakeStash(performers)

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

    args = {"mode": mode}
    if hook_context is not None:
        args["hookContext"] = hook_context
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"args": args, "server_connection": {}})))
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


def hook(pid, fields, trigger="Performer.Update.Post"):
    return {"id": str(pid), "type": trigger, "input": {}, "inputFields": list(fields)}


class TestTheUpdateHook:
    """Recalculating one performer when Stash says they changed."""

    def run(self, tmp_path, monkeypatch, performers, context, fake=None):
        return run_plugin(tmp_path, "recalculate_performer", performers, monkeypatch,
                          hook_context=context, fake=fake)

    def test_a_measurements_change_recalculates_that_performer(self, tmp_path,
                                                               monkeypatch):
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")],
                           hook(1, ["measurements"]))
        assert tag_adds(fake), "nothing was tagged"
        for update in tag_adds(fake):
            assert update["ids"] == ["1"]

    @pytest.mark.parametrize("field", ["measurements", "height_cm", "weight",
                                       "ethnicity", "gender"])
    def test_every_field_the_calculation_reads_triggers_it(self, tmp_path, monkeypatch,
                                                           field):
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")], hook(1, [field]))
        assert fake.bulk_updates, f"{field} did not trigger a recalculation"

    def test_only_the_one_performer_is_touched(self, tmp_path, monkeypatch):
        fake, _ = self.run(tmp_path, monkeypatch,
                           [performer(1, "a"), performer(2, "b", processed="v1")],
                           hook(1, ["measurements"]))
        for update in fake.bulk_updates:
            assert update["ids"] == ["1"], "the hook reached another performer"

    def test_a_new_performer_is_always_calculated(self, tmp_path, monkeypatch):
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")],
                           hook(1, [], trigger="Performer.Create.Post"))
        assert tag_adds(fake)

    def test_it_marks_the_performer_as_processed(self, tmp_path, monkeypatch):
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")],
                           hook(1, ["measurements"]))
        assert marker_writes(fake)

    def test_a_performer_deleted_before_the_hook_ran_is_not_an_error(self, tmp_path,
                                                                     monkeypatch):
        fake, _ = self.run(tmp_path, monkeypatch, [], hook(99, ["measurements"]))
        assert fake.bulk_updates == []


class TestTheHookDoesNotAnswerItself:
    """The loop that makes update hooks dangerous.

    `bulkPerformerUpdate` fires `Performer.Update.Post` once per performer it touched -
    so a plugin that recalculates on any update and then writes tags calls itself for
    ever. Two things stop it, and both are tested here.
    """

    def run(self, tmp_path, monkeypatch, performers, context):
        return run_plugin(tmp_path, "recalculate_performer", performers, monkeypatch,
                          hook_context=context)

    @pytest.mark.parametrize("fields", [
        ["tag_ids"],                  # what this plugin writes when it adds tags
        ["custom_fields"],            # what it writes when it marks a performer
        ["ids", "tag_ids"],           # a bulk update of its own
        ["name"],                     # someone else's edit that changes no measurement
        ["url", "twitter"],
    ])
    def test_an_update_touching_nothing_the_calculation_reads_is_ignored(
            self, tmp_path, monkeypatch, fields):
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")], hook(1, fields))
        assert fake.bulk_updates == [], f"{fields} caused a write, which would loop"

    def test_an_update_with_no_field_list_is_skipped_rather_than_guessed_at(
            self, tmp_path, monkeypatch):
        # Without the list there is no way to tell a user's edit from this plugin's own
        # write, and guessing wrong is an endless loop.
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")], hook(1, []))
        assert fake.bulk_updates == []

    def test_a_recalculation_that_changes_nothing_writes_nothing(self, tmp_path,
                                                                 monkeypatch):
        """The second guard: even a hook that did fire settles after one pass.

        The performer is given the tags the calculation produces and a current marker,
        so there is no difference to write - and therefore no update to fire the hook
        again.
        """
        # First pass: let the plugin tag a performer and record what it assigned.
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")],
                           hook(1, ["measurements"]))
        assigned = sorted({tid for u in tag_adds(fake) for tid in u["tag_ids"]["ids"]})
        assert assigned

        # Second pass: same performer, now carrying those tags and the marker.
        settled = performer(1, "a", processed="v1", tags=assigned)
        again = FakeStash([settled])
        again.tags_by_alias = fake.tags_by_alias
        again.tags_by_name = fake.tags_by_name
        again.tags_by_id = fake.tags_by_id
        fake2, _ = run_plugin(tmp_path / "second", "recalculate_performer", [settled],
                              monkeypatch, hook_context=hook(1, ["measurements"]),
                              fake=again)
        assert fake2.bulk_updates == [], "a settled performer was written to again"

    def test_it_can_be_turned_off(self, tmp_path, monkeypatch):
        work = tmp_path / "MyPerformerBodyCalculator"
        shutil.copytree(PLUGIN_DIR, work)
        (work / "config.py").write_text(
            (work / "example_config.py").read_text(encoding="utf-8")
            + "\nRECALCULATE_ON_UPDATE = False\n", encoding="utf-8")
        fake, _ = self.run(tmp_path, monkeypatch, [performer(1, "a")],
                           hook(1, ["measurements"]))
        assert fake.bulk_updates == []


class TestTheHookIsDeclared:
    def test_the_manifest_subscribes_to_both_performer_triggers(self):
        hooks = manifest()["hooks"]
        assert len(hooks) == 1
        assert set(hooks[0]["triggeredBy"]) == {"Performer.Create.Post",
                                                "Performer.Update.Post"}

    def test_its_mode_is_one_the_entry_point_dispatches(self):
        with open(os.path.join(PLUGIN_DIR, "my_performer_body_calculator.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        for entry in manifest()["hooks"]:
            assert f'"{entry["defaultArgs"]["mode"]}"' in source

    def test_it_does_not_subscribe_to_anything_it_cannot_act_on(self):
        # Destroy in particular: there is nothing to recalculate for a performer that no
        # longer exists, and the tags go with it.
        for entry in manifest()["hooks"]:
            assert not any("Destroy" in t for t in entry["triggeredBy"])
