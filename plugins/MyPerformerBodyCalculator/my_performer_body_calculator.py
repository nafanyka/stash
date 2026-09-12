"""My Performer Body Calculator - entry point.

From stg-annon's Performer Body Calculator (GPL-3.0, see LICENSE). The tag mechanism is
his and is untouched: tags are found by a prefixed alias, created from the enum if they
are missing, and the ids collected while doing so are what a full update later removes.
That is the part that makes the calculator safe to re-run, and there was no reason to
replace it.

What is new is *when* the work happens. The original had one mode, which began by
stripping every managed tag off every performer and then recalculated the library. That
is right once, and wasteful every time after: a library that grows by twenty performers
a week does not need the other three thousand rebuilt to pick them up. So there are two
tasks over one pipeline:

    Add New    - performers this calculator has not seen, left alone by everything else
    Full Update - the original's behaviour: reset, then recalculate the whole library

"Has not seen" is recorded on the performer itself, in a custom field, so it survives
restarts, reinstalls and this plugin being uninstalled and put back.
"""

import sys, json, shutil
from pathlib import Path
from collections import defaultdict

import logging as log

try:
    from stashapi.log import StashLogHandler
    from stashapi.stashapp import StashInterface
    from stashapi.stash_types import OnMultipleMatch
except ModuleNotFoundError:
    print("You need to install stashapi. (https://pypi.org/project/stashapi/)", file=sys.stderr)
    print("If you have pip (normally installed with python), run this command in a terminal (cmd): 'pip install stashapp-tools'", file=sys.stderr)
    sys.exit()

fragment = json.loads(sys.stdin.read())
mode = fragment['args']['mode']
stash = StashInterface(fragment["server_connection"])

try:
    import config
except ModuleNotFoundError:
    # The original called `stash.ensure_plugin_config_file`, which current releases of
    # stashapp-tools (0.2.59) no longer have - so on a fresh install, where config.py
    # does not exist yet, that line raises AttributeError before the plugin can run at
    # all. Copying the example is what that helper did, and doing it here works on every
    # version.
    _here = Path(__file__).resolve().parent
    if not (_here / "config.py").exists():
        shutil.copyfile(_here / "example_config.py", _here / "config.py")
    import config

from performer_calculator import *
from body_tags import *

log.basicConfig(format="%(message)s", handlers=[StashLogHandler()], level=config.log_level)

PLUGIN_NAME = "My Performer Body Calculator"

# The processed marker.
#
# A version rather than a boolean, so that changing how a body is calculated can make
# every performer eligible again without a full update: bump this, and last season's
# marker stops matching. Any *current* marker means Add New skips the performer.
CALCULATOR_VERSION = "1"
MARKER_FIELD = PLUGIN_NAME
MARKER_VALUE = f"v{CALCULATOR_VERSION}"

# Stash's performer custom fields are a free-form map - `Performer.custom_fields: Map!` -
# with no schema to register a field in and nothing to create before writing one. The
# first write brings the field into existence for that performer, and `CustomFieldsInput`
# has a `partial` arm that touches only the keys named, so writing the marker cannot
# disturb any other custom field the user keeps on their performers.


def main():
    if mode in ("add_new",):
        add_new()
    elif mode in ("full_update", "run_calculator"):   # the original's mode name still works
        full_update()
    elif mode == "destroy_managed_tags":
        destroy_managed_tags()
    elif mode == "recalculate_performer":
        recalculate_performer()
    else:
        log.error(f"unknown mode '{mode}'")


def destroy_managed_tags():
    """Delete this plugin's tags from Stash entirely.

    Unchanged from the original except for which marker it matches. Note what this is
    *not*: it destroys the tag definitions, so it is not how a full update resets - that
    takes the tags off performers and leaves the tags themselves alone.
    """
    tags = sorted(managed_tag_ids())
    log.info(f"Deleting {len(tags)} tags...")
    stash.destroy_tags(tags)


