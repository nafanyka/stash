"""PerformerOrganized: the manifest, the files it names, and the promises it makes.

This plugin has no Python, so there is nothing here to unit-test the way the others are
tested. What can still go wrong silently is the wiring - a manifest naming a file that
was renamed, scripts loaded in the wrong order, a documented public function that no
longer exists - and every one of those fails at runtime in the browser, where nobody is
watching a test suite. So that is what these check.
"""

from __future__ import annotations

import os
import re

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, os.pardir, "plugins", "PerformerOrganized")
MANIFEST = os.path.join(PLUGIN_DIR, "PerformerOrganized.yml")


def manifest():
    with open(MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def source(*parts):
    with open(os.path.join(PLUGIN_DIR, *parts), encoding="utf-8") as handle:
        return handle.read()


def ui_scripts():
    return manifest()["ui"]["javascript"]


class TestManifest:
    def test_the_filename_is_the_plugin_id(self):
        # Stash derives the plugin id from the yml filename, not from `name`, and the
        # id is what a duplicate install collides on.
        assert os.path.basename(MANIFEST) == "PerformerOrganized.yml"

    def test_it_declares_no_backend(self):
        # There is no Python here on purpose: every write is a mutation the browser
        # already has a session for. An `exec` line would make Stash expect one.
        assert "exec" not in manifest()
        assert "interface" not in manifest()

    def test_every_file_the_manifest_names_exists(self):
        entries = list(ui_scripts()) + list(manifest()["ui"]["css"])
        missing = [one for one in entries
                   if not os.path.isfile(os.path.join(PLUGIN_DIR, one))]
        assert missing == []

    def test_the_scripts_are_loaded_in_dependency_order(self):
        # Classic scripts, no modules: components.js reads the namespace api.js makes,
        # and patches.js reads the components. Reordering these silently loads nothing.
        assert ui_scripts() == ["ui/api.js", "ui/components.js", "ui/patches.js"]

    def test_it_has_a_version_and_a_description(self):
        # Both go into the source index; a package without them installs as a blank row.
        parsed = manifest()
        assert re.match(r"^\d+\.\d+\.\d+$", str(parsed["version"]))
        assert len(parsed["description"]) > 40


class TestTheFlag:
    """The stored shape is a contract with the user's library, not an implementation
    detail: it is what their saved filters select on."""

    def test_the_field_is_the_one_the_readme_documents(self):
        assert 'var FIELD = "organized";' in source("ui", "api.js")
        assert "custom_fields.organized" in source("README.md")

    def test_a_write_never_replaces_the_whole_map(self):
        # `full` would wipe every other custom field on the performer. Only `partial`
        # and `remove` may be used.
        text = source("ui", "api.js")
        assert "full:" not in text
        assert "partial: partial" in text
        assert "remove: [FIELD]" in text

    def test_unorganized_removes_the_key_rather_than_writing_false(self):
        # A performer nobody touched and one turned off have to read the same way, or
        # the filter contradicts itself.
        assert "return { remove: [FIELD] };" in source("ui", "api.js")


class TestFiltering:
    def test_it_filters_by_null_not_by_equality(self):
        # Stash joins IS_NULL/NOT_EQUALS with a LEFT JOIN and EQUALS with an inner one,
        # so only the null form includes performers that have no custom fields at all.
        text = source("ui", "api.js")
        assert '"NOT_NULL"' in text and '"IS_NULL"' in text
        assert '"EQUALS"' not in text

    def test_the_url_is_built_by_stashs_own_model(self):
        # Hand-encoding the criterion query string would break the next time Stash
        # changes it. clone/makeCriterion/replaceCriteria/makeQueryParameters are the
        # model's own methods.
        text = source("ui", "api.js")
        for method in ("filter.clone()", "makeCriterion(CRITERION)",
                       "replaceCriteria(CRITERION", "makeQueryParameters()"):
            assert method in text

    def test_other_custom_field_criteria_are_carried_over(self):
        # Switching Organized must not throw away a filter the user set on some other
        # custom field.
        assert "entry.field !== FIELD" in source("ui", "api.js")


class TestPublicApi:
    """`window.PerformerOrganized` is what the README tells other plugins to call."""

    NAMES = ("isOrganized", "isPerformerOrganized", "setPerformerOrganized",
             "setPerformersOrganized")

    def test_every_documented_helper_is_exported(self):
        text = source("ui", "api.js")
        exported = re.search(r"window\.PerformerOrganized = \{(.+?)\n  \};", text,
                             re.DOTALL)
        assert exported is not None
        for name in self.NAMES:
            assert name + ":" in exported.group(1)

    def test_the_readme_documents_exactly_those(self):
        text = source("README.md")
        for name in self.NAMES:
            assert "PerformerOrganized." + name in text


class TestAttachment:
    POINTS = {"PerformerDetailsPanel", "CompressedPerformerDetailsPanel",
              "PerformerCard.Overlays", "PerformerList"}

    def test_it_only_uses_published_patch_points(self):
        # Anything not on Stash's published list is an internal, and patching an
        # internal is what breaks on an update.
        used = set(re.findall(r'attach\("([^"]+)"', source("ui", "patches.js")))
        assert used == self.POINTS

    def test_the_patch_result_is_read_as_the_last_argument(self):
        """React error #31, and why the handlers do not name their arguments.

        Stash calls an `after` patch as `afterFn.apply(ctx, args.concat(result))`, and
        `args` is what React passed the component - which is `(props, context)`, not
        `(props)`. A handler written `function (props, result)` therefore receives the
        empty legacy-context object as `result`, and returning it as a child is
        "Objects are not valid as a React child (found: object with keys {})".
        """
        text = source("ui", "patches.js")
        # One place calls PluginApi.patch at all, and it names no argument it does not
        # count first. (The prose above quotes the wrong form on purpose, so this
        # cannot be a substring check on the whole file.)
        assert text.count("api.patch.") == 1
        assert "api.patch.after(name, function () {" in text
        assert "var result = arguments[arguments.length - 1];" in text

    def test_a_control_that_throws_does_not_take_the_page_with_it(self):
        assert "return result;" in source("ui", "patches.js")
        assert "catch (error)" in source("ui", "patches.js")

    def test_nothing_touches_the_dom(self):
        # A plugin that inserts nodes by hand has to clean them up on every SPA
        # navigation, and duplicates the moment it forgets. These are React components
        # handed to Stash, so there is nothing to duplicate.
        for name in ("api.js", "components.js", "patches.js"):
            text = source("ui", name)
            for banned in ("document.querySelector", "document.getElementById",
                           "appendChild", "innerHTML", "MutationObserver"):
                assert banned not in text, "%s uses %s" % (name, banned)

    def test_loading_twice_is_harmless(self):
        assert "if (window.PerformerOrganized)" in source("ui", "api.js")

    def test_a_failed_write_is_rolled_back_and_reported(self):
        text = source("ui", "components.js")
        assert "pending[1](null);" in text          # the rollback
        assert "toast.error(error)" in text         # and the user hears about it


class TestNothingSetsItButTheUser:
    """The flag is a manual workflow marker (requirement 26).

    The two mechanisms by which a Stash plugin can change something on its own are a
    hook - which fires on Performer.Update.Post and friends - and a task. This plugin
    declares neither, so there is no code path that can set the flag without somebody
    pressing something.
    """

    def test_it_registers_no_hooks(self):
        assert "hooks" not in manifest()

    def test_it_declares_no_tasks(self):
        assert "tasks" not in manifest()

    def test_the_only_writers_sit_behind_a_click(self):
        # Each mutation is called from exactly one place, and that place is a click
        # handler. A second call site is where an automatic rule would appear.
        text = source("ui", "components.js")
        assert text.count("PO.setPerformerOrganized(") == 1
        assert text.count("PO.setPerformersOrganized(") == 1
        assert "function toggle(event)" in text      # the card and page control
        assert "function apply(organized)" in text   # the bulk buttons
