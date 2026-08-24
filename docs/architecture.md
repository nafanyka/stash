# ScrapeDiscovery — architecture

ScrapeDiscovery is a discovery/orchestration layer over the scrapers already installed
in Stash. It probes many installed scrapers for one scene, follows the URLs those
scrapers return through the installed URL scrapers, stores **every** response in its
own database, correlates the responses into candidates, scores them, and lets the user
review and selectively apply the result.

It is not a metadata source. It never writes to a scene as a side effect of scraping.

Everything below was verified against a **live Stash v0.31.1** (`4de2351e`, 780 scene
scrapers, 55 plugins installed) and against the `v0.31.1` tag of `stashapp/stash`.
Source references are to that tag.

---

## 1. Stash API feasibility

### 1.1 Verified GraphQL surface

| Requirement | Supported | Mechanism | Notes |
| --- | --- | --- | --- |
| Enumerate scrapers | **Yes** | `listScrapers(types: [SCENE])` -> `Scraper{id name scene{urls supported_scrapes}}` | `supported_scrapes` is `NAME`/`FRAGMENT`/`URL`; `urls` is the URL-match table. |
| Invoke a *named* scene scraper (fragment, real scene) | **Yes** | `scrapeSingleScene(source:{scraper_id}, input:{scene_id})` | Stash builds the fragment from the DB scene. Verified: 1.9 s, returned a match. |
| Invoke a *named* scene scraper with a synthetic fragment | **Yes** | `scrapeSingleScene(source:{scraper_id}, input:{scene_input:{url,title,...}})` | `ScrapedSceneInput` carries `title code details director url urls date remote_site_id`. Verified: 0.65 s, match. Lets a URL be fed to a *fragment* scraper. |
| Invoke a *named* scene scraper by name search | **Yes** | `scrapeSingleScene(source:{scraper_id}, input:{query})` | Returns a **list** (verified: 12 results for one query, 7.8 s). |
| Invoke a stash-box | **Yes** | `scrapeSingleScene(source:{stash_box_endpoint\|stash_box_index}, input:{scene_id\|query})` | `scene_id` means fingerprint lookup. Endpoints from `configuration{general{stashBoxes{endpoint name}}}`. |
| Scrape a URL | **Yes, but not per scraper** | `scrapeSceneURL(url)` / `scrapeURL(url, ty)` | See 1.2 — **the scraper cannot be chosen.** |
| Match a scraper against a URL | **Yes (client-side, exact rule)** | `Scraper.scene.urls` + `strings.Contains(url, pattern)` | `pkg/scraper/definition.go:163`. Substring, *not* prefix. Replicated verbatim in `registry.py`. |
| Read scenes / filter by tag | **Yes** | `findScenes(scene_filter:{tags:{...}}, filter:{page,per_page,sort})` | `SceneFilterType` also has `organized`, `path`, `title`, `duration`, `url`, `studios`, `stash_id_count`, `updated_at`. |
| Read full scene input data | **Yes** | `findScene(id)` -> `files{basename path duration width height size fingerprints{type value}} urls title date details studio performers tags organized stash_ids` | oshash/phash come from `files.fingerprints`. |
| Update a scene (singular fields) | **Yes** | `sceneUpdate(input: SceneUpdateInput)` | `tag_ids`/`performer_ids`/`urls` here are **SET** semantics — dangerous for additive intent. |
| Update a scene **additively** | **Yes** | `bulkSceneUpdate(input:{ids:[id], tag_ids:{mode:ADD,ids}, performer_ids:{mode:ADD,ids}, urls:{mode:ADD,values}})` | `BulkUpdateIdMode = SET\|ADD\|REMOVE`. The safe primitive for set-like fields; no lost-update race. |
| Create/find tags for automation | **Yes** | `findTags(tag_filter:...)`, `tagCreate(input:{name})` | |
| Background task with progress | **Yes** | `runPluginTask(plugin_id, task_name, args_map: Map) : ID!` -> job id; progress via the `\x01p\x02` stderr protocol; observe with `jobQueue`/`findJob`/`jobsSubscribe` | `args_map` is `map[string]interface{}` (`pkg/plugin/args.go`), so arbitrary nested JSON args are fine. |
| Cancel a running task | **Yes** | `stopJob(job_id)` -> `cmd.Process.Kill()` (`pkg/plugin/raw.go`) | Hard kill. The DB must be crash-consistent, see 7.4. |
| Synchronous request/response into the plugin (UI to backend) | **Yes** | `runPluginOperation(plugin_id, args: Map) : Any` | `Cache.RunPlugin` starts the process, waits, returns `output.Output` verbatim (`pkg/plugin/plugins.go:298`). The server sets no `ReadTimeout`/`WriteTimeout`, so long calls are not cut off. Already used in production by the installed `sceneMatcher` plugin. |
| Plugin settings UI | **Yes** | `settings:` block in the manifest; read via `configuration{plugins}`; write via `configurePlugin` | Types are only `STRING`/`NUMBER`/`BOOLEAN`, and **there is no declarable default** (`pkg/plugin/setting.go`) — an untouched setting arrives absent, so defaults live in code. |
| Inject JS/CSS into the UI | **Yes** | `ui: {javascript: [...], css: [...], assets: {...}, csp: {...}, requires: [...]}` (`pkg/plugin/config.go:73`) | Files are served at `/plugin/{id}/javascript`, `/css`, `/assets/...`, loaded as **classic scripts** (`ui/v2.5/src/plugins.tsx` -> `useScript`), before the router renders. |
| Add a main-menu entry | **Yes** | `PluginApi.patch.before("MainNavBar.MenuItems", ...)` | Patch point present in `pluginApi.d.ts:709`. |
| Add a page/route | **Yes** | `PluginApi.register.route("/scrape-discovery", Component)` | Injects a `<Route>` into `PluginRoutes`, which sits inside the app `<Switch>` before the 404 (`App.tsx:274`). |
| Scene-page integration | **Yes** | `patch.before("ScenePage.Tabs")` + `patch.before("ScenePage.TabContent")`, `patch.after("SceneCard.Overlays")` | A dedicated tab, so the scene page is not cluttered. |
| Per-scraper timeout | **Yes, client-side** | Abort the GraphQL HTTP request | Script scrapers run under `stashExec.CommandContext(ctx,...)` (`pkg/scraper/script.go:232`), so aborting the request cancels the request context and kills the scraper process. HTTP-based scrapers additionally have Stash's own 60 s client timeout (`pkg/scraper/cache.go:26`). |
| Run discovery *as* a Stash scraper | **Technically yes, semantically wrong** | `sceneByFragment` | See section 2. |

