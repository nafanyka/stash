# ScrapeAll

Probes every non-URL scene source and returns **one merged result**, so the scene
edit panel opens its normal merge dialog with everything the installed scrapers
and stash-boxes together know about that scene.

Three phases:

1. **Inventory** — every scraper Stash has loaded, grouped by category (log only).
2. **Probe** — call `scrapeSingleScene` for each source that can work from the
   scene itself. Hits log at `Info`, misses and failures at `Error`.
3. **Merge** — combine the hits into a single scraped scene and hand it back.

What may be returned is limited by the
[ScrapeAllSettings](../../plugins/ScrapeAllSettings/) plugin.

## Where it shows up

`ScrapeAll.yml` declares a single action, `sceneByFragment`, which is exactly the
capability Stash uses to populate both places at once:

| Место | Как открыть | Что происходит с результатом |
| --- | --- | --- |
| Scene edit panel | Scene -> Edit -> **Scrape with...** -> `ScrapeAll` | стандартный диалог мержа, ничего не сохраняется до Apply |
| Batch identify | Settings -> Tasks -> **Identify** -> Add source -> `ScrapeAll` | пишется сразу, по правилам Field Options |

> ⚠ Начиная с версии 3 скраппер **пишет** в сцены. В панели редактирования это
> безопасно — решает диалог. Identify применяет молча: сначала прогоните одну
> сцену, посмотрите лог, и только потом батч.

## What gets probed

| Source | Mode | Letter | Matches on |
| --- | --- | --- | --- |
| Scene scraper with `FRAGMENT` | `scrapeSingleScene(scene_id:)` | `F` | whatever the scraper reads off the scene — stash-id, phash, oshash, title, duration |
| Scene scraper with `NAME` | `scrapeSingleScene(query:)` | `N` | the scene title, or its filename when the title is empty |
| Every configured stash-box | `scrapeSingleScene(scene_id:)` | `S` | fingerprints — stash-id, phash, oshash, duration |

A scraper supporting several non-URL modes is probed once per mode.

**Skipped, silently:** URL-bound scrapers (`U` flag only) — they need a URL on the
scene rather than the scene itself.

**Skipped, logged:** anything named in the plugin's *Ignored scrapers*.

**Skipped, always:** `ScrapeAll` itself. Stash takes a scraper's id from its yml
filename, so the probe drops that id — otherwise ScrapeAll would call itself
through the server and recurse without limit.

## How results are merged

| Field | Rule |
| --- | --- |
| `urls` | the scene's existing URLs first, then every new one. A URL already present is dropped, comparing case-insensitively and ignoring `http/https`, a leading `www.` and a trailing `/`. The result is the full accumulated list, not a replacement. |
| `performers`, `tags`, `groups` | union of every source, de-duplicated by name. The first spelling of a name wins; a `stored_id` — the link to an entity that already exists in Stash — is adopted from whichever source supplies it. |
| `studio` | a scene holds one studio, so the one the most sources agree on wins, ties going to the earliest. All candidates and their vote counts go to the log. |
| `title`, `code`, `date`, `director` | first source that returned one, in probe order. |
| `details` | **overwritten** with the provenance list: one line per source that identified the scene, as `<letter>: <name>`. |

`details` example:

```
F: FragSite
N: NameSite
S: StashDB
```

Only the **first** result of each source feeds the merge. A name search is ordered
by the scraper's own relevance, and folding a dozen candidates into one scene
would mix unrelated releases together; the rest are logged, not merged.

## Reading the log

Settings -> Logs. Levels are the reporting mechanism:

| Level | Line |
| --- | --- |
| `Info` | `FOUND [3/12] Name (fragment) after 1.4s` + a summary of what it returned |
| `Error` | `MISS  [4/12] Name (fragment) after 0.8s` — answered, found nothing |
| `Error` | `FAIL  [5/12] Name (fragment) after 0.1s: …` — crashed, or GraphQL error |
| `Error` | `BUDGET exhausted: 300s spent - 4 source(s) not tried` |

The `MERGE` block then shows how the answer was assembled:

```
== MERGE ===============================================================
identified by: F: FragSite, N: NameSite, F: BothSite, F: NoisySite, S: StashDB
urls         : 4 total (1 already on the scene, 3 new)
studio votes : 3x Acme, 2x Search Studio, 1x Other Studio
performers   : 5 - Ann Lee, Bob Roy, Name Hit 1, Cid Vic, Dee Ess
tags         : 4 - outdoor, hd, searched, indoor
settings     : whitelist details, urls
returning    : details, urls
```

If no source identified the scene, nothing is returned and the edit panel reports
no results.

## Cost

The probe is sequential and the UI blocks on it: a scrape that fans out to N
sources takes as long as all N together. Limits in `ScrapeAll.py`:

```python
PER_SOURCE_TIMEOUT = 60   # per source
TOTAL_BUDGET = 300        # whole probe phase, then it stops and says so
MAX_RESULTS_LOGGED = 10
```

## Where the data comes from

1. **GraphQL** — `listScrapers`, `configuration.general.stashBoxes`,
   `configuration.plugins` and the scene's current URLs, against the local server.
2. **Disk scan** (fallback) — sibling folders in the scrapers directory, parsed
   with a regex. Inventory only: with no server reachable there is nothing to
   probe and nothing to merge, and the log says so.

Which fields exist differs by Stash version (`url` became `urls`, `movies` became
`groups`, `code`/`director` are newer), and asking for a field that is gone fails
the whole query — so the scraped-scene field list, whether nested entities carry
`stored_id`, the stash-box source key (`stash_box_endpoint` vs the older
`stash_box_index`) and the scrape input shape are all read from GraphQL
introspection once per run. On a version whose scene holds a single `url` rather
than a list, only the first URL can be delivered, and the log warns about it.

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

stdout is the scraped scene JSON (or `null`); the report is on stderr, each line
carrying Stash's log-level prefix. Decode the prefixes for reading:

```bash
... 2>&1 >/dev/null | sed -e 's/\x01i\x02/[Info ] /' -e 's/\x01e\x02/[Error] /'
```

`stash_common` is only copied next to the script when the package is built, so a
source checkout needs `PYTHONPATH`.
