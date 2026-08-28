# stash-tools

Monorepo of [Stash](https://github.com/stashapp/stash) plugins and scrapers,
published as an installable Stash **source** via GitHub Pages.

## Layout

```
plugins/<Name>/<Name>.yml     plugin manifest (+ .py / .js / README.md next to it)
scrapers/<Name>/<Name>.yml    scraper manifest (+ .py next to it)
  scrapers/ScrapeAll/         probes every non-URL scene source, merges the hits
  scrapers/ScrapeDiscovery/   thin entry point into the ScrapeDiscovery plugin
  scrapers/FastDiscovery/     thin entry point into the FastDiscovery plugin
  plugins/ScrapeAllSettings/  the settings ScrapeAll obeys (no scraping of its own)
  plugins/ScrapeDiscovery/    runs many installed scrapers per scene, keeps every
                              answer in its own database, changes nothing
  plugins/FastDiscovery/      runs every stash-box and every URL scraper reachable
                              from a scene, then one review table; nothing is
                              written until you apply, and the results are then gone
common/python/stash_common/   shared helpers, bundled into packages that import them
docs/                         architecture, schema and dev notes for both plugins
tests/                        pytest suite, no Stash server needed
dist/plugins/index.yml        generated source index — the URL Stash subscribes to
dist/scrapers/index.yml       generated source index
.github/workflows/publish.yml CI: build zips + indexes, deploy to GitHub Pages
.github/workflows/build_index.py  the packager, runnable locally
```

Three answers to one question — *what does anything know about this scene?* — from
different directions:

| | Asks | Returns | Writes |
| --- | --- | --- | --- |
| **ScrapeAll** | every non-URL scene source | one merged scene, for Stash's own dialog | when you save the dialog |
| **ScrapeDiscovery** | far more sources, including every fragment and name scraper | scored candidates in its own database | nothing until you pick |
| **FastDiscovery** | every configured stash-box, then every scraper reachable from the scene's URLs, recursively | one review table, one column per answer | nothing until you apply, and the results are deleted the moment you decide |

Use ScrapeAll when you expect one good answer, ScrapeDiscovery when you do not know which
scraper — if any — knows the scene, and FastDiscovery when you want everything your
install knows about a scene in front of you at once. See
[`plugins/ScrapeDiscovery/README.md`](plugins/ScrapeDiscovery/README.md) and
[`plugins/FastDiscovery/README.md`](plugins/FastDiscovery/README.md).

One `.yml` per folder, named after the folder. Stash scans the scrapers directory
recursively and tries to load **every** `.yml` it finds as a scraper config, so a
stray sidecar yml becomes a broken scraper in the UI.

## Adding a plugin or scraper

1. Create `plugins/MyPlugin/` (or `scrapers/MyScraper/`). **The folder name is the
   id** — keep it CamelCase, no spaces.
2. Add a manifest named exactly after the folder: `MyPlugin/MyPlugin.yml`, with at
   least `name`, `description`, `version`.
3. Drop the implementation beside it (`MyPlugin.py`, `MyPlugin.js`, …) and a
   `README.md` describing settings and requirements.
4. To reuse the shared helpers, just `import stash_common`. The build detects the
   import and copies `common/python/stash_common/` into that package's zip, so it
   sits next to the script at runtime. Nothing to declare.
5. Push to `main`. CI packages everything and republishes the indexes.

### Index metadata

Plugin manifests carry `name`, `description` and `version` natively. **Scraper**
configs have no such fields and Stash rejects unknown keys in them, so the build
also reads them from comments the yml parser drops:

```yaml
# version: 1
# description: One line, shown in Stash's available-scrapers list.
name: MyScraper
```

## Installing in Stash

In Stash: Settings → Metadata Providers → Available Scrapers (or Plugins) →
**Add Source**, then paste an index URL. Two routes, pick one:

**Straight from the repo** (no GitHub configuration needed):

```
https://raw.githubusercontent.com/nafanyka/stash/refs/heads/main/dist/scrapers/index.yml
https://raw.githubusercontent.com/nafanyka/stash/refs/heads/main/dist/plugins/index.yml
```

`dist/` — both the indexes **and** the `.zip` packages — is committed for exactly
this reason: Stash resolves each entry's `path` relative to the index URL, so the
zip has to be a real file next to it in the branch. Run the build before every
commit that touches a plugin or scraper, or the published zip goes stale.

**Via GitHub Pages** (CI builds, nothing binary to commit):

```
https://<user>.github.io/<repo>/scrapers/index.yml
https://<user>.github.io/<repo>/plugins/index.yml
```

Needs Settings → Pages → Source: **GitHub Actions** enabled once; `publish.yml`
then rebuilds and deploys `dist/` on every push to `main`.

The two are independent — each index is served next to its own zips, so neither
can hand Stash a package that mismatches its `sha256`.

## Tests

```bash
pip install pytest pyyaml
python -m pytest tests/
```

Covers ScrapeDiscovery and FastDiscovery, and needs no Stash server: the Stash API is
faked, so the engines, caching, normalisation, the merge matrix and the apply path are
all exercised offline. FastDiscovery's tests are `test_fd_*.py` and include every
acceptance case from its specification.

## Building locally

```bash
pip install pyyaml
python .github/workflows/build_index.py
```

Writes `dist/<kind>/<Name>.zip` and regenerates both `index.yml` files. Commit
the result. Each entry's `version` is `<manifest version>-<short sha of the last
commit touching that folder>`, so a rebuild before committing shows the *previous*
sha — harmless for Stash's update check, which only compares strings.
