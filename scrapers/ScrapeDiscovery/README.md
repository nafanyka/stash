# ScrapeDiscovery (scraper)

The ScrapeDiscovery **plugin** does the work. This is a ~150-line entry point that puts
it in Stash's own *Scrape with…* menu, and holds no discovery logic of its own.

**Requires the [ScrapeDiscovery plugin](../../plugins/ScrapeDiscovery/) to be installed
and enabled.** On its own this does nothing; it will tell you so in the log.

## What it does

Press *Scrape with… → ScrapeDiscovery* on a scene and one of four things happens:

| | |
| --- | --- |
| **Something already agreed on** | The scene comes back and Stash opens its normal merge dialog, for you to confirm or discard. |
| **Answers, none corroborated** | Nothing comes back, and the log says how many answers are stored. Open the ScrapeDiscovery page to look at them. |
| **Nothing discovered yet** | A discovery scan is queued as a Stash job. Come back when it has run. |
| **A scan is already running** | Nothing happens, and the log says so. |

It never scrapes anything itself. Every answer it hands over was already found and
stored by a previous scan.

## What "already agreed on" means

Deliberately strict, because this feeds a dialog that writes to your library. A group of
stored answers qualifies only on evidence a guessing scraper cannot manufacture:

- a **stash-box matched the file's fingerprint**, or
- a scraper read **the site's own page for a URL already on the scene**, or
- **two independent witnesses agree** — where two scrapers for the same site count as
  one witness, and everything that merely re-read a URL the scene already had counts as
  one witness between them.

Anything less is left for review. That is not caution for its own sake: on a real scene,
throwing every installed fragment scraper at it produced 21 "matches", of which three
were the scene. Among the rest, one scraper returned the server's IP address as a title,
one returned a similarly-named different film, and one returned an unrelated scene.

When it does return something, the log says exactly why:

```
[ScrapeDiscovery] returning what 4 independent source(s) agree on: a stash-box matched
                  the file's fingerprint
[ScrapeDiscovery]   sources: AnalVids, CzechVR, StashDB.org, Timestamp.trade, theporndb.net
[ScrapeDiscovery]   from 19 stored answer(s); 12 other grouping(s) rejected, 2
                    contributed no evidence
[ScrapeDiscovery]   nothing has been written to the scene - Stash's merge dialog is
                    still yours to confirm or discard
```

## A note on Identify

Because it is registered for scene fragments, this also appears as a source in
**Settings → Tasks → Identify**, which saves whatever a source returns *without review*.
That is safe here — it only ever returns a corroborated answer, and returns nothing at
all otherwise — but it is worth knowing which of the two you are using. Reviewing
candidates is what the ScrapeDiscovery page is for.

## Connection

Scrapers, unlike plugins, are not handed the server's connection details, so this has to
find Stash for itself. Resolution order, first hit wins:

1. `STASH_URL` / `STASH_API_KEY` in the environment
2. `config.ini` next to this script, then one directory up (see `config.ini.example`)
3. `http://localhost:9999/graphql` with no API key

Only step 2 or 3 usually matters, and only if Stash is not on `localhost:9999` or has
authentication enabled.

## Why a shim and not the whole thing

Three reasons, and they are the same reasons the engine lives in a plugin:

1. **A scraper runs inside the GraphQL request.** A normal scan is 190 scraper
   invocations and about three minutes; the edit panel would just sit there.
2. **A fragment scraper's contract is to hand Stash values to save**, and Identify saves
   them unreviewed. ScrapeDiscovery exists so that nothing is saved until you pick it.
3. **One scraped scene cannot express the result** — several candidates, per-field
   sources, provenance and confidence do not fit in it.

There is also a hard constraint worth knowing about if you ever rename this: the plugin
refuses to invoke a scraper with this id, always, and not as a configurable default. This
scraper's answer to being invoked is to start a scan, so a scan that invoked it would
start a scan. Stash gives a scraper no way to know it is running inside one — the nested
run is a fresh process spawned by the server — so the guard has to be unconditional.