# ----------------------------------------------------------------- shared pipeline

def init_managed_tags():
    """Find or create every managed tag, and return the ids. The original's mechanism."""
    all_tag_ids = []
    log.info("Finding Tags in Stash...")
    for enum_class in get_tag_classes():
        enumtag_stash_init(enum_class, all_tag_ids)
    return all_tag_ids


def enumtag_stash_init(enum_class, tag_id_list=[]):
    """Unchanged from the original apart from the alias prefix, which names this plugin.

    The alias is the whole point of it: a user is free to rename "BodyShape.HOURGLASS"
    to something they would rather read, or merge it into a tag they already had, and
    the plugin still finds it next run because the alias came along.
    """
    for enum in enum_class:
        if not isinstance(enum, config.TAGS_TO_USE):
            continue
        tag_id_list.append(resolve_tag(enum))
    return tag_id_list


def resolve_tag(enum):
    """One enum's tag in Stash, found by this plugin's alias or created.

    The body of the original's loop, lifted out so the update hook can resolve the eight
    or nine tags one performer needs without walking all sixty.
    """
    tag_alias_id = f"{TAG_ALIAS_PREFIX}{enum}"
    stash_tag = stash.find_tag(tag_alias_id, on_multiple=OnMultipleMatch.RETURN_NONE)
    if stash_tag:
        enum.tag_id = stash_tag["id"]
    else:
        tag_create_input = enum.value.tag_create_input(str(enum), tag_alias_id)
        enum.tag_id = stash.find_tag(tag_create_input, create=True)["id"]
        claim_alias(enum.tag_id, tag_alias_id)
    return enum.tag_id


def claim_alias(tag_id, tag_alias_id):
    """Make sure the tag we just resolved actually carries our alias.

    `find_tag(create_input, create=True)` creates the tag when nothing matches - and then
    the alias is already on it - but when a tag of that *name* exists it returns that one
    and adds nothing. Which is the normal case here: the tag names are the original
    Performer Body Calculator's, so on a library that has ever run it every tag exists
    under the right name with the wrong alias.

    Without this the alias lookup misses for ever and every run pays a second query per
    tag; worse, renaming such a tag in Stash would lose it, because the alias is the only
    thing that survives a rename.
    """
    current = stash.find_tag(int(tag_id), fragment="id aliases")
    if not current or "aliases" not in current:
        # An older stashapi that does not return aliases. Writing a list we cannot see
        # the whole of would delete the aliases already there, so do nothing.
        return
    if tag_alias_id in (current["aliases"] or []):
        return
    log.debug(f"adding alias {tag_alias_id} to existing tag {tag_id}")
    stash.update_tag({"id": tag_id, "aliases": (current["aliases"] or []) + [tag_alias_id]})


class Tally:
    """What a run did, for the one line it prints at the end."""

    def __init__(self):
        self.total = 0
        self.already_processed = 0
        self.processed = 0
        self.missing_measurements = 0
        self.parse_errors = 0
        self.no_bodyshape = 0
        self.errors = 0
        self.markers_cleared = 0
        self.assignments_removed = 0

    def count_status(self, status):
        if status == STATUS_MISSING:
            self.missing_measurements += 1
        elif status == STATUS_UNPARSED:
            self.parse_errors += 1
        elif status == STATUS_NO_SHAPE:
            self.no_bodyshape += 1


