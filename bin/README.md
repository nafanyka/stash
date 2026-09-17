# bin/ — rebuilding the source after a change

```sh
bin/build.sh
```

That is the whole thing. Run it after editing anything under `plugins/` or `scrapers/`,
commit the result, and Stash will offer the update.

On Windows, run it from **Git Bash** (the one that comes with Git), not `cmd` or
PowerShell.

## What it does, in order

1. **Scans** every folder in `plugins/` and `scrapers/` and compares each one with what
   was there the last time it was built — the record is `bin/build-cache.json`.
   It reports each component as **new**, **version changed**, **changed without a
   version bump**, or **unchanged**.
2. **Stops right there** if nothing changed. Nothing is rewritten, nothing to commit.
3. **Runs the tests.** If they fail, `dist/` is not touched.
4. **Parses every plugin's JavaScript** with `node --check`. A syntax error in plugin UI
   code never reaches the test suite — it shows up as a page that silently refuses to
   load — so it is caught here instead. Skipped if `node` is not installed.
5. **Packages**: zips every component into `dist/` and writes `dist/plugins/index.yml`
   and `dist/scrapers/index.yml`, which are the files Stash subscribes to.
6. **Updates the cache**, so the next run knows what has already been built.

## Options

| | |
| --- | --- |
| `bin/build.sh` | build if anything changed |
| `bin/build.sh --check` | say what changed and stop; exits 1 if there is work to do |
| `bin/build.sh --force` | build even when nothing changed |
| `bin/build.sh --no-tests` | skip the test run — for a quick look, not for publishing |
| `bin/build.sh --help` | the same list |

## Making a change yourself

1. Edit the plugin or scraper.
2. **Bump the version** in its manifest — `version: 1.3.0` in a plugin's `.yml`, or the
   `# version:` comment line in a scraper's. See below for why.
3. `bin/build.sh`
4. Commit the source **and** `dist/` together.

### Why bump the version

Stash decides whether an update is available by comparing version strings. The published
version is the manifest's version with the last commit's short hash appended:

```
1.3.0-94bc585
```

So the hash alone makes the string change whenever the folder is committed, and Stash
*will* offer the update even if you forget. But the part a person reads stays `1.3.0`,
and there is then no way to tell from Stash's plugin list that anything happened. The
build script points this out rather than refusing:

```
  changed without a version bump:
    plugins/PerformerOrganized  1.0.3  files changed, version did not
```

### The hash lags one commit behind

`-94bc585` is the last commit that touched that folder — which, when you build before
committing, is the commit *before* the one you are about to make. That is harmless: what
matters is that the string differs from the previously published one, and it does.

It does mean a rebuild immediately after committing produces a different index. Do not
do that: build once, then commit. If you rebuild anyway, commit the result too.

## Adding a new plugin or scraper

Make the folder, give it a manifest named after it — `plugins/MyThing/MyThing.yml` —
and run `bin/build.sh`. It appears as **new** and is packaged with everything else. No
list to add it to.

Note that Stash takes a plugin's id from the **manifest's filename**, not from the `name`
field, and two manifests with the same filename collide however different their folders
are.

## What is in the cache file

`bin/build-cache.json` holds, per component, the version and a hash of everything that
goes into its zip — including the shared modules from `common/python/` that get bundled
in, so a change there is noticed too.

It is safe to delete. Doing so just means the next build treats everything as new, which
rebuilds everything, which is what happens anyway — the packager writes all the zips and
both indexes on every run, because an index lists every component with the sha256 of its
archive and cannot be written a piece at a time. Archives are reproducible (fixed
timestamps), so a component that did not change comes out byte for byte identical and
git shows no diff for it.

The cache is therefore not an optimisation. It is there to **tell you what moved**.

## If something goes wrong

**`python not found`** — the script needs Python for the packager. Set `PYTHON` if it is
under a different name: `PYTHON=py bin/build.sh`.

**Tests fail** — `dist/` is deliberately left alone. Fix the test, or run with
`--no-tests` if you know what you are doing and are not about to publish.

**Stash says the package hash does not match** — the index and the zip were published out
of step. Rebuild and commit both together; they are only ever valid as a pair.
