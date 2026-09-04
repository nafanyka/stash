# FastDiscovery — architecture

FastDiscovery answers one question for one scene: *what does everything I have installed
know about this?* It runs **every** stash-box Stash is configured with, follows every URL
the scene and those answers carry through **every** scraper that can read them, keeps
each answer separately with its provenance, and puts the lot in one review table. The
scene is not touched until somebody presses Apply, and the moment they decide, the
results are deleted.

It is not a metadata source and it is not a ranking engine. It never picks a winner.

Verified against the `develop` and `v0.31.1` sources of `stashapp/stash`; v0.31.1 is the
current release (April 2026) and is what the API notes below describe.

---

## 1. Stash API feasibility

### 1.1 What the requirements need, and what Stash offers

| Requirement | Supported | Mechanism |
| --- | --- | --- |
| Discover the configured stash-boxes without hardcoding them (§2) | **Yes** | `configuration { general { stashBoxes { endpoint name } } }`. Read at the start of every run, so a box added later is used with no code change. |
| Invoke each box **independently** (§1, §3) | **Yes** | `scrapeSingleScene(source: {stash_box_endpoint}, input: {scene_id})`, one request per box. `ScraperSourceInput` is `{stash_box_index (deprecated) \| stash_box_endpoint \| scraper_id}`. |
| Fingerprint / oshash / pHash matching done by Stash, not by us (§3) | **Yes** | That *is* what `input: {scene_id}` means to the stash-box branch of the resolver: Stash reads the scene's fingerprints and queries the box. FastDiscovery never computes or sends a hash. |
| No FastDiscovery-side stash-box credentials (§2, §43) | **Yes** | The `stashBoxes` query also returns `api_key`; the selection here deliberately asks only for `endpoint` and `name`. |
| Enumerate installed scrapers and their URL patterns (§5) | **Yes** | `listScrapers(types: [SCENE]) { id name scene { urls supported_scrapes } }`. |
| Scrape a URL (§5) | **Partly** — see **L1** | `scrapeSceneURL(url)`. |
| Local entity resolution for performers/tags/studios (§12–15) | **Yes, natively** | Stash does it in `pkg/match/scraped.go`: stash id → exact name → alias, and sets `stored_id` **only on a unique match**. That is requirement 13's priority order, already implemented by the server, so FastDiscovery reads `stored_id` instead of reimplementing it. |
| Create an entity only when chosen (§20) | **Yes** | `performerCreate` / `tagCreate` / `studioCreate` / `groupCreate`, called only for a ticked candidate. |
| Write the scene once (§20, §55) | **Yes** | One `sceneUpdate`. `SceneUpdateInput` covers `title code details director date rating100 urls studio_id performer_ids tag_ids groups stash_ids cover_image`. |
| Apply a chosen cover (§17) | **Yes** | `cover_image` takes *either* a URL or a base64 data URI — `utils.ProcessImageInput` fetches a URL server-side — so FastDiscovery never downloads a cover itself. |
| Background work with progress and cancel (§30, §46) | **Yes** | `runPluginTask` → job id; progress through the `\x01p\x02` stderr protocol; `stopJob` to cancel. |
| Synchronous UI ↔ backend calls | **Yes** | `runPluginOperation(plugin_id, args) : Any`. |
| A page, a scene tab, and a bulk action (§25–27) | **Yes** | `PluginApi.register.route`, and the patch points `ScenePage.Tabs`, `ScenePage.TabContent`, **`SceneListOperations`** (the selected-scenes menu) and `MainNavBar.MenuItems`. |
| Appear under *Scrape with…* (§28) | **Yes, with a caveat** — see **L3** | A `sceneByFragment` scraper. |

### 1.2 Identify is **not** usable, and this was the deciding check

`internal/identify/identify.go`, `scrapeScene()`:

```go
for _, source := range t.Sources {
    results, err := source.Scraper.ScrapeScenes(ctx, scene.ID)
    if err != nil { ...; continue }
    if len(results) > 0 {
        ...
        return &scrapeResult{result: results[0], source: source}, nil   // <-- stops here
    }
}
```