### 1.2 Limitations found, and what we do instead

**L1 — A URL scrape cannot be aimed at a specific scraper, and the choice is
non-deterministic.**
`Cache.ScrapeURL` iterates `c.scrapers`, which is a `map[string]scraper`
(`pkg/scraper/cache.go:121`), and returns the **first** entry whose
`supportsURL` matches (`cache.go:312`). Go map iteration order is randomised, so when
several scrapers match one URL, which one runs varies between calls, and nothing in the
response says which one it was.

*What we do:* compute the matching set ourselves with Stash's exact rule
(`strings.Contains`) and store it on the `discovered_urls` row as `handler_ids`.
When the set has one member, attribution is certain and recorded. When it has more, the
attempt is recorded with `scraper_id = NULL` and `attribution = AMBIGUOUS`, listing the
candidate handlers. Optionally (`urlRescrapeAttempts`, default 1) a URL with several
handlers can be scraped more than once; distinct payloads are kept and de-duplicated by
fingerprint, which harvests more than one handler's view of the same URL. In the live
library every scene URL tested matched exactly one scraper, so this is an edge case, not
the normal path.

**L2 — There is no "scrape this URL with scraper X" call, even for scrapers that
clearly support the URL.** Mitigated by the synthetic-fragment path: for a scraper that
declares `FRAGMENT`, `scrapeSingleScene(source:{scraper_id:X}, input:{scene_input:{url}})`
reaches that specific scraper with that URL (verified working). It invokes
`sceneByFragment`, not `sceneByURL`, so it is a different code path inside the scraper and
is recorded as a distinct `method` (`FRAGMENT_INPUT`), never conflated with a `URL`
attempt.

**L3 — A broken scraper can fail the whole GraphQL request.** One scraper on the live
instance (`StasyQVR`) returns `Internal system error. Error <runtime error: invalid
memory address or nil pointer dereference>` for a fragment scrape. Because a scraper
error makes the resolver return an error for the entire query
(`internal/api/resolver_query_scraper.go:170`), ScrapeDiscovery issues **exactly one
scraper per GraphQL request**. Errors are therefore isolated per attempt by construction,
and recorded as `ERROR` with the message.