def process_performers(performers, tally):
    """The calculation, for however many performers it is handed.

    Both tasks come through here, so neither can drift from the other. It returns the
    tag updates to apply and the ids of the performers that got all the way through -
    the ones a marker may be set on.

    A performer whose measurements are blank, unreadable, or of a shape no category
    covers still counts as done. Those are answers about the data, they will not change
    on a retry, and leaving such a performer unmarked would mean re-reading it on every
    Add New for as long as the library exists. Only an exception - which means the
    performer was not successfully *read* - leaves it unmarked.
    """
    tag_updates = defaultdict(list)
    completed = []

    log.info(f"Parsing {len(performers)} performer(s)...")
    for p in performers:
        p_id = f"{p['name']} ({p['id']})"
        try:
            performer = StashPerformer(p)
            performer.get_tag_updates(tag_updates)
            tally.count_status(performer.status)
            tally.processed += 1
            completed.append(p["id"])
        except DebugException as e:
            log.debug(f"{p_id}: {e}")
            tally.errors += 1
        except WarningException as e:
            log.warning(f"{p_id}: {e}")
            tally.errors += 1
        except Exception as e:
            log.error(f"{p_id}: {e}")
            tally.errors += 1

    return tag_updates, completed


def apply_tag_updates(tag_updates, tally, completed):
    """Add the calculated tags, one bulk call per tag. The original's approach.

    A bulk call that fails takes its performers out of `completed`, so no marker is set
    for a performer whose tags did not make it into Stash and the next Add New tries
    again. Adding the same tag twice is harmless - the mode is ADD - so a retry cannot
    do damage.
    """
    failed = set()
    for enum, performer_ids in tag_updates.items():
        if not isinstance(enum, config.TAGS_TO_USE):
            log.debug(f"{enum} not in config skipping")
            continue
        if not performer_ids:
            continue
        log.info(f"Adding {enum} tag to {len(performer_ids)} performer(s)...")
        try:
            stash.update_performers({
                "ids": performer_ids,
                "tag_ids": {
                    "ids": [enum.tag_id],
                    "mode": "ADD"
                }
            })
        except Exception as e:
            log.error(f"could not add {enum} to {len(performer_ids)} performer(s): {e}")
            failed.update(performer_ids)
            tally.errors += 1

    if failed:
        completed[:] = [pid for pid in completed if pid not in failed]
    return completed


def set_markers(performer_ids, tally):
    """Record that these performers have been calculated, at this version.

    `partial` rather than `full`: it writes the one key and leaves every other custom
    field on the performer untouched.
    """
    if not performer_ids:
        return
    log.info(f"Marking {len(performer_ids)} performer(s) as processed...")
    try:
        stash.update_performers({
            "ids": performer_ids,
            "custom_fields": {"partial": {MARKER_FIELD: MARKER_VALUE}}
        })
    except Exception as e:
        # Not fatal: the tags are in Stash and correct. The performers simply come round
        # again on the next Add New, which is idempotent.
        log.error(f"could not write the processed marker: {e}")
        log.error("the calculated tags were saved; these performers will be processed "
                  "again on the next Add New")
        tally.errors += 1


def is_processed(performer):
    fields = performer.get("custom_fields") or {}
    return str(fields.get(MARKER_FIELD) or "") == MARKER_VALUE


def find_performers():
    """Every performer, with what this plugin needs to know about each.

    The `custom_fields` in the fragment is also the capability check: a Stash too old to
    have performer custom fields rejects the query, and saying so plainly beats a raw
    GraphQL error about an unknown field.
    """
    try:
        return stash.find_performers(fragment=PERFORMER_FRAGMENT)
    except Exception as e:
        if "custom_fields" in str(e):
            log.error("this Stash does not support performer custom fields, which "
                      f"{PLUGIN_NAME} uses to record which performers it has already "
                      "calculated. Stash v0.28 or newer is required.")
            raise SystemExit(1)
        raise


# ------------------------------------------------------------------------ the hook

# The fields the calculation actually reads. An update that touched none of them cannot
# change any tag, so there is nothing to recalculate.
CALCULATION_FIELDS = frozenset({
    "measurements", "height_cm", "weight", "ethnicity", "gender",
})


