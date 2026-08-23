# stash-tools

Monorepo of [Stash](https://github.com/stashapp/stash) plugins and scrapers,
published as an installable Stash **source** via GitHub Pages.

## Layout

```
plugins/<Name>/<Name>.yml     plugin manifest (+ .py / .js / README.md next to it)
scrapers/<Name>/<Name>.yml    scraper manifest (+ .py next to it)
  scrapers/ScrapeAll/         lists every loaded scraper, grouped by category
common/python/stash_common/   shared helpers, bundled into packages that import them
dist/plugins/index.yml        generated source index — the URL Stash subscribes to
dist/scrapers/index.yml       generated source index
.github/workflows/publish.yml CI: build zips + indexes, deploy to GitHub Pages
.github/workflows/build_index.py  the packager, runnable locally
```

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

Settings → Plugins → Available Plugins → **Add Source**:

- Plugins: `https://<user>.github.io/<repo>/plugins/index.yml`
- Scrapers: `https://<user>.github.io/<repo>/scrapers/index.yml`

Enable GitHub Pages for the repo once (Settings → Pages → Source: **GitHub Actions**).

## Building locally

```bash
pip install pyyaml
python .github/workflows/build_index.py
```

Writes `dist/<kind>/<Name>.zip` and regenerates both `index.yml` files. The zips
are gitignored; the indexes are committed so the Pages artifact is always
reproducible.