**L4 — Plugin settings have no declarable defaults and only three types.** All defaults,
validation, and any structured configuration (per-scraper priority/timeout/cache tables,
scoring weights) live in code and in JSON-valued `STRING` settings edited through
ScrapeDiscovery's own settings page, which is richer than what the manifest can express.
The manifest still declares the handful of scalar settings so they are visible and
editable in Stash's own plugin settings UI.

**L5 — No plugin-served HTTP API.** A plugin cannot expose an endpoint; `ui.assets` only
serves static files. All UI-to-backend traffic therefore goes through
`runPluginOperation`, which costs one Python process spawn per call (~0.1-0.3 s). Fine
for a UI; not usable as a streaming channel. Live scan progress is read from Stash's own
`jobQueue`/`findJob` instead.

**L6 — Scraped images arrive as base64 data URIs.** Verified: a single `ScrapedScene`
came back with an ~200 KB `data:image/jpeg;base64,...` cover. Storing those inline in the
raw JSON for hundreds of attempts per scene would bloat the database badly. Images are
therefore externalised on write into a `blobs` table keyed by SHA-256 (so the same cover
from ten scrapers is stored once), and the raw JSON keeps a `{"$blob": "<sha256>", ...}`
reference. Raw fidelity is preserved — reprocessing (section 6) can reconstruct the
original payload exactly.

**L7 — A plugin route must not live under `/plugin/...`.** That prefix is server-owned
(`r.Mount("/plugin", ...)`, `internal/api/server.go:222`); verified live:
`GET /plugin/ScrapeDiscovery` -> 404, while `GET /scrape-discovery` -> the SPA shell. The
page is registered at **`/scrape-discovery`** so a hard reload works.

**L8 — Plugin files are deleted on package update/uninstall.** `Manager.Install` removes
every file listed in the previous manifest before extracting the new zip, and `Uninstall`
removes the package directory (`pkg/pkg/manager.go`). The database therefore lives
**outside** the plugin directory, by default in
`{server_connection.Dir}/scrape-discovery/scrape-discovery.sqlite` — the Stash config
directory, which survives both.

