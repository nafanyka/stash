# MyMoover

Bulk-moves the physical files of selected **Scenes** to a destination folder, straight
from the Scenes page — media plus sidecars (funscripts, subtitles, ...), with a
dry-run preflight and per-conflict overwrite decisions before anything on disk moves.

## Using it

1. Select one or more Scenes on the Scenes page. A small **Move** bar appears above
   the list (see *Where the button lives*, below) showing how many are selected.
2. Pick a destination folder — browse from one of your configured library roots, or
   create a new folder inside the folder you're browsing with **+ New Folder**.
3. **Analyze**. Nothing on disk changes yet. You get counts (`N scenes`, `N media
   files`, `N sidecars`) and, for every filename that already exists at the
   destination, who it belongs to and an **Overwrite** checkbox — unchecked means
   skip, and that is the default. `Select all` / `Select none` only changes the
   checkboxes; every one is still applied individually.
4. **Move**. Only what you approved changes: files that are already in the
   destination are left alone and reported as such, a Scene's files that were not
   selected are untouched, and a skipped conflict keeps its target exactly as it was.
5. A result screen shows what happened per file (moved / already there / overwritten
   / skipped / error), plus a **Scan affected folders** button if you want Stash to
   pick up anything a scan would notice - see *Rescanning*, below. Nothing is scanned
   automatically.

Only the **files** move. `/old/Bonnie/video.mp4` moved to `/new/` becomes
`/new/video.mp4` — never `/new/Bonnie/video.mp4` — and `/old/Bonnie/` is left in
place, even once it's empty. A Scene with several Files moves every one of them;
there is no "primary file only".

## Settings

| Setting | Default | |
| --- | --- | --- |
| **Move sidecar files** | on | Off moves media only. |
| **Sidecar patterns** | funscripts (incl. multi-axis), `.srt`, `.vtt`, `.ass`, `.ssa`, `.nfo`, `.json` | Comma/newline-separated. `*` stands for one wildcard segment, so `.*.funscript` also matches `.L0.funscript`, `.R1.funscript`, etc. Only the media file's own source folder is searched — never recursively. |
| **Debug logging** | off | |

## How a move actually happens

Every physical move and every `File.path` update goes through Stash's own
`moveFiles` mutation — the plugin never edits Stash's database directly, and never
does a bare filesystem rename of a file Stash tracks. `moveFiles` runs the rename (or,
on a cross-device destination, copy-then-delete) and the database update in one
transaction, so a Scene's title, performers, tags, studio, rating, o-counter and
everything else are untouched — only the file's path changes. Sidecars are not File
records in Stash's schema at all, so those are moved by the plugin directly on disk,
using the same rename-or-copy-then-delete fallback Stash's own mover uses.

### Conflicts and overwrite

If the target path is already taken, `moveFiles` refuses on its own rather than
silently replacing it, and MyMoover asks you before doing anything about it. When you
approve an overwrite and the target is a file Stash already tracks:

* it belongs to a Scene that has other files too → that Scene keeps its other files;
  the conflicting one is reassigned away from being primary if it was, then removed
  (`sceneUpdate(primary_file_id: ...)` + `deleteFiles`);
* it is that Scene's only file → the Scene itself is removed
  (`sceneDestroy(delete_file: true, destroy_file_entry: true)`) — there is no
  Stash-supported way to detach a Scene's last file and leave an empty Scene behind;
* the target exists on disk but Stash has no record of it at all → it's just deleted.

Right before any of this runs, the current state is checked again against what
Analyze showed you. If it changed — a different file turned up, the owning Scene
changed — the item is marked **changed since Analyze** and left alone rather than
overwritten blind. One item failing (permission error, disappeared mid-move, ...)
never stops the rest of the batch.

### Rescanning

Because `moveFiles` updates `File.path` itself, a moved media file does not need a
rescan to show its new location. Nothing here ever starts a scan on its own — the
**Scan affected folders** button after a Move runs `metadataScan` scoped to exactly
the source and destination folders touched, only when you press it.

## Where the button lives

**Move** appears inline inside Stash's own selection toolbar — the same
Play / Edit / Delete / "..." row that appears once something is selected — not in a
separate control of its own. That row (`<div className="list-operations">`, built by
`ui/v2.5/src/components/List/ListOperationButtons.tsx`) is a local variable inside
`FilteredSceneList`, not a name a plugin can patch directly — a check against the
current `stashapp/stash` source (the tag this was verified on, and `develop`) found
no trace of a `SceneListOperations`-style patch point some notes elsewhere assume
exists, and nothing in `ListOperationButtons.tsx` is wrapped for patching either.

So MyMoover patches `FilteredSceneList` itself with `PluginApi.patch.after`, and does
a small, depth-bounded search over the element tree it returns. That tree is not yet
rendered — `<ListOperations .../>` is still an *unexecuted* element descriptor at
this point, so the `list-operations` class its own render eventually produces does
not exist yet and can never be found this way (confirmed the hard way: the first cut
of this searched for that class name and never matched anything). What the search
matches instead is the `<ListOperations>` call itself, by its own props — an
`operations` array together with `onEdit`/`onDelete` functions is distinctive enough
that nothing else in the tree is expected to match — and inserts the Move button as
that element's *next sibling*, so it lands exactly where `ListOperations` renders,
without needing to know anything about its internals. The same pass also reads the
current selection (`selectedIds`/`onSelectChange`) directly off the `SceneList`
element sitting in the same tree, matched by component reference
(`PluginApi.components.SceneList`) rather than by name, so it's never a render
behind. Both searches stop at the first match, so the (potentially large) card grid
sitting next to that row is never walked or re-cloned. If a future Stash release
changes this enough that nothing matches, Move still appears — just as its own small
bar above the list instead of inline with the native one — rather than silently
disappearing.

## Security

The backend never trusts a path the browser sent it. Every destination and every new
folder name is checked against `configuration.general.stashes` (resolved with
`realpath`, so a symlink can't point outside a configured root), a new folder's name
is rejected if it contains a path separator, `..`, or is otherwise not a bare name,
and every source path acted on is the path Stash itself reports for that exact File
ID — never one echoed back from the UI.
