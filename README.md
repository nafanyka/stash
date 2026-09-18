# stash-tools

Monorepo of [Stash](https://github.com/stashapp/stash) plugins and scrapers,
published as an installable Stash **source** via GitHub Pages.

## Layout

```
plugins/<Name>/<Name>.yml     plugin manifest (+ .py / .js / README.md next to it)
scrapers/<Name>/<Name>.yml    scraper manifest (+ .py next to it)
  scrapers/FastDiscovery/     thin entry point into the FastDiscovery plugin
  scrapers/Babepedia/         performer scraper, from CommunityScrapers - kept here
                              after it stopped receiving updates from that source
  scrapers/IAFD/              performer/scene/movie scraper, from CommunityScrapers -
                              same story; needs py_common installed separately
  plugins/FastDiscovery/      runs every stash-box and every URL scraper reachable
                              from a scene, then one review table; nothing is
                              written until you apply, and the results are then gone
  plugins/PerformerOrganized/ the Organized flag scenes have, for performers - kept
                              in the performer's own custom_fields, so the standard
                              Performers filter selects on it server-side
  plugins/MyPerformerBodyCalculator/  a fork of stg-annon's Performer Body Calculator:
                              reads metric bust sizes correctly, and splits the run into
                              Add New and Full Update
  plugins/path_vr_tagger/     tags a scene VR/NonVR from its file path, on save
  plugins/performer-url-cleanup/  normalises, deduplicates and sorts performer URLs
  plugins/stashdb-tag-sync/   syncs tags from StashDB into the local instance
common/python/stash_common/   shared helpers, bundled into packages that import them
docs/                         architecture and dev notes for FastDiscovery
tests/                        pytest suite, no Stash server needed
dist/plugins/index.yml        generated source index — the URL Stash subscribes to
dist/scrapers/index.yml       generated source index
.github/workflows/publish.yml CI: build zips + indexes, deploy to GitHub Pages
.github/workflows/build_index.py  the packager, runnable locally
bin/build.sh                  rebuild dist/ after a change — see bin/README.md
```

After editing a plugin or scraper, run **`bin/build.sh`** (from Git Bash on Windows). It
reports what changed, runs the tests, checks the plugin JavaScript parses, repackages
`dist/`, and remembers what it built so the next run can tell you what moved. Commit the
source and `dist/` together.

**FastDiscovery** answers one question — *what does anything I have installed know
about this scene (or performer)?* It asks every configured stash-box, then every
scraper reachable from there through URLs, recursively, and puts every answer in one
review table, one column per source. Nothing is written until you apply, and the
results are deleted the moment you decide. See
[`plugins/FastDiscovery/README.md`](plugins/FastDiscovery/README.md).

Separate from that, and about performers rather than scenes:
**[PerformerOrganized](plugins/PerformerOrganized/README.md)** adds the *Organized*
flag Stash gives scenes, galleries and images to performers as well — a switch on the
performer page, an icon on the card, bulk *Set organized*, and a server-side
`Organized = Yes/No` filter in the standard Performers list. It stores the flag in the
performer's own `custom_fields`, so nothing about it depends on the plugin staying
installed.

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

Covers FastDiscovery (`test_fd_*.py`) and needs no Stash server: the Stash API is
faked, so the engine, the merge matrix and the apply path are all exercised offline,
including every acceptance case from its specification.

## Building locally

```bash
pip install pyyaml
python .github/workflows/build_index.py
```

Writes `dist/<kind>/<Name>.zip` and regenerates both `index.yml` files. Commit
the result. Each entry's `version` is `<manifest version>-<short sha of the last
commit touching that folder>`, so a rebuild before committing shows the *previous*
sha — harmless for Stash's update check, which only compares strings.