**L9 — Job progress is a single float.** `Job.subTasks` cannot be set by a plugin; only
`\x01p\x02<fraction>` is available. Detailed progress ("scene 18/120, scraper 7/42,
3 matches, 8 URLs, current: IAFD") is written to the log at info level and mirrored into
the `scans` row, which the ScrapeDiscovery page polls; the job's own progress bar gets
the overall fraction.

**L10 — `runPluginOperation` panics for a plugin that is not installed.** Verified
against the live instance: asking for a plugin id Stash does not have answers
`Internal system error. Error <runtime error: invalid memory address or nil pointer
dereference>` and logs a panic. `Cache.RunPlugin` looks the plugin up and then uses it
without a nil check, unlike `CreateTask` a few lines above, which returns a clean error.
This matters because "installed the scraper, forgot the plugin" is the single most likely
setup mistake, so the shim asks `plugins { id enabled }` first and reports exactly what
is wrong — which also keeps the panic out of the user's log.

Related, from the same session: `runPluginTask` for a missing plugin is accepted and
queued, and only fails when the job reaches the front of the queue. So a queued job is
not evidence that anything will run, which is one of the reasons a scene is no longer
marked "scanning" at the moment a scan is queued (section 7.4).

### 1.3 Scale observed on the live instance

780 scene scrapers: 756 support `URL`, 194 support `FRAGMENT`, 201 support `NAME`
(the sets overlap). 1140 scenes. A fragment attempt took 0.5-2 s, a name attempt ~8 s.
So a naive "try everything" pass is roughly 6 minutes of fragment work plus 25 minutes of
name work per scene, serialised. This is what makes routing, priorities, caching, stop
conditions and the Normal/Deep split load-bearing rather than decorative — and why Normal
Scan does **not** run every name scraper by default.

Note also that a number of installed "scrapers" are local utilities rather than metadata
sources (`builtin_autotag`, `CopyMetadata`, `FileMetadata`, `Filename`,
`scrapeCoverFromFile`, `scene-cover-in-dir`, ...). They are excluded by a configurable
default blocklist; Deep Scan can still be told to include them.

---

## 2. Should ScrapeDiscovery be a plugin, a scraper, or both?

**Decision: a plugin that owns everything, plus an optional thin scraper entry point.**

A Stash scraper registered with `sceneByFragment` does appear both in the scene edit
panel's *Scrape with...* menu and as an *Identify* source — that is how the existing
`ScrapeAll` scraper in this repository works. But making discovery itself a scraper is
wrong on three counts:

1. **It is synchronous inside the GraphQL request.** Discovery over even a fraction of
   194 fragment scrapers takes minutes; the scene edit panel would block on it.
2. **It inverts the core rule.** A fragment scraper's job is to hand Stash a
   `ScrapedScene`, which Stash then offers to write into the scene — and *Identify*
   writes it without review. Requirement 1 is that discovery must never modify the scene.
3. **It cannot express the product.** Multiple candidates, per-field source selection,
   provenance and confidence do not fit into one `ScrapedScene` return value.

So the scraper is **not** the engine. `scrapers/ScrapeDiscovery/` is a ~150-line entry
point that speaks only GraphQL and holds no discovery logic. It asks the plugin one
question — `runPluginOperation(op: "scraper.entry")` — and the plugin answers with one
of four actions:

| action | what the shim does |
| --- | --- |
| `returned` | print the agreed scene, so Stash opens its normal merge dialog |
| `no_consensus` | print `null`, and log how many stored answers there are to review |
| `queued` | print `null`; a discovery job has been started |
| `running` | print `null`; a scan for this scene is already going |

It reads nothing from the SQLite database directly, and never scrapes: everything it
hands over was found by a previous scan. One source of truth, no duplicated logic
(requirement 47), and a test asserts the shim never imports the engine or calls a scrape
operation.

### The recursion guard

Registering for scene fragments means the discovery engine would otherwise find the shim
in `listScrapers` and invoke it — and the shim's answer to being invoked is to start a
scan. Stash gives a scraper no way to detect that it is running inside one, because the
nested run is a fresh process spawned by the server, so the guard has to live in the
plugin and cannot be configurable: `settings.NEVER_INVOKE` drops that scraper id before
any work list is built, whatever the configuration says.

### What the shim is allowed to return

Handing an arbitrary "best" stored result to a dialog that writes to the library would be
actively harmful. Measured on one real scene: 21 attempts reported a match, three of
which were the scene. Among the rest, one scraper returned the server's IP address as a
title, one a similarly-named different film, one an unrelated scene.

`consensus.py` therefore answers a narrower question than the candidate correlator will —
*is there one answer trustworthy enough to offer?* — and qualifies a group only on
evidence a guessing scraper cannot manufacture: a stash-box fingerprint match, a scraper
reading the site's own page for a URL already on the scene, or two independent witnesses.
Independence is counted carefully, and this is where the live data changed the design
twice:

- **Source identity is who was asked, not what came back.** Keying on the returned URL's
  host collapsed StashDB, theporndb, Timestamp.trade and the site's own scraper into one
  witness, because all four reported the same link. A scraper declaring exactly one host
  is keyed by that host (so two scrapers for one site are one witness); one covering
  several sites, or none, is keyed by its own id; stash-boxes by endpoint.
- **Re-reading a URL we already had is one piece of evidence, not five.** Several
  installed scrapers answer a fragment scrape by fetching whichever URL is in the
  fragment. Correct or not, that is the same evidence as the site's own scraper reading
  the same page, so they share one witness key. A fingerprint match is exempt: it
  identified the file, whatever URL it printed alongside.

Two further filters come straight from observed behaviour. A result contributes no
evidence at all if it only echoes what the fragment already contained — one scraper
returned the given URL in the `code` field plus its own name as the studio — and a title
on its own is never evidence, whatever it says, because it could have come from the
filename. Field values are then voted on, with ties broken by provenance and then
brevity: two sources offered a `code` of `211` and two offered the page's entire HTML
title, and counting votes alone picked whichever happened to be first.

The primary single-scene entry point is still the plugin's own UI: a **Discovery tab on
the scene page** with `Discover`, `Deep Scan` and `View results`, plus a candidate-count
badge. That is a real Stash-supported scene action (`ScenePage.Tabs` patch). The shim is
the convenience route for people who already reach for *Scrape with…*, and it can be
left uninstalled without losing anything.

---

## 3. How arbitrary installed scrapers are invoked

For one scene the engine builds a work list of typed *attempts*. Every attempt is one
GraphQL request carrying one scraper.

| `method` | Call | Applicability |
| --- | --- | --- |
| `URL` | `scrapeSceneURL(url)` | a URL from the scene or from a previous result, matched by at least one installed scraper |
| `FRAGMENT_SCENE` | `scrapeSingleScene(source:{scraper_id}, input:{scene_id})` | scraper declares `FRAGMENT` |
| `FRAGMENT_INPUT` | `scrapeSingleScene(source:{scraper_id}, input:{scene_input:{url,title,date,...}})` | scraper declares `FRAGMENT`; used to aim a discovered URL at a specific scraper (L2) |
| `NAME` | `scrapeSingleScene(source:{scraper_id}, input:{query})` | scraper declares `NAME`; query = title, else cleaned filename |
| `STASHBOX_FP` | `scrapeSingleScene(source:{stash_box_endpoint}, input:{scene_id})` | any configured stash-box; fingerprint match |
| `STASHBOX_QUERY` | `scrapeSingleScene(source:{stash_box_endpoint}, input:{query})` | any configured stash-box |

Ordering (requirement 21): URL attempts for URLs already on the scene, then stash-box
fingerprint, then `High` priority scrapers, then scrapers whose URL patterns match a known
scene URL host (domain routing, requirement 62), then the remaining `Normal` fragment
scrapers, then `NAME` scrapers, then `Low`. Within a band, configured priority then
scraper id, so runs are reproducible. Historical statistics are collected (requirement 60)
but do **not** reorder anything in v1.

Execution is a bounded thread pool (`maxConcurrency`, default 3) of plain `urllib` POSTs
to `/graphql`, each with its own socket timeout that doubles as the scraper timeout (see
the "Per-scraper timeout" row above). Nothing holds a database transaction open across a
scraper call (requirement 50): the attempt row is written as `RUNNING`, the network call
happens outside any transaction, then a short transaction commits the result.

---

## 4. UI integration

Plain JavaScript, no build step — consistent with this repository's Python-only CI, and
with how Stash loads plugin scripts (classic `<script>`, not modules). Components are
written with `PluginApi.React.createElement` via a local `h()` alias.

```
main nav        patch.before("MainNavBar.MenuItems")   -> "ScrapeDiscovery" entry
page            register.route("/scrape-discovery")            -> Inbox
                register.route("/scrape-discovery/scene/:id")  -> scene discovery view
                register.route("/scrape-discovery/settings")   -> settings
scene page      patch.before("ScenePage.Tabs")         -> "Discovery" tab + count badge
                patch.before("ScenePage.TabContent")   -> the tab's panel
scene cards     patch.after("SceneCard.Overlays")      -> small confidence badge (optional)
```

Styling reuses Stash's own Bootstrap classes and CSS variables through
`PluginApi.libraries.Bootstrap`, so the page looks native. The CSP served by the live
instance (`img-src data: *`, `style-src 'unsafe-inline'`, `connect-src 'self'`) already
permits everything needed — base64 candidate images render, same-origin GraphQL works —
so the manifest declares no `csp` block.

Data flow: the UI never touches SQLite. It calls
`runPluginOperation(plugin_id:"ScrapeDiscovery", args:{op:"...", ...})` — the pattern the
installed `sceneMatcher` plugin already uses on this instance — and reads Stash's own
`findScene`/`jobQueue` directly for scene data and live scan status.

Operations (`ops.py`), all returning plain JSON:

```
inbox.list          {status, q, sort, dir, page, perPage, minConfidence, studio, tag, scraper}
scene.summary       {scene_id}          badge/tab counts
scene.detail        {scene_id}          scans, attempts, candidates, urls, graph
candidate.detail    {candidate_id}      fields, per-field sources, score breakdown
candidate.compare   {scene_id}          the comparison matrix incl. current Stash values
apply.preview       {scene_id, selection}
apply.commit        {scene_id, candidate_id, selection}
scan.start          {scene_id, mode}    delegates to runPluginTask, returns the job id
scan.cancel         {job_id}
settings.get / settings.set
diagnostics.info    db size, counts, versions, scraper stats
```

---

## 5. Database

SQLite, WAL, `foreign_keys=ON`, `busy_timeout`, short transactions, schema version in
`PRAGMA user_version` with forward-only numbered migrations in `db/migrations.py`.
Default path `{stash config dir}/scrape-discovery/scrape-discovery.sqlite` (L8),
overridable by a setting. Stash's own database is never opened.

```
meta(key TEXT PK, value TEXT)                   -- versions, last registry sync

scrapers(id PK, name, kinds_json, url_patterns_json,
         fingerprint,               -- sha256(name|kinds|patterns): identity and "version"
         first_seen, last_seen)     -- gives "not tried with scraper B yet" (req. 17)

scans(id PK, scene_id, trigger, mode, status,
      started_at, finished_at, config_json, scene_snapshot_json,
      scraper_count, attempt_count, match_count, error_count, url_count,
      candidate_count, best_confidence, stop_reason, error, progress_json)

attempts(id PK, scan_id -> scans, scene_id,
         parent_id -> attempts,        -- parent = the discovery graph edge
         depth, scraper_id, scraper_fingerprint, attribution,
         method, target, target_key,   -- target_key is the dedup/cache fingerprint
         input_json, status, error, started_at, finished_at, duration_ms,
         result_count, from_cache)

results(id PK, attempt_id -> attempts, ordinal,
        raw_json, raw_fingerprint,
        normalized_json, normalized_fingerprint, norm_version,
        image_sha256 -> blobs.sha256)

blobs(sha256 PK, mime, bytes, data BLOB)        -- deduped cover images (L6)

discovered_urls(id PK, scan_id, scene_id, url, normalized, host, norm_key, depth,
                source_result_id -> results, handler_ids_json, handler_count,
                state, attempt_id -> attempts)

candidates(id PK, scene_id, scan_id, identity_key,
           confidence, level, score_json, merged_json,
           source_count, independent_source_count, state,
           correlation_version, scoring_version, created_at, updated_at)

candidate_sources(candidate_id -> candidates, result_id -> results, scraper_id, role)
candidate_fields(id PK, candidate_id -> candidates, field, value_key, value_json,
                 source_count)
candidate_field_sources(candidate_field_id -> candidate_fields, result_id -> results,
                        scraper_id)

applications(id PK, scene_id, candidate_id, applied_at, mode,
             selection_json, before_json, after_json, changes_json, status, error)

scene_state(scene_id PK, status, last_scan_id, last_scanned_at,
            candidate_count, best_confidence, attempt_count, error_count,
            title, path, studio_name, updated_at)   -- the Inbox index
```

`scene_state` exists so the Inbox can filter, sort and page entirely in SQL without
touching raw JSON (requirement 49). Indexes: `attempts(scene_id,target_key)`,
`attempts(scan_id)`, `attempts(scraper_id,status)`, `results(attempt_id)`,
`results(raw_fingerprint)`, `discovered_urls(scene_id,norm_key)`,
`candidates(scene_id,confidence)`, `scene_state(status,best_confidence)`.

Scraper statistics (requirement 60) are a **view** over `attempts`, so nothing extra has
to be maintained and history stays the single source of truth.

---

## 6. Pipeline stages, and why they are separable

```
acquisition   -> raw_json                 (network; the only stage that can fail slowly)
normalization -> normalized_json          (pure function of raw_json, versioned)
correlation   -> candidates               (pure function of normalized results, versioned)
scoring       -> confidence + score_json  (pure function of candidates + scene, versioned)
application   -> applications + Stash write (explicit, audited)
```

Each stage after acquisition is a pure function of stored data and carries a version
number in `meta`. `Rebuild candidates from stored raw results` and `Recalculate
confidence` therefore re-run the later stages over the database with **no external
requests** (requirements 43, 44).

### Normalization

URL normalization: lowercase scheme and host, drop `www.`, drop default port, drop
fragment, strip tracking parameters (`utm_*`, `ref`, `aff`, `src`, ...), sort remaining
query parameters, collapse a trailing slash; path case preserved, since some sites are
case-sensitive. Both the original and the normalized form are stored.
Result normalization: trim strings, unify dates to `YYYY-MM-DD`, split multi-value fields,
canonicalise performer/studio/tag names (casefold, collapse punctuation and whitespace)
for comparison while keeping the display form, externalise images (L6).
Fingerprints are SHA-256 over canonical JSON (sorted keys, no whitespace).

### Correlation (conservative by design)

Union-find over normalized results, merging only on a strong rule:

1. shared canonical URL, or
2. equal normalized title **and** equal date, or
3. title similarity >= `titleMergeThreshold` **and** (equal date **or** duration within
   tolerance) **and** no studio contradiction.

Anything weaker stays a separate candidate. Per-field provenance is retained through the
merge (`candidate_fields` / `candidate_field_sources`), which is what makes "do
independent sources agree?" answerable (requirements 13, 61).

### Scoring

Normalised 0-100 weighted sum over the signals that *apply* to this scene, so a scene
with no existing metadata is not penalised for comparisons that cannot be made:

```
score = 100 * sum(w_i * s_i) / sum(w_i)      over applicable signals only
```

| signal | strength `s` |
| --- | --- |
| fingerprint match (stash-box oshash/phash) | 1 if matched |
| canonical URL agreement | 1 if the candidate URL is already on the scene, or two or more independent sources share it |
| title similarity | normalized similarity against the better of existing title / cleaned filename |
| duration | 1 within `durationTolerance`, decaying linearly to 0 at three times the tolerance |
| date agreement | 1 exact, 0.5 within a day |
| studio agreement | 1 exact or alias, 0 on contradiction |
| performer agreement | Jaccard over canonical names |
| independent source agreement | scales with the number of distinct sources, counting *distinct hosts*, so three scrapers of one site are not three witnesses |

Weights, tolerances and the level thresholds (`>=95` almost certain, `80-94` strong,
`60-79` possible, `<60` weak) are configuration. Two guards keep the number honest: a
single unverified source is capped below "almost certain", and a candidate below
`minCandidateConfidence` is kept as a raw result but not promoted to a candidate — which
is what stops a `NAME` scrape's dozen near-misses from becoming a dozen candidates.
`score_json` records every signal's strength, weight, contribution and the sources that
drove it, so the UI can render the breakdown instead of a bare number.

### Loop prevention and limits (all configurable)

`maxDepth` 2, `maxUrlsPerScan` 40, `maxAttemptsPerScan` 250, `sceneTimeBudget` 900 s,
uniqueness on `(scene_id, norm_key)` for URLs and on `(scene_id, target_key)` for
attempts, and a scraper is never run twice on the same target within a scan.

### Cache TTLs (configurable)

`NO_MATCH` 30 d, `MATCH` 90 d, `ERROR` 1 d, `TIMEOUT` 3 d, keyed on
`(scene_id, target_key, scraper_fingerprint)`. A changed scraper fingerprint invalidates
that scraper's cached attempts, which is also the mechanism behind *Scan with newly
installed scrapers*.

---

## 7. Apply, merge, automation

### 7.1 Merge semantics

Singular fields (`title`, `date`, `details`, `code`, `director`, `image`, `studio`): the
user picks the source per field; nothing is replaced unless selected. Set-like fields
(`performers`, `tags`, `urls`, `groups`): additive by default, executed with
`bulkSceneUpdate` and `mode: ADD`, so existing values cannot be lost even if the scene
changes between preview and apply. Removal is possible but always explicit. `studio_id` is
singular in Stash's model, so replacing a studio is an explicit choice, never implied.

### 7.2 Apply sequence

1. re-read the scene from Stash;
2. resolve the selection into a change set, dropping no-ops;
3. resolve `ScrapedPerformer`/`ScrapedTag`/`ScrapedStudio` to Stash ids — `stored_id`
   when the scraper supplied one, else lookup by name, else create only if the
   corresponding `create...` setting is on;
4. write: `sceneUpdate` for singular fields, `bulkSceneUpdate(ADD)` for set-like fields;
5. record `applications` with `selection_json`, `before_json`, `after_json`,
   `changes_json`;
6. update tags per the automation rules and refresh `scene_state`.

### 7.3 Automation

Tag names are configuration, resolved to ids at run time and created on demand:
`inputTag` (default `needs_scrape`) selects the batch, `reviewTag` (`scrape_review`) is
added when candidates were found, `failedTag` (`scrape_failed`) when nothing was, and on
a successful apply the input and review tags are removed. Auto-apply is **off** by
default; when enabled it requires `confidence >= autoApplyMinConfidence` (95) **and**
`>= autoApplyMinIndependentSources` (2), and obeys its own safety switches
(only-fill-empty singular fields, never overwrite title/date, add performers/tags/urls,
studio behaviour). Every automatic write produces an `applications` row exactly like a
manual one.

### 7.4 Crash and cancellation consistency

`stopJob` kills the process outright, so consistency comes from write discipline, not
cleanup: each attempt and result is committed as it completes, so completed attempts
always survive. A scan row left `RUNNING` by a killed process is swept to `CANCELLED` on
the next run of any ScrapeDiscovery operation, detected from a heartbeat timestamp in
`progress_json`.

A scene is **not** marked as scanning when a scan is merely queued. Two reasons, both
observed: a queued job has not started — the live instance had 1290 jobs ahead of it —
and `runPluginTask` accepts a job even for a plugin that cannot run it (L10). Writing the
status optimistically therefore leaves a scene permanently claiming to scan, with no scan
row for the sweep to repair, and both the UI and the scraper shim then refuse to start
another one. The scan sets the status when it actually begins; until then the job id is
what the caller watches. Queuing the same scene twice is harmless anyway — the second
scan finds everything cached and finishes in milliseconds. `sweep_stale_scans` also
repairs any scene claiming to scan with no running scan behind it, so an older database
in that state heals itself.

---

## 8. Repository layout

Follows this repository's existing convention (`plugins/<Name>/<Name>.yml`,
`scrapers/<Name>/<Name>.yml`, shared code auto-bundled from `common/python/` when
imported, `dist/` indexes built by `.github/workflows/build_index.py`). The packager
already zips a plugin folder recursively, so a backend subpackage needs no build changes.

