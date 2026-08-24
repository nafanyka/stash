# Developing ScrapeDiscovery

## Layout

```
plugins/ScrapeDiscovery/
  ScrapeDiscovery.yml       manifest: exec, ui, tasks, settings
  ScrapeDiscovery.py        entry point: reads stdin, dispatches, prints one JSON object
  scrapediscovery/
    settings.py             configuration schema, defaults, validation
    logs.py                 the Stash stderr log protocol
    stash.py                the Stash API adapter — the only GraphQL in the plugin
    registry.py             installed scrapers, URL routing, work-list planning
    normalize.py            URL and result normalisation, fingerprinting
    cache.py                TTL policy and error classification
    executor.py             bounded concurrency, deadlines, early stops
    engine.py               one scene's scan
    ops.py                  the operations the UI calls
    tasks.py                the tasks Stash's job queue runs
    db/migrations.py        numbered forward migrations
    db/repo.py              the only SQL in the plugin
  ui/scrapediscovery.js     the page, in plain JS against PluginApi
  ui/scrapediscovery.css
tests/                      pytest, no Stash server required
```

Two boundaries are worth keeping:

- **`stash.py` owns GraphQL, `db/repo.py` owns SQL.** Everything else passes plain
  dicts. That is what lets the schema change without the engine noticing, and what
  makes the engine testable against a fake client.
- **Acquisition is separate from everything after it.** `normalize`, `correlate` and
  `score` are pure functions of stored data. If a change to one of them needs a
  network request to take effect, the separation has been broken.

## Running the tests

```bash
pip install pytest pyyaml
python -m pytest tests/ -q
```

No Stash server, no network. `tests/conftest.py` provides a `FakeClient` whose canned
responses can be exceptions, which is how the error and timeout paths are exercised.

`tests/test_manifest.py` checks the manifest against the code — every declared task has
a handler, every declared setting exists, the injected UI files exist. Stash matches
tasks and settings by exact string, so a rename on one side only produces a control that
silently does nothing.

## Running it by hand

The plugin reads its input from stdin exactly as Stash provides it, so it can be driven
without Stash:

```bash
echo '{"server_connection":{"Scheme":"http","Host":"localhost","Port":9999,
       "Dir":"/tmp/sd"},"args":{"op":"ping"}}' | python ScrapeDiscovery.py
```

Operations take `{"op": "..."}`, tasks take `{"task": "..."}`. Human-readable output goes
to stderr with the Stash log prefix; stdout is exactly one JSON object, because that is
what Stash parses as the result.

Without a `server_connection` the entry point falls back to `STASH_URL` and
`STASH_API_KEY`, which is convenient for a local loop.

## Working on the UI

`ui/scrapediscovery.js` is plain ES5-ish JavaScript with no build step. Stash loads
plugin scripts as classic `<script>` tags before the router mounts
(`ui/v2.5/src/plugins.tsx`), so there is no module system to target, and this repository
has no Node toolchain in CI. Components use `PluginApi.React.createElement` through a
local `h` alias — the same thing JSX compiles to.

```bash
node --check plugins/ScrapeDiscovery/ui/scrapediscovery.js
```

The patch points used, all confirmed present in v0.31.1's `pluginApi.d.ts`:

| | |
| --- | --- |
| `register.route` | the page, at `/scrape-discovery` |
| `MainNavBar.MenuItems` | the main menu entry |
| `ScenePage.Tabs` / `ScenePage.TabContent` | the Discovery tab |

The route must not live under `/plugin/…`: that prefix is mounted by the Stash server,
so a client route there 404s on a hard reload. Verified against a live instance.

Iterating on the UI against a running Stash means editing the file in Stash's own
plugins directory (or symlinking), then **Settings → Plugins → Reload** and a hard
refresh. There is no hot reload.

## Adding a migration

Append a new list of statements to `MIGRATIONS` in `db/migrations.py`. Never edit a
released step — somebody's database has already run it. Additive changes only: a new
table, a new column with a default, a new index. If the meaning of derived data changes,
bump the matching version constant instead of rewriting rows, and let the reprocessing
task rebuild them from the raw results.

## Adding a scoring signal

1. Add a weight to `DEFAULT_SCORE_WEIGHTS` in `settings.py`.
2. Compute a 0–1 strength in `score.py`, and mark it *not applicable* rather than 0 when
   the comparison cannot be made — the score normalises over applicable signals only, so
   a scene with no existing metadata is not punished for what could not be compared.
3. Bump `SCORING_VERSION`.
4. Run `Recalculate confidence`. No external requests.

## Checking against a real Stash

Anything about the Stash API should be confirmed rather than assumed. The GraphQL
endpoint answers introspection, which is faster and more reliable than reading docs:

```bash
curl -s -X POST http://<host>:9999/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ __type(name:\"ScrapedScene\"){ fields{ name } } }"}'
```

Findings that shaped this design, with the source behind each, are in
[`architecture.md`](architecture.md) §1.2. When something looks impossible, check there
first — several things that look impossible are, and have a documented workaround.

## Style

Match the surrounding code: standard library only, `from __future__ import annotations`,
module docstrings that say *why* rather than restating the code, and comments reserved
for decisions a reader would otherwise have to guess at. Scraper output is untrusted
everywhere — parameterised SQL, no `eval`, no HTML from a scraped string, and a URL is
only ever a link after its scheme has been checked.