It **returns at the first source that answers**, so the remaining stash-boxes are never
asked, and the task then writes the result through `modifyScene()` with no review. Both
halves are the opposite of what FastDiscovery is for. It is not used, wrapped or imitated
anywhere in this plugin; every box is invoked directly, one request each.

### 1.3 Limitations found, and what is done about them

**L1 — a URL scrape cannot be aimed at a chosen scraper, and the winner is not reported.**
`Cache.ScrapeURL` iterates `c.scrapers`, a Go map, and returns the first entry whose
`supportsURL` matches (`pkg/scraper/cache.go`). Map iteration order is randomised, so with
several matching scrapers the choice varies between calls and nothing in the response says
which one ran. Requirement 5 — *run every applicable scraper for every URL* — therefore has
no direct API.

*What FastDiscovery does instead*, in `registry.url_sources`:

* the matching set is computed locally with Stash's own rule,
  `strings.Contains(url, pattern)` (`pkg/scraper/definition.go`), lower-cased on both
  sides;
* **one matching scraper** → `scrapeSceneURL`, attribution `CERTAIN`;
* **several** → `scrapeSceneURL` still runs, as a column honestly labelled
  `auto (A, B)` with attribution `AMBIGUOUS`, **plus** one aimed call per matching
  scraper that also declares `FRAGMENT`:
  `scrapeSingleScene(source: {scraper_id: X}, input: {scene_input: {url}})`, which does
  reach that specific scraper and gets a column of its own;
* a matching scraper that is **URL-only** and lost the ambiguous draw cannot be reached
  by any public API. It is recorded as a source with status `UNREACHABLE` and the reason,
  so the review shows the gap instead of implying the URL was fully covered.

No hidden workaround: nothing pokes at Stash's internals, and the aimed call is recorded
as a different `method` (`URL_FRAGMENT`) so it is never confused with a real URL scrape.

**L2 — one broken scraper fails the whole GraphQL request.**
`internal/api/resolver_query_scraper.go` returns the scraper's error for the entire query.
FastDiscovery therefore issues **exactly one source per request**, which makes requirement
29 — a failed source never fails the run — true by construction rather than by handling.

**L3 — a scene scraper's return value is what Stash writes.**
Registering `sceneByFragment` puts FastDiscovery in *Scrape with…* and in Identify's source
list, and Identify saves whatever a source returns without review. So the shim in
`scrapers/FastDiscovery/` **always prints `null`**: it starts a run (or reports the one
already waiting) and points at the review. A test asserts that the only thing it ever
prints is `null` and that it contains no scraping logic. This is the "minimal entry point
where possible, orchestration in the plugin" answer requirement 28 asks for, rather than a
fake merged `ScrapedScene`.

**L4 — plugin settings have no declarable defaults and only three types.**
Every default lives in `fastdiscovery/settings.py`; the manifest declares each setting so
it is editable in Stash's own panel, and a test keeps the two in step.

**L5 — a plugin cannot serve HTTP.** All UI traffic goes through `runPluginOperation`,
one Python process per call, so operations are cheap and discovery itself is always a job.

**L6 — a plugin route must not live under `/plugin/...`** (server-owned,
`internal/api/server.go`), and plugin files are deleted on package update. The page is at
**`/fast-discovery`** and the database lives in the Stash config directory, outside the
plugin.

**L7 — "never store binary images" is not fully achievable.** Requirement 42 asks for
references only, but a scraper's cover often arrives as `data:image/jpeg;base64,…`, which
has no address to store instead. So:

* an image given as an **http(s) URL** is stored as that URL and never downloaded;
* an image given as a **data URI** has its bytes stored once, keyed by SHA-256, so the
  same cover from ten sources costs one row; the payload keeps a reference;
* thumbnails are fetched lazily, one at a time, only when actually rendered;
* every blob is deleted with its run — on Apply, on Reject, and by the maintenance task.

**L8 — `ScrapedScene` has no `rating`.** Requirement 10 lists it, but no scraper can
supply one; `rating100` exists only on `SceneUpdateInput`. It is shown as a row whose only
possible value is the scene's own, so the review stays a complete picture of what Apply
would write.

---

## 2. The run

