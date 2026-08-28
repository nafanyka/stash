# FastDiscovery (scraper)

An entry point into the [FastDiscovery plugin](../../plugins/FastDiscovery/README.md), and
nothing else. It contains no scraping logic and no discovery logic; it asks the plugin one
question and prints `null`.

## Why it always returns nothing

A Stash scene scraper's return value is what Stash offers to save, and *Identify* saves it
without showing you anything. FastDiscovery exists so that every source's answer is put in
front of you *before* a scene changes. Handing a merged scene back here would undo that, so
this never does.

Being a scraper is still worth it: it puts FastDiscovery in the scene edit panel's
**Scrape with…** menu, where people already reach for it. Choosing it there starts a
FastDiscovery run for the scene — or tells you one is already running, or that results are
already waiting — and points you at the scene's FastDiscovery tab. Nothing is written.

It is safe to leave selected as an *Identify* source for the same reason: it returns
nothing, so Identify has nothing to save.

## Requirements

The **FastDiscovery plugin**, installed from the same source and enabled. If it is missing
or disabled the scraper says so in the log rather than failing obscurely — asking Stash to
run a plugin it does not have crashes the server's resolver, so this checks first.

## Configuration

Usually none. Scrapers, unlike plugins, are not handed the server's connection details, so
if Stash is not on `localhost:9999`, or has authentication enabled, copy
`config.ini.example` to `config.ini` next to the script and fill it in. `STASH_URL` and
`STASH_API_KEY` in the environment take precedence.

## Not to be confused with

- **ScrapeAll** — a scraper that probes many sources and returns one merged scene for Stash
  to save. Useful when you expect one good answer.
- **ScrapeDiscovery** — an orchestrator that probes far more sources, including every
  fragment and name scraper installed, and scores what comes back.
- **FastDiscovery** — every stash-box plus everything reachable from the scene's URLs,
  compared side by side and applied by hand.
