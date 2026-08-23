# ScrapeAll

Lists every scraper the running Stash has loaded, grouped by category.

A diagnostic, not a metadata source. It contacts no remote site and **writes
nothing** — it answers "what is actually installed, and where can each one be
used?" without walking `Settings -> Metadata Providers` row by row.

## Where it shows up

`ScrapeAll.yml` declares a single action, `sceneByFragment`, which is exactly the
capability Stash uses to populate both places at once:

| Место | Как открыть |
| --- | --- |
| Scene edit panel | Scene -> Edit -> **Scrape with...** -> `ScrapeAll` |
| Batch identify | Settings -> Tasks -> **Identify** -> Add source -> `ScrapeAll` |

Both call `ScrapeAll.py scene-fragment` with the scene fragment on stdin.

## Reading the output

The report goes to **Settings -> Logs** at `Info` level. Nothing appears in the
UI where you launched it: the scraper returns an empty result, so the edit panel
says it found no results and Identify counts the scene as unmatched. That is the
expected outcome, and it is what makes the scraper safe to point at the whole
library — there is no field for Stash to write, whatever the Identify field
options say.

```
ScrapeAll - installed scraper inventory (log only, nothing written).

Source: http://localhost:9999/graphql via Stash 0.28.1, listScrapers(scene, group, performer, gallery, image)
Total scrapers: 47

== SCENE (23) ==========================================================
   19 of these are usable as Identify sources (F flag).
  NFU  ExampleSite
          examplesite.com, www.examplesite.com
  -F-  ScrapeAll
...
------------------------------------------------------------------------
Flags: N = by name (Tagger)  F = by fragment (edit panel + Identify)  U = by URL
```

Categories are the Stash content types — SCENE, GROUP/MOVIE, PERFORMER, GALLERY,
IMAGE — and a scraper appears under each category it supports.

The scene fragment on stdin is only used for the one log line naming which scene
triggered the run, so launching it from any scene gives the same inventory.

## Where the data comes from

1. **GraphQL** (preferred) — `listScrapers` against the local server, so the list
   is exactly what Stash loaded, including scrapers whose yml failed to parse and
   are therefore absent.
2. **Disk scan** (fallback) — sibling folders in the scrapers directory, parsed
   with a regex for `name:` and `<kind>By<Mode>:` keys. Kicks in when the server
   is unreachable or refuses the request; the report's `Source:` line says which
   path was taken.

Because Stash does not hand scrapers the server connection details, step 1 needs
to be told where to look if anything is non-default:

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
echo '{"id":"1"}' | PYTHONPATH=common/python python scrapers/ScrapeAll/ScrapeAll.py
```

The report is on stderr (each line carrying Stash's log-level prefix); stdout is
just `null`. `stash_common` is only copied next to the script when the package is
built, so a source checkout needs `PYTHONPATH`.