```
scene ──► every configured stash-box, one request each      depth 0
      └─► the scene's own URLs ──┐
                                 ├─► URL pool (normalised, de-duplicated)   depth 0
          stash-box result URLs ─┘
                                 └─► every scraper that matches each URL    depth 0
                                          └─► the URLs those results returned   depth 1
                                                   └─► …                        depth n
```

Loop prevention lives in the database, not in a set some function has to remember to
update:

* `urls(run_id, norm_key)` is unique — a URL enters the frontier once, however many
  results mention it;
* `sources(run_id, source_key)` is unique, where `source_key` is
  `(method, scraper, normalised url)` — the `(scraper, url)` pair from requirement 4.

So `A → B → C → A` terminates on the second sight of `A`, and the same scraper reached two
different ways stays two distinct answers.

URL normalisation is deliberately shallow (requirement 6): lower-cased scheme and host,
`www.` dropped, default port dropped, fragment dropped, one trailing slash collapsed,
duplicate slashes collapsed, and only a fixed list of referrer parameters (`utm_*`, `ref`,
`aff`, …) removed. Path case is preserved and no other query parameter is touched, because
on a great many sites the query *is* the page. The original spelling is always kept and is
what gets written.

Related URLs — a performer's homepage, a studio's site — are recorded for the graph but
never scraped: they are not scene pages.

---

## 3. The review

Built by `merge.py` as a pure function of the run's stored results plus the scene **as it
stands right now** (not the snapshot taken during the run, which may be stale).

* **A cell's shape follows its row's.** A single-choice row maps a column to one option
  id or nothing; a list row maps it to the list of options that column contributed. The
  selection is applied as a whole, so one row handing back the wrong shape takes every
  other field down with it.
* **One logical value, many sources.** Four sources agreeing on a date is one entry with
  four source references. The comparison key is normalised; the value written is the raw
  one (requirements 18, 34).
* **No empty rows.** A field with nothing on the scene and nothing from any source has no
  row. A field only the scene has still gets one — it is what Apply would keep
  (requirements 9, 33).
* **Everything ScrapedScene offers.** The field table in `fields.py` drives the matrix, the
  defaults and the write. A field a future Stash adds appears automatically as a read-only
  row, with its values and provenance, via introspection; making it writable is one line
  (requirement 32).

| Kind | Fields | How it merges |
| --- | --- | --- |
| scalar | title, date, code, director, details, rating100\* | choose one |
| entity | studio | choose one |
| entity list | performers, tags, groups | union, tick individually |
| url list | urls | union, all ticked by default |
| stash id list | stash_ids | union, only the existing ones ticked |
| image | image → cover_image | choose one |

\* no scraper can supply a rating, so that row only ever shows the scene's own value.
`duration` is not a row at all: it comes from the scene's own file and can never be
written, so it would be a row nobody could act on. Sources still report it and it is
still in the stored results.

Every option carries an id derived from **what the option is** - the value's comparison
key, or an entity's strongest identity - never from its position. The review is built
twice, once to show and once when Apply resolves the selection, and in between an
unmatched name can become a known record, which reorders the row. Positional ids would
silently move a tick from one performer to another; hashed ones either resolve to the
same thing or fail loudly.

The URL row is what the sources claim as the **scene's own address**: their `url` and
`urls` fields. A URL a scraper mentioned in its `details` prose is followed and appears
in the discovery graph, but it is not offered as a scene URL - it is a lead, not a claim,
and the URL row is ticked by default.

### Entity identity

Union-find over **strong** evidence only, in requirement 13's order: a shared local id, a
shared `(endpoint, stash_id)`, or a shared canonical URL. Two mentions agreeing on nothing
but a name are **not** merged — names collide, and fusing two people silently is worse than
showing one twice. Instead the candidate is labelled *may be \<the other one\>* and left
for the user.

Mentions with no strong evidence at all are grouped by name among themselves, because there
they are all the same evidence-free suggestion.

`stored_id` is Stash's own answer. For a scraper that returned a bare name and got no
`stored_id`, the review asks Stash by exact name — a unique hit becomes an existing entity,
an ambiguous one stays a candidate with the alternatives attached.

