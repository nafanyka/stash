# ScrapeAll

Runs every non-URL scene scraper against one scene and logs what each one
returned. A diagnostic: it **writes nothing** and returns nothing.

Two phases, both log-only:

1. **Inventory** — every scraper Stash has loaded, grouped by category.
2. **Probe** — for each source that can work from the scene itself, call
   `scrapeSingleScene` and log the outcome.

## Where it shows up

`ScrapeAll.yml` declares a single action, `sceneByFragment`, which is exactly the
capability Stash uses to populate both places at once:

| Место | Как открыть |
| --- | --- |
| Scene edit panel | Scene -> Edit -> **Scrape with...** -> `ScrapeAll` |
| Batch identify | Settings -> Tasks -> **Identify** -> Add source -> `ScrapeAll` |

Both call `ScrapeAll.py scene-fragment` with the scene fragment on stdin, from
which only the scene id (and the title, if set) is used.

## What gets probed

| Source | Mode | Matches on |
| --- | --- | --- |
| Scene scraper with `FRAGMENT` | `scrapeSingleScene(scene_id:)` | whatever the scraper reads off the scene — stash-id, phash, oshash, title, duration |
| Scene scraper with `NAME` | `scrapeSingleScene(query:)` | the scene title, or its filename when the title is empty |
| Every configured stash-box | `scrapeSingleScene(scene_id:)` | fingerprints — stash-id, phash, oshash, duration |

A scraper supporting several non-URL modes is probed once per mode, so the log
shows which mode produced which result.

**Skipped, silently:** URL-bound scrapers (`U` flag only). They need a URL on the
scene rather than the scene itself — out of scope for now, and not reported.

**Skipped, always:** `ScrapeAll` itself. Stash takes a scraper's id from its yml
filename, so the probe drops that id — otherwise ScrapeAll would call itself
through the server and recurse without limit. An environment marker would not
help: the nested run is spawned by Stash, not by this process.

## Reading the output

Everything lands in **Settings -> Logs**. Levels are the whole reporting
mechanism:

| Level | Line | Meaning |
| --- | --- | --- |
| `Info` | `FOUND [3/12] Name (fragment) after 1.4s` | результат есть, ниже — его сводка |
| `Error` | `MISS  [4/12] Name (fragment) after 0.8s` | скраппер ответил, но ничего не нашёл |
| `Error` | `FAIL  [5/12] Name (fragment) after 0.1s: …` | скраппер упал или GraphQL вернул ошибку |
| `Error` | `BUDGET exhausted: 300s spent - 4 source(s) not tried` | probe не успел пройти весь список |

```
== PROBE: 7 non-URL source(s) ==========================================
FOUND [1/7] FragSite (fragment) after 2.1s
        - title="A Found Scene"  date="2020-05-01"  code="ABC-123"  studio="Fragsite Studio"  performers=2  tags=7  url=https://fragsite.com/s/1  details=240ch
FOUND [2/7] NameSite (name search "Real Title Here") after 3.4s (12 results)
        - title="Search hit #1"  date="2019-02-01"  studio="S"  performers=1  url=https://x/1
        - ... 2 more not listed
FAIL  [5/7] BrokenSite (fragment) after 0.1s: scraper BrokenSite: exit status 1
FOUND [6/7] StashDB (stash-box fingerprints) after 1.2s
MISS  [7/7] ThePornDB (stash-box fingerprints) after 0.9s
PROBE DONE: found 5, missed 1, failed 1 of 7. Nothing was written to the scene.
```

Nothing appears in the UI where you launched it: the scraper returns an empty
result, so the edit panel reports no results and Identify counts the scene as
unmatched. That is the expected outcome, and it is what makes it safe to point at
anything — there is no field for Stash to write, whatever the Identify field
options say.

## Cost

The probe is sequential and the UI blocks on it: a scrape that fans out to N
scrapers takes as long as all N together. Two limits in `ScrapeAll.py`:

```python
PER_SOURCE_TIMEOUT = 60   # per source
TOTAL_BUDGET = 300        # whole probe phase, then it stops and says so
MAX_RESULTS_LOGGED = 10   # a name search can return a long list
```

Running it from Identify across a large library multiplies that by the number of
scenes. Try one scene first and read the timings before doing anything wider.

## Where the data comes from

1. **GraphQL** (preferred) — `listScrapers` plus `configuration.general.stashBoxes`
   against the local server, so the list is exactly what Stash loaded.
2. **Disk scan** (fallback) — sibling folders in the scrapers directory, parsed
   with a regex for `name:` and `<kind>By<Mode>:` keys. Inventory only: with no
   server reachable there is nothing to probe, and the log says so.

Which `ScrapedScene` fields exist differs by Stash version (`url` became `urls`,
`movies` became `groups`, `code`/`director` are newer), and asking for a field
that is gone fails the whole query — so the field list, the stash-box source key
(`stash_box_endpoint` vs the older `stash_box_index`) and the scrape input shape
are read from GraphQL introspection once per run.

Because Stash does not hand scrapers the server connection details, all of this
needs to be told where to look if anything is non-default:

```ini
; config.ini, next to ScrapeAll.py
[stash]
url = http://localhost:9999/graphql
api_key = eyJhbGciOi...
```

`STASH_URL` / `STASH_API_KEY` in the environment take precedence. Without either,
it tries `http://localhost:9999/graphql` unauthenticated — fine unless Stash has
authentication enabled, where an API key (Settings -> Security) is required.

## Requirements

Python 3.8+, standard library only.

## Running it outside Stash

```bash
cd stash-tools
echo '{"id":"42"}' | PYTHONPATH=common/python python scrapers/ScrapeAll/ScrapeAll.py
```

The report is on stderr, each line carrying Stash's log-level prefix
(`\x01i\x02` for Info, `\x01e\x02` for Error); stdout is just `null`. Decode the
prefixes for reading:

```bash
... 2>&1 >/dev/null | sed -e 's/\x01i\x02/[Info ] /' -e 's/\x01e\x02/[Error] /'
```

`stash_common` is only copied next to the script when the package is built, so a
source checkout needs `PYTHONPATH`.
