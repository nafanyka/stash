# FastDiscovery

Run **everything you have** against one scene, then decide once.

FastDiscovery asks every stash-box Stash is configured with — all of them, not the first
one that answers — then takes every URL the scene already has and every URL those answers
returned, and pushes each through every installed scraper that can read it. Whatever those
scrapers return is followed in turn, recursively, until nothing new turns up. Every answer
is kept separately, with a record of where it came from.

Then it shows you one table: the field down the side, your scene in the first column, and
one column per answer. You pick what you want and press Apply.

**Nothing is written to the scene until you do.** And once you decide, the results are
deleted — this is a review queue, not a growing archive of scraped metadata.

## What it is not

- It does not choose a best match, score anything, or rank sources.
- It does not stop after the first stash-box that identifies the scene. That is what
  Stash's own *Identify* does, and it is the reason this exists.
- It does not create performers, tags or studios it found. A name your library does not
  have shows up as a *candidate*, unticked; it is created only if you tick it.
- It does not keep a copy of your stash-box API keys. It reads the endpoints from Stash
  every time it runs, so a box you add later is used with no configuration here.

## Requirements

Stash **v0.31.x**. Python 3.8 or newer on the server's PATH (standard library only — no
pip install). Optionally the `FastDiscovery` **scraper** from the same source, which adds
an entry point under *Scrape with…*.

## Using it

**One scene.** Open the scene, go to the **FastDiscovery** tab, press *Run FastDiscovery*.
The run goes into Stash's job queue, so you can walk away. Come back to the same scene
later — the tab remembers, shows how many sources answered, and opens the review.

**Many scenes.** Select scenes in the scene list, open the operations menu and choose
**FastDiscovery**. It does not open dozens of review windows; the runs land on the
FastDiscovery page (main menu, or `/fast-discovery`), one row per scene, and you review
them when you want to.

**Reviewing.** The table gives you:

- *scalar fields* — title, date, code, director, details: click the cell you want. The
  scene's own value is selected to begin with, always;
- *lists* — performers, tags, groups, URLs, stash ids: tick what you want from the union
  of everything found. Existing entities are ticked; candidates that would have to be
  created are not;
- *the studio* — one choice, like a scalar, and again a candidate is only created if you
  pick it;
- *the image* — step through every distinct cover, or open the gallery to see them side by
  side. Identical covers from several sources appear once, with all of them credited.

Under *Sources* you get every source that was asked and what it said, including the ones
that failed. A source failing never fails a run.

**Apply** writes your selection in a single `sceneUpdate`, creates only the candidates you
ticked, and then deletes the results. **Reject** deletes them without touching the scene.
**Rescan** throws away an undecided run and starts again, after asking.

## Settings

Settings → Plugins → FastDiscovery, or the settings page at `/fast-discovery/settings`,
which additionally lists the stash-boxes it detected.

| Setting | Default | What it does |
| --- | --- | --- |
| Recursive URL discovery | on | Follow URLs found inside results, and their results in turn. |
| Max URL recursion depth | 3 | The scene's own URLs and the stash-box results are depth 0. |
| Max URLs per run | 60 | A guard against a site that links to a hundred related scenes. |
| Max sources per run | 120 | Hard ceiling on scraper invocations for one run. |
| Concurrent scraper operations | 3 | Each one is a request to Stash that may launch a browser. |
| Scraper timeout | 30 s | Abandoning the request also kills the scraper on the server. |
| Reach every scraper for a shared URL | on | See *the one thing Stash cannot do*, below. |
| Also search stash-boxes by title | off | On top of the fingerprint lookup, which always runs. Off because a name search answers with near-misses. |
| Max results per source | 3 | A source answering with a list contributes at most this many columns. |
| Time budget per scene | 900 s | Stops a run, keeping everything already found. 0 removes the limit. |
| Stale run timeout | 12 h | A run still claiming to run after this was killed, and is marked failed. |
| Database path | *(empty)* | Empty means `<stash config dir>/fast-discovery/fastdiscovery.sqlite`. Keep it out of the plugin directory: updating a plugin package deletes the files it installed. |
| Debug logging | off | Per-source input, timing and field counts. Never logs credentials. |

There is deliberately no stash-box list here. Add one in Settings → Metadata Providers and
FastDiscovery picks it up on the next run.

## The one thing Stash cannot do

When two installed scrapers both claim the same site, Stash's URL scrape picks one of them
by iterating a map — the choice is not deterministic and the response does not say who
answered. So for such a URL FastDiscovery:

- runs the ordinary URL scrape anyway, as a column honestly labelled `auto (A, B)`;
- additionally calls each of those scrapers *directly* with the URL, for every one that
  also supports fragment scraping, so it gets a column of its own;
- and lists any scraper it still could not reach as `UNREACHABLE`, with the reason, rather
  than pretending the URL was fully covered.

That, and everything else the Stash API does and does not allow, is written up in
[`docs/fastdiscovery.md`](../../docs/fastdiscovery.md).

## Tasks

Settings → Tasks:

- **Discover scenes** — what the buttons queue. Running it by hand with no scene ids does
  nothing, and says so.
- **Maintenance** — mark killed runs as failed, drop images nothing references, compact
  the database.
- **Show effective settings** — the configuration as a run will read it, the stash-boxes
  detected, and where the database lives. Run this first when something behaves oddly.

## Where it keeps things

One SQLite file of its own, by default
`<stash config dir>/fast-discovery/fastdiscovery.sqlite`. Stash's database is never opened
and never written to except through the API. After Apply or Reject, all that is left for a
run is one row: scene, run, when it started and finished, how many sources answered and how
many failed.