### Rejecting a column

A result that matched the wrong scene is wrong about every field at once, and unticking
its twenty values one row at a time is tedious and easy to do incompletely. Every column
header therefore carries a **reject** toggle: the review is rebuilt without that column,
so its values, its votes, its entities and its images all disappear, and a row it was the
only contributor to disappears with it.

A column, not a source. A source that answers with a list - a stash-box name search -
gets one column per answer, and the whole point is that the first can be the scene while
the second is a different film. `StashDB (search)` and `StashDB (search) #2` are struck
out independently.

The column stays in the table, dimmed and hatched, because a decision you cannot see is
one you cannot take back.

The set is stored on the run (`runs.rejected_columns_json`), not held in the page, for
one reason: the matrix, the defaults and Apply all have to agree about which columns
count. Apply rebuilds the review with the same set, so it can only ever write something the
reviewer actually saw. A tick left pointing at a value only the rejected column
offered is dropped when the matrix is rebuilt - and for a single-choice row, which would
otherwise be left selecting nothing, the row falls back to its default.

### Fields with nothing chosen

A writable row where nothing is ticked writes nothing. That is a legitimate choice and
also exactly what an overlooked one looks like, so the field name — the sticky column,
the one part of a wide row always on screen — is tinted red and marked `○`.

### Defaults (requirement 35)

* scalar: the scene's own value, always, when it has one; if the scene has nothing and
  exactly one answer exists, that one; if several disagree, nothing;
* URLs: all of them;
* entities: on the scene, or an existing local record → ticked. **A candidate that would
  have to be created is never ticked by default**;
* stash ids: only the ones already on the scene;
* image: the scene's current cover.

---

## 4. Apply, Reject, Rescan

### Never creating what already exists

A candidate is labelled "does not exist yet" as of the moment the review was built. By
the time Apply runs, the same tag can have been created by another scene's review, by a
second browser tab, or by hand - and creating it blindly is a unique-constraint error
from Stash's database with the whole apply lost behind it.

So every ticked candidate is looked up by name once more, immediately before it would be
created, and linked instead if it is there. That lookup is uncapped, unlike the one that
labels the review: there are only ever as many as the user actually ticked. If the create
still fails - somebody won the race in the milliseconds between, or the name collides
with an alias, which an exact-name lookup cannot see - the lookup is repeated once and
the result linked if it has appeared. Only a create that fails for a real reason is
reported as one.

The result says which entities were created and which turned out to exist already, so
"created 3" on one scene and "created 0" on the next is not a mystery.

All of which rests on the lookup actually working, and for several versions it did not.
`findTags(tag_filter: {...}, filter: $filter)` takes a `FindFilterType` for `filter` -
that argument is paging, not the entity's own filter, which goes inline above it.
Declaring the variable as `TagFilterType` is a GraphQL *validation* error: the server
rejects the query before running it, and this client turns a rejected query into an empty
answer. Every name lookup therefore reported that nothing existed, which is
indistinguishable from a library that really has nothing - until Apply created a tag Stash
already had and the mutation failed.

A lookup that could not be asked now says so in the log (`name matching is degraded`)
instead of answering "no", and a unit test asserts that every variable in these queries is
declared as the type it is used as.

### Names, and names a record answers to

The lookup asks two questions, not one. First: what is *called* this? Then, only for a
name nothing is called: what has this as an *alias*?

The second question is not a nicety. `EnsureTagNameUnique` (`pkg/tag/update.go`) checks a
new name against existing names **and** existing aliases, so a name the library appears
not to have can still be spoken for, and creating it is refused with `name 'X' is used as
alias for 'Y'`. A candidate offered as "does not exist yet" would then be undeliverable.
So a name only an alias owns resolves to the record that owns it, and the scene gets that
record.

When it does, the library's own spelling wins: the option is shown under the record's
name, with the scraped name kept as one of its aliases and named in the chip's tooltip.
If the owner is already on the scene, or arrived from another source under its real name,
the two fold into one option by local id, as any two mentions of one record do.

