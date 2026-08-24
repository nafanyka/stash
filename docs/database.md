# The ScrapeDiscovery database

One SQLite file, owned entirely by the plugin. Stash's own database is never opened and
its schema is never touched.

```
<stash config dir>/scrape-discovery/scrape-discovery.sqlite
```

Not the plugin directory: installing a newer version of a plugin package deletes every
file the previous version installed (`pkg/pkg/manager.go`), and uninstalling removes the
directory. The `databasePath` setting overrides the location.

Settings applied on open, in `db/repo.py`:

| | |
| --- | --- |
| `journal_mode = WAL` | A scan's writes never block a UI read, or the reverse. |
| `synchronous = NORMAL` | The usual WAL companion. A power cut can cost the last few commits; for a cache of scraper answers that is not worth an fsync per write. |
| `foreign_keys = ON` | Cascades are load-bearing — deleting a scan must take its attempts and their results. |
| `busy_timeout = 30000` | Two plugin processes can be alive at once: a scan job and a UI operation. |

## Versioning

The schema version lives in `PRAGMA user_version`, so a fresh file reports 0 and no
bookkeeping table is needed. `db/migrations.py` holds a list of numbered, forward-only
steps; each runs in its own transaction, so a failed migration leaves the previous
version intact. A database from a *newer* plugin than the one running is refused rather
than half-read.

Three separate version numbers live in `meta`, one per derived stage:

```
NORM_VERSION          how a raw payload becomes comparable
CORRELATION_VERSION   how results become candidates
SCORING_VERSION       how a candidate becomes a number
```

Rows record the version they were produced with. That is what makes reprocessing
possible: change the scoring model, bump `SCORING_VERSION`, and the maintenance task
rebuilds every score from the stored raw results without asking a website anything.

## Entities

```
                         ┌───────────┐
                         │  scrapers │  registry snapshot + fingerprint
                         └───────────┘

  scans ──< attempts ──< results ──> blobs
              │  ▲          │
              │  └──────────┼──── parent_id: the discovery graph edge
              │             │
              └──< discovered_urls ─┘ (source_result_id)

  candidates ──< candidate_sources ──> results
       └──< candidate_fields ──< candidate_field_sources ──> results

  applications          audit trail of what was written to Stash
  scene_state           the inbox index
  meta                  versions and bookkeeping
```

### scans

One discovery execution for one scene: trigger, mode, status, the configuration in
force, and the scene as it looked at the time. `heartbeat_at` is how a killed scan is
detected — Stash's `stopJob` kills the process outright, so a scan can never write its
own final status, and any operation that opens the database sweeps scans that stopped
reporting.

### attempts

One scraper invocation. `target_key` is the identity of the question being asked — the
deduplication key within a scan and the cache key across scans. For a URL attempt it
deliberately excludes the scraper, because Stash chooses the handler itself.

`scraper_id` is null when Stash auto-routed a URL among several possible handlers:
`Cache.ScrapeURL` iterates a Go map and returns the first match, so the choice is
non-deterministic and unreported. `attribution` records whether the source is known.

`error_kind` is `permanent` or `transient`. A single scene thrown at every installed
fragment scraper produced 101 errors out of 191 attempts on a real library, nearly all
of them scrapers failing at a scene from a site they have nothing to do with. Those are
cached like a no-match; a timeout or a connection reset is retried within a day.

`from_cache` marks a row copied into a later scan rather than executed, and
`cached_from` points at the attempt that actually ran, so the payload is stored once and
the chain never deepens.

### results and blobs

One row per returned item — a name search answers with a list, and each entry is a
separate potential candidate. Both forms are kept: `raw_json` exactly as the scraper
said it, `normalized_json` as something comparable.

Images are the reason `blobs` exists. Scrapers return covers inline as base64 data URIs;
one measured cover was 214 KB. They are lifted out of the payload on write, stored once
per SHA-256, and referenced from the raw JSON as `{"$blob": "<sha256>", ...}` — so the
original data URI can still be rebuilt byte for byte, and a list query never carries a
JPEG.

### discovered_urls

Every URL a result mentioned, with the set of installed scrapers whose patterns match
it, computed with Stash's own rule (`strings.Contains`). `UNIQUE (scan_id, norm_key)` is
the loop guard. `state` says what happened to it, and never lies by omission —
`NO_HANDLER`, `SKIPPED_DEPTH` and `SKIPPED_LIMIT` are all recorded explicitly.

### candidates and provenance

`candidate_fields` holds each distinct value offered for each field, and
`candidate_field_sources` which results offered it. That pair is what makes "do these
sources independently agree?" answerable, and what the per-field source selector reads.

### scene_state

Denormalised on purpose. The inbox filters, sorts and pages over potentially tens of
thousands of scenes, and must do all of it in SQL without opening a raw result. Every
column is derived from the tables above, so it can always be rebuilt.

Statuses: `UNSCANNED`, `SCANNING`, `CANDIDATES`, `RESULTS`, `NO_RESULTS`, `FAILED`,
`APPLIED`, `DISMISSED`. `RESULTS` sits between candidates and nothing-found: scrapers
answered, but nothing has correlated those answers yet. Without it a scene with five
matches would read as "nothing found", which is exactly backwards.

### scraper_stats

A view over `attempts`, not a counter table, so the numbers cannot drift and nothing has
to be maintained on the hot path. Cache hits are excluded — a cache hit is not evidence
about a scraper.

## Retention

| | |
| --- | --- |
| `Clear expired cache` | Deletes expired attempts that returned **nothing**. An attempt that owns results is never touched by cache clearing: those results are what every later stage is rebuilt from. |
| `Delete old history` | Deletes whole scans older than `historyRetentionDays`, cascading to their attempts and results — the only thing allowed to remove stored answers, and it removes them with their context. |
| `Vacuum database` | Compacts, and collects blobs nothing references. |

## Querying it by hand

Read-only inspection is safe while Stash is running (WAL). Some starting points:

```sql
-- what has been tried against a scene, most recent first
SELECT started_at, scraper_name, method, status, duration_ms, error
FROM attempts WHERE scene_id = 42 ORDER BY started_at DESC;

-- which scrapers actually earn their place
SELECT scraper_name, attempts, matches, errors, timeouts, avg_ms
FROM scraper_stats ORDER BY matches DESC LIMIT 20;

-- where the space went
SELECT (SELECT SUM(bytes) FROM blobs) AS image_bytes,
       (SELECT SUM(LENGTH(raw_json)) FROM results) AS raw_bytes;

-- URLs found that nothing installed can follow: candidates for a new scraper
SELECT DISTINCT host, COUNT(*) FROM discovered_urls
WHERE state = 'NO_HANDLER' GROUP BY host ORDER BY 2 DESC;
```