def recalculate_performer():
    """One performer, recalculated because Stash says it changed.

    Fired by `Performer.Update.Post` and `Performer.Create.Post`. Two things make this
    safe to hang off an update hook:

    **It does not react to its own writes.** `bulkPerformerUpdate` fires the update hook
    once *per performer it touched*, so a plugin that recalculates on every update and
    then writes tags would call itself for ever. `hookContext.inputFields` says which
    fields the update carried, and this plugin only ever writes `tag_ids` and
    `custom_fields` - neither of which the calculation reads. An update that touched
    none of `CALCULATION_FIELDS` is ignored before anything is fetched.

    **It writes only differences.** The tags the performer should have are compared with
    the ones it has, and only the difference is sent. A recalculation that changes
    nothing writes nothing - which means that even if the guard above were somehow
    bypassed, the second pass would be silent and the chain would stop there.
    """
    if not getattr(config, "RECALCULATE_ON_UPDATE", True):
        log.debug("RECALCULATE_ON_UPDATE is off in config.py; ignoring hook")
        return

    context = (fragment.get("args") or {}).get("hookContext") or {}
    performer_id = context.get("id")
    trigger = context.get("type") or ""

    if not performer_id:
        log.error(f"{trigger or 'hook'} fired without a performer id; nothing to do")
        return

    if not _hook_is_relevant(context, trigger, performer_id):
        return

    performer = stash.find_performer(int(performer_id), fragment=PERFORMER_FRAGMENT)
    if not performer:
        log.debug(f"performer {performer_id} is gone; nothing to recalculate")
        return

    tally = Tally()
    name = f"{performer.get('name')} ({performer_id})"

    tag_updates, completed = process_performers([performer], tally)
    if not completed:
        log.warning(f"{name}: not recalculated, see the error above")
        return

    wanted = set()
    for enum in tag_updates:
        if not isinstance(enum, config.TAGS_TO_USE):
            continue
        try:
            wanted.add(str(resolve_tag(enum)))
        except Exception as e:
            log.error(f"{name}: could not resolve the {enum} tag: {e}")
            return

    managed = managed_tag_ids()
    present = {str(t["id"]) for t in (performer.get("tags") or [])}
    ours = present & managed

    add = sorted(wanted - ours)
    remove = sorted(ours - wanted)
    marked = is_processed(performer)

    if not add and not remove and marked:
        log.debug(f"{name}: recalculated, nothing changed")
        return

    try:
        if remove:
            stash.update_performers({"ids": [str(performer_id)],
                                     "tag_ids": {"ids": remove, "mode": "REMOVE"}})
        if add:
            stash.update_performers({"ids": [str(performer_id)],
                                     "tag_ids": {"ids": add, "mode": "ADD"}})
    except Exception as e:
        log.error(f"{name}: could not update tags: {e}")
        return

    if not marked:
        set_markers([str(performer_id)], tally)

    log.info(f"{name}: recalculated after {trigger or 'an update'} - "
             f"{len(add)} tag(s) added, {len(remove)} removed")


def _hook_is_relevant(context, trigger, performer_id):
    """Should this hook firing lead to a recalculation?

    A new performer always should. An update should only when it carried a field the
    calculation reads - which is also what keeps the plugin from answering its own
    writes.
    """
    if trigger.startswith("Performer.Create"):
        return True

    fields = context.get("inputFields")
    if not fields:
        # Without the field list there is no way to tell this update from one of this
        # plugin's own, and guessing wrong means a plugin that triggers itself for ever.
        # Skipping is the safe answer; Add New picks the performer up either way.
        log.warning(f"{trigger} for performer {performer_id} carried no inputFields, so "
                    "it cannot be told apart from this plugin's own writes - skipped. "
                    "Run Add New, or Full Update, to recalculate.")
        return False

    touched = CALCULATION_FIELDS.intersection(fields)
    if not touched:
        log.debug(f"performer {performer_id}: update touched {sorted(fields)}, none of "
                  "which the calculation reads - nothing to do")
        return False

    log.debug(f"performer {performer_id}: {sorted(touched)} changed, recalculating")
    return True


