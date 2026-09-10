# Performer Organized

The **Organized** flag Stash gives scenes, galleries and images — for performers.

A switch on the performer page, an icon on the card, *Set organized* / *Set unorganized*
for a whole selection, and a real server-side filter in the standard Performers list.

Nothing sets the flag but you. It is not touched by scraping, identifying, editing or
saving a performer, and this plugin never infers it from a performer's completeness.

## What it is stored in

A **custom field on the performer**, not a tag and not a database of its own:

```
Performer.custom_fields.organized = true
```

Organized sets the key. Not organized **removes** it, rather than writing `false`, so a
performer nobody has touched and one that was turned off read the same way and the filter
cannot disagree with itself.

You can see it — and set it by hand — in *Performer → Edit → Custom Fields*. Values are
read generously there: `true`, `yes`, `1` and `on` all count as organized.

### Why not a tag

A service tag would have worked, and it is the usual answer in a plugin. It is the wrong
answer here, because Stash already has the better one:

| | tag | custom field |
| --- | --- | --- |
| server-side filter | yes | **yes** — `PerformerFilterType.custom_fields` |
| visible in the tag list | yes, forever | no |
| visible in every performer's tag editor | yes | no |
| pollutes tag counts and tag pages | yes | no |
| can be broken by editing tags | yes | no |

`Performer.custom_fields` is a real field of the Stash schema (`Map!`), writable through
`PerformerUpdateInput` and `BulkPerformerUpdateInput`, and — the part that decides it —
filterable through `PerformerFilterType.custom_fields: [CustomFieldCriterionInput!]`. The
filter is SQL against the performers table, so it stays fast on a library of any size.
Nothing is ever loaded into the browser to be sieved there.

### What a write touches

`CustomFieldsInput` has three arms: `full` replaces the whole map, `partial` updates only
the keys given, `remove` deletes only the keys listed. This plugin uses `partial` and
`remove`, never `full`. So one key of one map changes, and nothing else is in the mutation
at all — not your other custom fields, and none of name, aliases, tags, URLs, images,
measurements, stash IDs, rating or favourite.

## Filtering

Two ways in, both the same query.

**The quick control**, above the Performers grid:

```
Organized   [ Any ] [ Yes ] [ No ]
```

It rewrites the page's own query string and navigates. What comes back is the standard
Performers list — same grid, same paging, same everything — filtered by the server.

> It edits the URL rather than the list's `ListFilterModel` on purpose. Going through
> the model would be tidier, but it needs the patched component to hand its filter over,
> and `PerformerList` does not pass one in every Stash build — which showed up as three
> buttons that were disabled, with no click, no console error and no request to explain
> why. The query string is the interface both ends already agree on: Stash writes it
> from the filter builder and reads it back on every navigation, so it cannot be absent,
> and a criterion put there is indistinguishable from one set by hand.

**The filter builder**, for a filter you want to keep or combine:

```
Performers → Filter → Custom Fields → organized → is not null
```

`is not null` is organized, `is null` is not organized. The two controls read each other:
set the criterion in Filters and the quick control shows it; press *No* and the criterion
appears in Filters. Saving it as a **filter preset** works natively, because it is a
native criterion — nothing about the preset depends on this plugin.

> Why `is null` and not `equals false`: Stash joins `IS_NULL` and `NOT_EQUALS` with a
> LEFT JOIN (`pkg/sqlite/custom_fields.go`), so *not organized* correctly includes every
> performer that has no custom fields at all — which, on a fresh library, is all of them.
> `EQUALS` uses an inner join and would answer "none".

## Bulk

Select performers in the list and the strip above the grid gains:

```
3 selected   [ Set organized ]  [ Set unorganized ]
```

One `bulkPerformerUpdate` for the whole selection — one round trip, one transaction, no
loop that can half-succeed — and the list refreshes itself afterwards, so rows that no
longer match an active Organized filter leave it.

## For other plugins

`window.PerformerOrganized` is the public surface:

```js
PerformerOrganized.isOrganized(performer)              // from an object in hand
PerformerOrganized.isPerformerOrganized(id)            // -> Promise<boolean>
PerformerOrganized.setPerformerOrganized(id, true)     // -> Promise<boolean>
PerformerOrganized.setPerformersOrganized(ids, false)  // -> Promise<Performer[]>
```

The three asynchronous calls need Stash's Apollo client, which this plugin captures the
first time one of its controls renders. Open a performer page or the performer list once
in the session before calling them from somewhere that has never shown a control.

## How it attaches

Four published plugin patch points, no DOM surgery and no component replacement:

| Where | Patch point | What is added |
| --- | --- | --- |
| Performer page | `PerformerDetailsPanel` | the Organized row |
| Performer page, scrolled | `CompressedPerformerDetailsPanel` | the icon |
| Performer card | `PerformerCard.Overlays` | the icon |
| Performer list | `PerformerList` | quick filter + bulk actions |

Each patch takes what the component rendered and returns it with one more thing beside
it, so another plugin patching the same component still sees the whole thing. Everything
is a React component, so an SPA navigation or a re-render cannot leave a duplicate
control behind, and there is nothing to re-initialise after a page reload.

## Behaviour worth knowing

* **A click is optimistic.** The control changes immediately and rolls back, with an
  error toast, if the server refuses. Nothing is left claiming a state the library does
  not agree with.
* **Toggling in a filtered list does not make the row vanish.** The icon updates; the row
  stays until the list is next fetched. This is what Stash's own Organized does on
  scenes. A bulk change does refresh the list, because that is when it matters.
* **Deleting the custom field by hand** — in *Edit → Custom Fields* — is exactly the same
  as pressing the toggle off. There is no separate state to corrupt.

## Limitations

* The filter builder lists the criterion as **Custom Fields → organized**, not as a
  criterion named *Organized*. Stash's per-page criterion list is a fixed constant with
  no registration point for plugins, so adding a named filter field would mean patching
  Stash's internals — which would break on an update, for a label. The quick control
  gives the one-click UX instead, and presets save either way.
* There is no `PerformerListOperations` patch point (scenes do not have one either), so
  the bulk actions live in a strip above the grid rather than inside the list's own
  ⋯ menu.
* Both quick control and bulk actions need the performer list; they do not appear in the
  tagger view.
