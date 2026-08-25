# ScrapeDiscovery

You have a scene Stash cannot identify. You have several hundred scrapers installed.
Right now the only way to find out which of them knows the scene is to open *Scrape
with…* and try them one at a time.

ScrapeDiscovery does that for you. It runs the scrapers you already have against a
scene, follows the URLs their answers contain through the URL scrapers you already
have, stores every answer in its own database, and shows you what came back.

**It never writes to a scene on its own.** Discovery only ever reads and stores.
Applying metadata is a separate, explicit step.

> **Status: phase 1.** Scanning, storage, history, the inbox and the scene view work.
> Candidate correlation, confidence scoring, field-level comparison and applying
> metadata are the next phases — the database and the pipeline are already built for
> them, and see [what works today](#what-works-today) for exactly where the line is.

---

## Why it is not a scraper

A Stash scraper registered for scene fragments would appear in *Scrape with…*, which
looks like the obvious place for this. It is the wrong shape for three reasons:

1. **It would block.** A scraper runs inside the GraphQL request. On a real library
   this plugin plans 190 attempts for a normal scan and 393 for a deep one; the scene
   edit panel would sit there for minutes.
2. **It would write.** A fragment scraper's whole job is to hand Stash a scraped scene,
   which Stash then offers to save — and the *Identify* task saves it without asking.
   The point of ScrapeDiscovery is that nothing is saved until you choose it.
3. **It could not show you anything.** Several candidates, per-field sources,
   provenance and confidence do not fit into one scraped-scene return value.

So the engine is a plugin, and the entry point is a **Discovery tab on the scene page**
plus a **ScrapeDiscovery page** of its own.

There *is* also a scraper, in [`scrapers/ScrapeDiscovery/`](../../scrapers/ScrapeDiscovery/):
a ~150-line shim that appears in *Scrape with…* and asks the plugin one question. It
never scrapes. It hands back an answer only when independent sources already agreed on
one, otherwise it queues a scan or says how many answers are waiting for review. Install
it if you like reaching for that menu; skip it and nothing is lost.

---

## Installation

Add this repository as a plugin source in Stash (**Settings → Plugins → Add Source**),
install **ScrapeDiscovery**, then reload plugins. Nothing else is required: the plugin
uses only the Python standard library, because the interpreter Stash shells out to is
often a bare system install.

Verified against **Stash v0.31.1**. Earlier versions are untested; the plugin needs
`listScrapers`, `runPluginOperation`, `bulkSceneUpdate` and the UI plugin API
(`PluginApi.register.route`).

After installing, run **Settings → Tasks → ScrapeDiscovery → Show effective settings**.
It logs the configuration exactly as discovery will read it, what is installed, and
where the database went. It is the first thing to check whenever something is odd.

---

## Where the database lives

By default:

```
<stash config dir>/scrape-discovery/scrape-discovery.sqlite
```

**Not** in the plugin directory, deliberately: installing a newer version of a plugin
package deletes the files the previous version installed. Set `databasePath` if you
want it elsewhere. Stash's own database is never opened, and its schema is never
touched.

---

## Using it on one scene

Open a scene, choose the **Discovery** tab, and press **Discover**.

The scan runs as a normal Stash job, so it has a progress bar and a stop button in the
job queue, and you can leave the page. When it finishes, the tab shows what was found
and links to the full discovery view.

| | |
| --- | --- |
| **Discover** | The scrapers likely to know this scene: the URLs already on it, your stash-boxes by fingerprint, high-priority scrapers, the scrapers that own the scene's sites, then the rest of the fragment scrapers. |
| **Deep scan** | Every enabled scraper, name searches included, ignoring early-stop conditions. Slow — see the timings below. |

## Using it on a hundred scenes

1. Tag the scenes you cannot identify with your input tag (`needs_scrape` by default).
2. Run **Settings → Tasks → ScrapeDiscovery → Discover tagged scenes**.
3. Leave it. Come back to the **ScrapeDiscovery** page in the main menu.

The inbox groups scenes by what happened, so you can spend your attention on the ones
worth reviewing instead of opening each in turn.

---

## What a scan actually does

```
scene
  ├─ URLs already on the scene      → whichever installed scraper matches each one
  ├─ stash-boxes                    → fingerprint lookup (oshash / phash)
  ├─ fragment scrapers              → the site's own scraper first, then the rest
  └─ name search (deep scan only)   → the title, or the filename cleaned up
        │
        └─ every answer is inspected for URLs, which are recorded with the
           scrapers that could follow them (following them is phase 2)
```

Every attempt is stored with what was asked, of whom, when, how long it took, and
what came back verbatim. Nothing is thrown away — including the no-matches, which are
what stop the next scan asking the same question again.

### What the numbers look like in practice

Measured on a live library with 780 installed scene scrapers (186 fragment-capable,
201 name-capable), at the default concurrency of 3:

| | |
| --- | --- |
| Normal scan, one scene | 190 attempts, ~200 s |
| Deep scan, one scene | 393 attempts, several minutes |
| Typical outcome for one scene | 21 matched, 64 found nothing, 106 failed |
| Second scan of the same scene | 0 external requests, ~0.01 s |

Over half of the attempts failing is **normal**, not a problem: most of those are
scrapers being handed a scene from a site they have nothing to do with. They are
recognised as permanent failures and remembered for as long as a no-match, so they are
not retried tomorrow. Transient failures — timeouts, connection resets, 5xx — are
retried much sooner.

---

## The Scrapers page

**ScrapeDiscovery → Scrapers** is the payoff of storing every attempt: a table of what
each installed scraper has actually done for you.

| | |
| --- | --- |
| Attempts / Match / None / Err / T-o | Counted from stored history, ignoring cache hits — a reused answer says nothing new about a scraper. |
| Rate | Matches per attempt. |
| Avg | How long it takes, which is what makes a useless scraper expensive. |
| **How it fails** | The shape of its typical failure, how often, and whether that is permanent or transient. |
| Last used | |

Sort by **Time wasted** — attempts that found nothing, weighted by how long they took —
to see what is costing you most for nothing.

The failure column is the useful one. Raw messages embed the URL that was fetched, so
they are grouped by signature: on one real scan, 101 failures across 187 scrapers came
down to seven shapes.

```
x24  permanent  scraper script error: exit status #
x18  permanent  failed to load URL "...": http error #:Not Found
x9   permanent  Internal system error. Error <runtime error: invalid memory address...>
x9   transient  could not unmarshal json from script output: EOF
x8   permanent  failed to load URL "...": http error #:Bad Request
x7   transient  failed to load URL "...": exec: "...": executable file not found in $PATH
x5   transient  timed out after #s
```

Which tells you three different things: a 404 on a query URL means the scraper has
nothing to do with your library, a non-zero exit means it is broken, and
`executable file not found in $PATH` means it needs something you have not installed.
Hover any row for the verbatim message and the last time it happened.

### Uninstalling a scraper

Each row offers **Uninstall**, which removes the package through Stash's own package
manager. It asks first, showing what will be deleted, where it came from, and what the
scraper has found for you — and the backend refuses unless the confirmation echoes the
scraper's own id, so a mis-click in a table of 780 rows cannot do it.

- **The history is kept.** It is the record of what was tried and why it was not worth
  keeping, and its results may be part of another candidate's evidence.
- **Only packages Stash installed can go.** A scraper copied in by hand, or a built-in,
  says so instead of offering a button that would not work.
- **ScrapeDiscovery's own entry point is refused**, since that would be removing the
  thing you are using.
- Stash removes the files in a job, so the scraper stays loaded until you press
  **Reload scrapers** afterwards.

## Configuration

Both Stash's own plugin settings panel and the ScrapeDiscovery settings page write to
the same store, so they cannot disagree. The panel shows the scalar settings; the
ScrapeDiscovery page also shows the structured ones (per-scraper priorities, scoring
weights) which Stash's three setting types cannot express.

The ones worth knowing about:

| Setting | Default | |
| --- | --- | --- |
| `maxConcurrency` | 3 | Scrapers in flight. Each is a request that may launch a browser. |
| `defaultTimeout` | 30 s | Per attempt. Abandoning the request also kills the scraper process on the server, which is the only way to stop a hung scraper. |
| `maxDepth` | 2 | How far to follow URLs found inside results. |
| `maxUrlsPerScan` | 40 | |
| `maxAttemptsPerScan` | 250 | |
| `sceneTimeBudget` | 900 s | A scene's scan stops after this, keeping everything found so far. |
| `normalIncludesNameScrapers` | off | Name search is the slowest kind of attempt and mostly returns near-misses. Deep scan always runs it. |
| `excludedScrapers` | 8 local utilities | `Filename`, `FileMetadata`, `CopyMetadata`, autotag and friends: they answer every fragment scrape with something derived from the file you already have, which is not discovery. Clear the field to include them. |
| `manageTags` | off | While it is off, discovery never touches a scene at all, not even its tags. |
| `autoApply` | off | |
| `debugLogging` | off | Logs every attempt's input, cache decision and timing. A deep scan is hundreds of attempts, so it is noisy. |

### Cache

| Result | Reused for |
| --- | --- |
| Match | 90 days |
| No match | 30 days |
| Permanent error | 30 days (it is a no-match in everything but name) |
| Transient error | 1 day |
| Timeout | 3 days |

A cache entry is also invalidated when the scraper itself changes. Scraper configs
carry no version, so ScrapeDiscovery fingerprints each scraper's name, capabilities and
URL patterns; a change to any of those means the scraper has not been tried in its
current form. That is the same mechanism behind **Scan with newly installed scrapers**.

---

## Tasks

| Task | |
| --- | --- |
| Discover scenes | Specific scenes. Started from the UI; running it from the Tasks page without a scene does nothing. |
| Discover tagged scenes | Every scene carrying the input tag. The main batch workflow. |
| Discover unresolved scenes | Scenes on record with no candidates yet. |
| Retry failed discoveries | Re-runs failures, ignoring the cached errors that would otherwise skip them. |
| Scan with newly installed scrapers | Unresolved scenes, with scrapers they have never been tried with. |
| Show effective settings | Diagnostics. Run this first. |
| Clear expired cache | Drops expired attempts that returned nothing. Attempts that returned results are never touched. |
| Delete old history | Honours the retention setting. |
| Vacuum database | Compacts, and drops images nothing references. |

---

## What works today

**Working now (phase 1, plus the scraper shim)**

- single-scene and batch scanning, as Stash jobs with progress and cancellation
- all six ways of invoking a scraper: URL, fragment by scene, synthetic fragment,
  name search, and stash-box by fingerprint or query
- raw and normalised storage of every answer, with images stored once and out of line
- attempt caching with per-status TTLs and error classification
- discovered URLs recorded with the scrapers that could follow them
- the inbox, the scene discovery view, scan history, settings and diagnostics
- a per-scraper table: attempts, matches, failures grouped by shape, time wasted, and
  uninstalling a scraper that is earning its keep
- a conservative consensus - what independent sources agree on - shown on the scene page
  and offered through the scraper shim's merge dialog
- **no writes to Stash at all**

**Not yet**

- following discovered URLs during a scan (the code is there and tested; it is off by
  default until phase 2 turns it on)
- correlating answers into candidates, and scoring them — the scene view currently
  lists the raw answers grouped by source, which is honest but more work to read
- field-by-field comparison, change preview and applying metadata
- tag automation and auto-apply (the settings exist and are inert)

---

## Troubleshooting

**Nothing appears in the main menu.** The UI is injected as a plugin script; check the
plugin is enabled, then hard-reload the page. `[ScrapeDiscovery] UI loaded` in the
browser console means the script ran.

**"the plugin returned nothing - is it enabled?"** The page reached Stash but Stash
could not run the plugin. Check Settings → Logs for a Python error, and that Stash's
configured Python can run `plugins/ScrapeDiscovery/ScrapeDiscovery.py`.

**A scan says most attempts failed.** Expected — see the numbers above. What matters is
the match count. Turn on `debugLogging` to see each failure.

**A scan is stuck.** Stop the job from Stash's queue. That kills the process, and
ScrapeDiscovery notices next time anything opens the database and marks the scan
cancelled. Everything the scan had already finished is kept.

**One scraper hangs every time.** Give it a shorter timeout, or set its priority to
`disabled`, in `scraperOverrides`.

---

## Development

Tests run without a Stash server:

```bash
pip install pytest pyyaml
python -m pytest tests/
```

The architecture, the API feasibility findings behind it, and the schema are in
[`docs/architecture.md`](../../docs/architecture.md). Two notes for anyone reading the
code:

- `scrapediscovery/stash.py` is the only module containing GraphQL, and
  `scrapediscovery/db/repo.py` the only one containing SQL. Everything else deals in
  plain dicts.
- this plugin does not use the repository's shared `stash_common` helpers, on purpose:
  it needs per-call timeouts, a distinction between a timeout and an error, and a
  cached schema probe. Bending the shared helper to fit would have changed behaviour
  for the ScrapeAll scraper that already depends on it.
