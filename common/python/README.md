# common/python

Shared Python helpers for plugins and scrapers.

| Module | Purpose |
| --- | --- |
| `stash_common.log` | Stash's stderr log protocol (`log.info`, `log.error`, `log.progress`) |
| `stash_common.config` | locate the local GraphQL endpoint + API key (env, `config.ini`, default) |
| `stash_common.graphql` | stdlib-only GraphQL client (`call`, `try_call`) |

Nothing here is published on its own. The build copies any top-level package or
module in this folder into the root of a package's zip **if that package's Python
imports it** — no manifest key to maintain. So `import stash_common` resolves
next to the script at runtime, and works in a source checkout with
`PYTHONPATH=common/python`.

Two rules keep this usable from inside Stash:

- **Standard library only.** Stash shells out to whatever `python` is on PATH,
  which is regularly a bare system install. A missing `requests` turns into an
  ImportError that the UI reports as a broken scraper.
- **Never print to stdout.** Stdout is the scrape result. Diagnostics go through
  `stash_common.log`, which writes prefixed lines to stderr.