```
plugins/ScrapeDiscovery/
  ScrapeDiscovery.yml       manifest: exec, tasks, settings, ui{javascript,css}
  ScrapeDiscovery.py        entry point - parses stdin, dispatches, prints {"output":...}
  README.md
  scrapediscovery/
    __init__.py
    settings.py             configuration schema, defaults, parsing (L4)
    logs.py                 structured logging on top of stash_common.log
    stash.py                Stash API adapter - the only module with GraphQL text
    registry.py             scraper registry, URL routing, scraper fingerprints
    normalize.py            URL and result normalization, fingerprinting
    engine.py               scene stage, URL expansion, limits, stop conditions
    executor.py             bounded concurrency, timeouts, cancellation
    cache.py                attempt cache / TTL policy
    correlate.py            candidate correlation
    score.py                confidence signals and scoring
    merge.py                change preview + apply/merge engine
    tasks.py                plugin tasks (single, batch, retry, new-scraper, maintenance)
    ops.py                  runPluginOperation handlers (the UI API)
    db/
      __init__.py
      migrations.py         numbered forward migrations
      repo.py               repository - parameterized SQL only
  ui/
    scrapediscovery.js
    scrapediscovery.css
scrapers/ScrapeDiscovery/
  ScrapeDiscovery.yml       sceneByFragment -> thin entry point
  ScrapeDiscovery.py        GraphQL only; no discovery logic
docs/
  architecture.md
  database.md
  development.md
tests/
  test_normalize.py test_fingerprint.py test_correlate.py test_score.py
  test_cache.py test_limits.py test_merge.py test_repo.py test_reprocess.py
```