Uniqueness still decides. Two records answering to the same name is not a match, it is a
question only the user can answer, so the entity stays a candidate with both alternatives
attached. Groups are asked by name only - `GroupFilterType` has no alias criterion.

### A rejection that keeps what survives it

Striking out a column must drop what only that column said, and nothing else. That is
harder than it looks, because an option id is a hash of the option's identity, and an
entity's identity is built from every mention of it.

Two stash-boxes reporting the same tag report it with their own remote ids, so it arrives
as two mentions agreeing on nothing but a name. The name lookup gives both the same local
record, they fold into one option, and the option keeps the id of whichever mention came
first. Strike that box out and the survivor is the *other* mention, under its own id - so
a selection carried across by id alone loses a tag both boxes agreed on, which reads as
FastDiscovery quietly undoing a decision.

So the selection is carried across by **signature** - what the option is (an entity's
name and disambiguation, or a value's key), not the evidence for it. A signature that
still matches keeps its tick; a signature that matches nothing was only ever offered by
the source now struck out, and dropping it is the point.

### Apply, step by step

Apply, in order: rebuild the review against the live scene → drop the no-ops → create only
the ticked candidates → **one** `sceneUpdate` → audit row → delete the payload. A scene
edited between the review being rendered and Apply being pressed is detected by
`updated_at` and refused rather than silently overwritten.

If the update fails, nothing is deleted: the run becomes `FAILED_APPLY`, stays reviewable,
and Apply can be pressed again. Entities created before the failure are named in the audit
row.

### The FastDiscovery tag

An applied scene comes out carrying a tag called **FastDiscovery**, created the first time
one is applied to. It is the only thing Apply writes that nobody ticked, and it exists so
a scene FastDiscovery has written to can be told apart in Stash from one it has never
touched - filterable like any other tag.

Three things it deliberately is not:

* **not a second mutation.** The tag joins the same `tag_ids` the reviewed selection
  produced, so a scene cannot come out marked as dealt with while half of what was ticked
  failed to write. When the selection did not touch tags at all, `tag_ids` is built from
  the tags the scene has right now, so marking adds one tag and removes none.
* **not written when nothing else is.** Apply with nothing selected stays what it was -
  no write, review left open. An apply that writes nothing is not an apply, and a tag
  saying otherwise would be a lie about the scene.
* **not added by Reject, and not re-added when it is already there.** A scene that already
  carries the tag and whose selection does not touch tags gets no `tag_ids` in its update
  at all.

The tag is found by name, not by id, so deleting it in Stash is not a broken state: the
next apply simply makes it again. Renaming it, on the other hand, makes the next apply
create a fresh one under the old name.

Reject writes nothing and deletes the payload. Rescan replaces an undecided run, after a
confirmation the API enforces: `run.start` refuses a scene that already has results waiting
unless the caller passes `replace`.

Reject is also on every reviewable row of the run list, next to Review, and does exactly
what the button in the review does - the same `run.reject`. A run that found nothing worth
reading is not worth opening first, and the list is where that is obvious. There is no
confirmation, on purpose: it is meant to be cheaper than opening the review, and the scene
is untouched either way. What it does destroy is the stored results, which is what Reject
means everywhere else here.

### What survives a decision (requirement 24)

Everything in `sources`, `results`, `urls` and `images` is deleted. What is kept is the
`runs` row — scene id, run id, started, finished, final status, source count, error count —
and one `applications` row naming the fields written and the entities created. No metadata
values, ever. A run left `RUNNING` by a killed process is swept to `FAILED` after
`staleRunHours`.

---

## 5. Database

SQLite, WAL, `foreign_keys=ON`, short transactions, version in `PRAGMA user_version`.
Default path `{stash config dir}/fast-discovery/fastdiscovery.sqlite`. Stash's own database
is never opened.

```
meta          key/value: the cached ScrapedScene field list, per Stash build
runs          one discovery run; its first columns are the audit record, and it holds
              the draft selection and the rejected-column set until a decision
sources       one invocation: type, method, name, endpoint/scraper/url, depth,
              parent_source_id (the discovery graph edge), attribution, status, error
results       raw_json exactly as Stash returned it, plus an image reference
urls          the frontier and the graph: norm_key unique per run, state, found-by
images        base64 covers, de-duplicated by sha256, deleted with the run
applications  what an apply did: fields and entity ids, never values
```

Nothing holds a transaction open across a scraper call: the source row is written, the
transaction ends, the network call happens, then a short transaction commits the result. A
`stopJob` kill therefore loses at most the one source in flight.

---

## 6. Concurrency and limits

A bounded thread pool (`maxConcurrentScrapers`, default 3) of plain `urllib` POSTs, each
with its own timeout that doubles as the scraper timeout — script scrapers run under
`CommandContext`, so abandoning the request kills the scraper process. Database writes all
happen on the calling thread.

Defaults: `maxDepth` 3, `maxUrlsPerRun` 60, `maxSourcesPerRun` 120, `runTimeBudget` 900 s,
`maxResultsPerSource` 3, `staleRunHours` 12. Every limit that stops something records why,
on the URL or on a skipped source, so a truncated run says so.

---

## 7. Files

```
plugins/FastDiscovery/
  FastDiscovery.yml        manifest: exec, ui, tasks, settings
  FastDiscovery.py         entry point - parses stdin, dispatches, prints {"output": ...}
  fastdiscovery/
    settings.py    configuration schema and defaults (L4)
    logs.py        Stash's stderr protocol, with credential redaction
    stash.py       the only module containing GraphQL
    urls.py        URL normalisation and the loop guard's key
    fields.py      the scene field model: what exists and how it merges
    registry.py    installed scrapers, stash-boxes, URL routing (L1)
    executor.py    bounded concurrency
    discovery.py   the run
    merge.py       stored results -> the review matrix
    apply.py       a reviewed selection -> one write, then the payload is dropped
    ops.py         the UI's API
    tasks.py       the job-queue entry points
    db/            migrations and the only SQL
  ui/fastdiscovery.js, ui/fastdiscovery.css
scrapers/FastDiscovery/
  FastDiscovery.yml, FastDiscovery.py    the entry point that always returns null (L3)
tests/
  fd_common.py, test_fd_*.py             the acceptance tests, offline
```

---

## 8. Acceptance tests

Every numbered acceptance test in the specification is a test in `tests/`:

| Spec | Test |
| --- | --- |
| §47 basic | `test_fd_discovery.py::TestAcceptanceBasic` |
| §48 duplicates | `test_fd_merge.py::TestEntityDedup` |
| §49 unknown entity | `test_fd_merge.py::TestUnknownEntities`, `test_fd_apply.py::TestEntities` |
| §50 empty fields | `test_fd_merge.py::TestEmptyRows` |
| §51 URL recursion | `test_fd_discovery.py::TestAcceptanceRecursion` |
| §52 same scraper, many URLs | `test_fd_discovery.py::TestAcceptanceSameScraperManyUrls` |
| §53 images | `test_fd_merge.py::TestImages` |
| §54 failure | `test_fd_discovery.py::TestAcceptanceFailure` |
| §55 apply safety | `test_fd_apply.py::TestAfterwards` |

Plus: every box runs even after one matches, boxes are read from Stash rather than
hardcoded, the orchestrator scrapers can never be invoked, the ambiguous-URL workaround
behaves as described, and credentials never reach a log line.

---

## 9. Security

Scraper output is untrusted. It is never `eval`ed, never interpolated into SQL (the
repository layer only passes parameters), and never inserted into the DOM as HTML — the UI
builds elements through `React.createElement`, and a discovered URL becomes an `href` only
after its scheme is checked. Data URIs are validated for MIME type and size before storage.
No filesystem path from scraper output is opened. Every log line goes through
`logs.sanitise`, which redacts anything shaped like an API key, token, cookie or
authorization header — scraper and server errors quote the request they failed on, which is
where such a thing would otherwise appear.

---

## 10. Deliberately not done

* No scoring, ranking or "best match". FastDiscovery shows; the user decides.
* No automatic apply, at any confidence.
* No stash-box query beyond the fingerprint lookup unless `stashboxNameSearch` is turned
  on — a name search answers with near-misses, and each one would be a column to read past.
* No permanent history. This is a review queue, not an archive (requirement 36).