def managed_tag_ids():
    """Every tag this plugin manages, in one query.

    By the marker in the description, the way `destroy_managed_tags` finds them, rather
    than by resolving all sixty-odd enum aliases one at a time: a hook runs once per
    performer edit, and sixty queries per edit is not a thing to do to a library during
    a scrape.
    """
    marker = TAG_MANAGED_BY.replace("[", "\\[").replace("]", "\\]")
    found = stash.find_tags(
        f={"description": {"value": f"^{marker}", "modifier": "MATCHES_REGEX"}},
        fragment="id")
    return {str(t["id"]) for t in found}


# ---------------------------------------------------------------------- the tasks

def add_new():
    """Calculate the performers this plugin has not calculated before.

    Nothing is removed and nothing already tagged is touched: the performers that
    already carry a marker are not even read past their marker.
    """
    tally = Tally()
    all_tag_ids = init_managed_tags()

    performers = find_performers()
    tally.total = len(performers)

    pending = []
    for p in performers:
        if is_processed(p):
            tally.already_processed += 1
        else:
            pending.append(p)

    if not pending:
        log.info(f"{PLUGIN_NAME} - Add New: nothing to do, "
                 f"all {tally.total} performer(s) already processed")
        return

    tag_updates, completed = process_performers(pending, tally)
    completed = apply_tag_updates(tag_updates, tally, completed)
    set_markers(completed, tally)

    log.info(
        f"{PLUGIN_NAME} - Add New completed\n"
        f"Total performers: {tally.total}\n"
        f"Already processed: {tally.already_processed}\n"
        f"Processed now: {tally.processed}\n"
        f"Missing measurements: {tally.missing_measurements}\n"
        f"Parse errors: {tally.parse_errors}\n"
        f"Could not classify: {tally.no_bodyshape}\n"
        f"Errors: {tally.errors}"
    )


def full_update():
    """Recalculate the whole library: reset what this calculator did, then do it again.

    Phase 1 takes this plugin's managed tags off every performer and clears the markers,
    in one bulk call. What it removes is exactly the ids collected while finding the
    managed tags - so a tag the user added by hand, a tag from another plugin, and a tag
    the original Performer Body Calculator manages are all invisible to it.

    It is not `destroy_managed_tags`: the tags themselves stay, with their aliases,
    descriptions and any renaming the user has done to them. Only the assignments go.
    """
    tally = Tally()
    all_tag_ids = init_managed_tags()

    performers = find_performers()
    tally.total = len(performers)

    tally.markers_cleared = sum(1 for p in performers
                                if (p.get("custom_fields") or {}).get(MARKER_FIELD))
    managed = set(all_tag_ids)
    tally.assignments_removed = sum(
        1 for p in performers for t in (p.get("tags") or []) if t["id"] in managed)

    log.info(f"Removing {tally.assignments_removed} existing plugin tag assignment(s) "
             f"and {tally.markers_cleared} marker(s)...")
    stash.update_performers({
        "ids": [p["id"] for p in performers],
        "tag_ids": {
            "ids": all_tag_ids,
            "mode": "REMOVE"
        },
        "custom_fields": {"remove": [MARKER_FIELD]}
    })

    tag_updates, completed = process_performers(performers, tally)
    completed = apply_tag_updates(tag_updates, tally, completed)
    set_markers(completed, tally)

    log.info(
        f"{PLUGIN_NAME} - Full Update completed\n"
        f"Performers: {tally.total}\n"
        f"Markers cleared: {tally.markers_cleared}\n"
        f"Managed tag assignments removed: {tally.assignments_removed}\n"
        f"Processed: {tally.processed}\n"
        f"Missing measurements: {tally.missing_measurements}\n"
        f"Parse errors: {tally.parse_errors}\n"
        f"Could not classify: {tally.no_bodyshape}\n"
        f"Errors: {tally.errors}"
    )


if __name__ == '__main__':
    main()