---

## 9. Security

Scraper output is untrusted. It is never `eval`ed, never interpolated into SQL (the
repository layer only ever passes parameters), and never inserted into the DOM as HTML —
the UI builds elements through `React.createElement`, which escapes text, and renders
discovered URLs as an `href` only after validating that the scheme is `http` or `https`.
Image data URIs are validated for MIME type and size before being stored. No filesystem
path from scraper output is ever opened. Nothing but the database is read from or written
to disk, and secrets (API keys, session cookies) are never logged.

---

## 10. Implementation phases

Each phase leaves the plugin installable and useful.

1. **Core** — settings, DB and migrations, Stash adapter, registry, single-scene scan over
   fragment/name scrapers, raw and normalized storage, statuses, Inbox and scene view,
   no writes to Stash.
2. **Discovery** — URL extraction, handler matching, URL expansion, loop prevention,
   discovery graph and its display.
3. **Candidates** — correlation, scoring with breakdown, source agreement, field
   comparison matrix.
4. **Application** — per-field source selection, change preview, additive merge,
   `Apply selected` / `Apply candidate`, audit history.
5. **Automation** — tag batch tasks, cache TTLs, new-scraper scan, priorities,
   concurrency, stop conditions, Normal vs Deep.
6. **Advanced** — auto-apply, reprocessing tasks, advanced filtering, graph
   visualisation.

The scraper shim was pulled forward out of phase 6 and is built, together with the
conservative consensus rule it needs; see section 2.

Deliberately out of scope for now but not designed out: AI-assisted ranking, image/pHash
candidate matching, per-studio scraper routing, scheduled discovery, cross-scene
candidates, shared caches. Each is either a new scoring signal, a new attempt `method`,
or a new trigger — all extension points that already exist above.
