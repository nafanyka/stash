"""One performer's Fast/Full discovery run.

`discovery.py`'s sibling, for performers instead of scenes. The contract is the same:
this module acquires and stores, never writes to the performer, and never decides
which answer is right. Two real shape differences from a scene's run, both confined
to this module:

* **the seed is a name search, not a stash-box fingerprint lookup.** A performer has
  no fingerprint Stash can match on; discovery starts from a chosen set of installed
  performer-name scrapers, each searched with the performer's current display name.
  Requirement 3 (never stop after one source answers, never take only the first
  result) holds exactly as it does for scenes: every scraper in the set is asked,
  and *every* result each one returns becomes its own column - `_persist` already
  stores every payload a source returns, up to `maxResultsPerSource`, which is
  precisely "Babepedia returned five people, keep all five" with no special case
  needed here.
* **FAST and FULL are the same run, extended.** Fast runs a chosen subset of
  installed scrapers; Full runs the rest. Both write into the same `run_id`, and the
  database's own uniqueness guard on `sources(run_id, source_key)` is what stops Full
  from re-invoking a scraper Fast already tried (requirement: never repeat a Fast
  scraper on Full) - nothing here has to remember which scrapers ran; it is read
  back from the sources already stored.

URL discovery afterwards - the frontier, the loop guard, the depth limit, aiming an
otherwise-ambiguous URL scrape at every scraper that also supports FRAGMENT - is the
exact same mechanism scenes use (`registry.Registry.url_sources`, `urls.py`,
`executor.py`), just pointed at a `Registry` built from performer scrapers and at
`scrapePerformerURL`/`scrapeSinglePerformer` instead of the scene equivalents.
"""

from __future__ import annotations

import time

from . import executor, fields, logs, registry as registry_module, urls as urls_module
from .db import repo as R
from .discovery import _is_empty, split_data_uri

HEARTBEAT_EVERY = 3

ENTITY_TYPE = "performer"


class PerformerMissing(RuntimeError):
    """The performer the run was asked about does not exist any more."""


class PerformerRunner:
    """Executes one performer run. Holds no state beyond what is in the database."""

    def __init__(self, client, repo, config, schema=None, registry=None):
        self.client = client
        self.repo = repo
        self.config = config
        self._schema = schema
        self._registry = registry

    # -- setup ---------------------------------------------------------

    def registry(self):
        """Installed performer-name-and-URL scrapers, with the config applied.

        Built with an empty box list on purpose: stash-boxes are never optional here
        and never go through the settings picklist `Registry` filters against, so
        they are asked directly (`registry_module.performer_box_sources`,
        `self.client.stash_boxes()`), not through this `Registry` at all.
        """
        if self._registry is None:
            scrapers = registry_module.from_list_scrapers(
                self.client.list_performer_scrapers(), content_key="performer")
            self._registry = registry_module.Registry(scrapers, [], self.config)
        return self._registry

    def selection(self):
        if self._schema is None:
            from . import stash
            self._schema = stash.PerformerSchema.load(self.client, self.repo)
        return self._schema.selection

    def fast_scraper_ids(self):
        """The performer-name scrapers picked in settings, resolved to real scraper
        ids and filtered to what is actually installed right now (requirement 23: a
        removed scraper drops out silently rather than failing the run). Stash-boxes
        are not in this list at all - see `_run_name_and_box_waves`.

        The setting itself may be the FastDiscovery settings page's own JSON array,
        or a plain comma-separated list typed straight into Stash's generic
        Settings -> Plugins text box (`settings.parse_choice_list` handles both), and
        each entry is matched against installed scrapers by id *or* display name,
        case-insensitively - someone typing "StashDB" should not have to know
        whether that is also the scraper's internal id.
        """
        from . import settings as settings_module
        wanted = settings_module.parse_choice_list(self.config["performerFastScrapers"])
        if not wanted:
            return []
        by_key = {}
        for entry in self.registry().scrapers:
            by_key[entry["id"]] = entry["id"]
            by_key[entry["id"].casefold()] = entry["id"]
            by_key[entry["name"].casefold()] = entry["id"]
        out = []
        for choice in wanted:
            real = by_key.get(choice) or by_key.get(choice.casefold())
            if real and real not in out:
                out.append(real)
        return out

    def _run_name_and_box_waves(self, state, registry, scraper_ids, name, progress_hook):
        """Stash-boxes first, then the picked scrapers - in that priority order, one
        wave fully finished before the next starts (requirement: stash-boxes are
        never optional and always come first, exactly as they do for a scene).

        Boxes are unconditional and every one of them is asked every time, Fast or
        Full alike - never gated behind the picklist, the same way
        `discovery.Runner` always asks every configured stash-box for a scene. A box
        Full asks that Fast already asked in this same run is a harmless no-op: the
        unique `(run_id, source_key)` index on `sources` is what actually prevents
        the repeat, not any bookkeeping here.
        """
        boxes = registry_module.performer_box_sources(self.client.stash_boxes(), name)
        if boxes:
            self._run_wave(state, boxes, registry, progress_hook)
        scrapers = registry_module.performer_name_sources(registry, scraper_ids, name)
        if scrapers:
            self._run_wave(state, scrapers, registry, progress_hook)

    # -- the run ---------------------------------------------------------

    def run_fast(self, performer_id, trigger="manual", job_id=None, replace=True,
                progress_hook=None):
        """Start (or replace) a Fast run: every stash-box, then the scrapers the
        settings page picked."""
        scraper_ids = self.fast_scraper_ids()
        if not scraper_ids and not self.client.stash_boxes():
            logs.warning("performer %s: no stash-boxes are configured and no Fast "
                        "performer scrapers are picked in settings" % performer_id)
        return self._start(performer_id, scraper_ids, "FAST", trigger, job_id, replace,
                           progress_hook)

    def run_full(self, run_id, progress_hook=None):
        """Top up an existing run with every installed scraper Fast did not use
        (stash-boxes already ran unconditionally during Fast)."""
        run = self.repo.run(run_id)
        if not run or run["entity_type"] != ENTITY_TYPE:
            raise ValueError("no such performer run")
        performer_id = int(run["scene_id"])
        performer = self.client.find_performer(performer_id)
        if not performer:
            raise PerformerMissing("performer %s does not exist" % performer_id)

        registry = self.registry()
        attempted_scrapers = {source["scraper_id"] for source in self.repo.sources_of(run_id)
                              if source["method"] == registry_module.M_PERFORMER_NAME
                              and source.get("scraper_id")}
        remaining_scrapers = [entry["id"] for entry in registry.scrapers
                              if entry["id"] not in attempted_scrapers]

        # Visibly RUNNING for as long as Full takes, exactly like a fresh run - the
        # Results page and the review's own polling both key off this rather than a
        # separate "is Full going" flag.
        self.repo.set_run_status(run_id, R.RUNNING)
        snapshot = fields.performer_snapshot(performer)
        state = _State(run_id, performer_id, snapshot)
        # `_finish` writes these as absolute totals for the run, not "how many this
        # pass added" - Full is a second pass over the *same* run, so its counters
        # have to start from what Fast already left, or finishing Full would
        # overwrite Fast's contribution instead of adding to it.
        state.sources = self.repo.source_counts(run_id)["total"]
        state.url_total = self.repo.url_count(run_id)
        state.results = int(run.get("result_count") or 0)
        state.max_depth = int(run.get("max_depth_reached") or 0)

        logs.info("performer %s: run %s going to Full - %d scraper(s) left to try "
                  "(stash-boxes were already asked during Fast)"
                  % (performer_id, run_id, len(remaining_scrapers)))
        try:
            # Re-asking every stash-box costs nothing (the source_key guard skips
            # ones already answered) and picks up a box added since Fast ran.
            self._run_name_and_box_waves(state, registry, remaining_scrapers,
                                         snapshot["search_term"], progress_hook)
            self._expand_urls(state, registry, progress_hook)
            # Absolute counts, covering Fast's sources too - not just what this Full
            # pass added - so a Fast-only error is not forgotten once Full succeeds.
            state.errors = self.repo.source_counts(run_id)["errors"]
            status = self._final_status(state)
            self._finish(run_id, state, status)
            self.repo.set_run_mode(run_id, "FULL")
        except Exception as exc:
            # Full runs *on top of* a result that was already reviewable. If Full
            # itself blows up - not one scraper failing, which `executor.run` already
            # turns into a per-source error and never raises - the run must not come
            # out FAILED: that would throw away Fast's results, which are still sound
            # and still sitting in the database untouched. So this recomputes the
            # status from what is actually stored (Fast's results, plus whatever of
            # Full's had already been persisted before the crash) and leaves `mode`
            # at FAST, which is the honest description of what actually finished.
            message = executor.describe_error(exc)
            logs.error("performer %s: run %s - Full discovery failed, keeping Fast's "
                      "results reviewable - %s" % (performer_id, run_id, message))
            state.errors = self.repo.source_counts(run_id)["errors"]
            status = self._final_status(state)
            self.repo.update_run_counts(
                run_id, source_count=self.repo.source_counts(run_id)["total"],
                ok_source_count=self.repo.source_counts(run_id)["ok"],
                error_count=state.errors, url_count=self.repo.url_count(run_id),
                result_count=state.results, max_depth_reached=state.max_depth)
            self.repo.finish_run(run_id, status, stop_reason="Full discovery failed: "
                                                             + message)
            raise
        return state.summary(status)

    def _start(self, performer_id, scraper_ids, mode, trigger, job_id, replace,
              progress_hook):
        performer_id = int(performer_id)
        performer = self.client.find_performer(performer_id)
        if not performer:
            raise PerformerMissing("performer %s does not exist" % performer_id)
        snapshot = fields.performer_snapshot(performer)

        if replace:
            for status in (list(R.REVIEWABLE) + [R.NO_RESULTS, R.RUNNING, R.FAILED]):
                while True:
                    previous = self.repo.latest_run(performer_id, [status],
                                                    entity_type=ENTITY_TYPE)
                    if not previous:
                        break
                    self.repo.delete_run(previous["id"])

        run_id = self.repo.start_run(performer_id, trigger, self.config.as_dict(),
                                     snapshot, job_id=job_id, entity_type=ENTITY_TYPE,
                                     mode=mode)
        state = _State(run_id, performer_id, snapshot)
        registry = self.registry()

        logs.info("performer %s: run %s started (%s) - %d stash-box(es), %d "
                  "scraper(s), %d performer url(s)"
                  % (performer_id, run_id, mode, len(self.client.stash_boxes()),
                     len(scraper_ids), len(snapshot["urls"])))

        self.repo.add_terminal_source(
            run_id, performer_id,
            {"type": "current", "method": "", "name": "Current", "depth": 0,
             "source_key": "current", "attribution": registry_module.CERTAIN},
            R.S_OK)

        try:
            self._seed_urls(state, registry)
            self._run_name_and_box_waves(state, registry, scraper_ids,
                                         snapshot["search_term"], progress_hook)
            self._expand_urls(state, registry, progress_hook)
            status = self._final_status(state)
            self._finish(run_id, state, status)
        except Exception as exc:
            message = executor.describe_error(exc)
            logs.error("performer %s: run %s failed - %s"
                      % (performer_id, run_id, message))
            self.repo.finish_run(run_id, R.FAILED, error=message)
            raise

        logs.info("performer %s: run %s %s - %d source(s), %d with results, %d "
                  "error(s), %d result(s), %d url(s)"
                  % (performer_id, run_id, status, state.sources, state.ok,
                     state.errors, state.results, self.repo.url_count(run_id)))
        return state.summary(status)

    def _finish(self, run_id, state, status):
        counted = self.repo.source_counts(run_id)
        self.repo.update_run_counts(
            run_id, source_count=counted["total"], ok_source_count=counted["ok"],
            error_count=counted["errors"], url_count=self.repo.url_count(run_id),
            result_count=state.results, max_depth_reached=state.max_depth)
        self.repo.finish_run(run_id, status, stop_reason=state.stop_reason)

    def _final_status(self, state):
        if state.results:
            return R.READY_WITH_ERRORS if state.errors else R.READY_FOR_REVIEW
        return R.READY_WITH_ERRORS if state.errors else R.NO_RESULTS

    # -- urls --------------------------------------------------------------

    def _seed_urls(self, state, registry):
        for record in state.snapshot["urls"]:
            handlers = [entry["id"] for entry in registry.handlers_for(record["url"])]
            self.repo.add_url(state.run_id, record, 0, role=urls_module.ROLE_SCENE,
                              origin="performer", handler_ids=handlers,
                              state=R.U_PENDING if handlers else R.U_NO_HANDLER,
                              note=None if handlers
                              else "no installed scraper matches this URL")

    def _record_urls(self, state, source, result_id, raw, registry):
        depth = 0 if source["type"] in ("current", "performer_name") \
            else int(source.get("depth") or 0) + 1
        limit = int(self.config["maxUrlsPerRun"])

        for entry in urls_module.from_result(raw):
            record = urls_module.normalize(entry["url"])
            if not record:
                continue
            if entry["role"] != urls_module.ROLE_SCENE:
                self.repo.add_url(state.run_id, record, depth, role=entry["role"],
                                  origin=entry["source"],
                                  discovered_by_result_id=result_id,
                                  handler_ids=[], state=R.U_RELATED)
                continue
            if state.url_total >= limit:
                state.stop_reason = state.stop_reason or "maxUrlsPerRun reached"
                break
            handlers = [one["id"] for one in registry.handlers_for(record["url"])]
            url_id = self.repo.add_url(
                state.run_id, record, depth, role=entry["role"], origin=entry["source"],
                discovered_by_result_id=result_id, handler_ids=handlers,
                state=R.U_PENDING if handlers else R.U_NO_HANDLER,
                note=None if handlers else "no installed scraper matches this URL")
            if url_id is not None:
                state.url_total += 1

    def _expand_urls(self, state, registry, progress_hook):
        max_depth = int(self.config["performerMaxUrlDepth"])
        if not self.config["recursiveUrlDiscovery"]:
            max_depth = 0

        depth = 0
        while depth <= max_depth:
            pending = self.repo.pending_urls(state.run_id, depth)
            if not pending:
                depth += 1
                continue

            wave, seen_parents = [], {}
            for row in pending:
                parent = row["discovered_by_result_id"]
                if parent is not None and parent not in seen_parents:
                    seen_parents[parent] = self._source_of_result(parent)
                sources, unreachable = registry.url_sources(
                    {"url": row["url"], "key": row["norm_key"], "host": row["host"]},
                    depth, parent_source_id=seen_parents.get(parent))
                for source in sources:
                    source["url_row_id"] = row["id"]
                    wave.append(source)
                for entry in unreachable:
                    self.repo.add_terminal_source(
                        state.run_id, state.performer_id,
                        {"type": "url_scraper", "method": registry_module.M_URL,
                         "name": entry["name"], "scraper_id": entry["scraper_id"],
                         "url": entry["url"], "url_key": row["norm_key"],
                         "host": row["host"], "depth": depth,
                         "source_key": "UNREACHABLE|%s|%s" % (entry["scraper_id"],
                                                              row["norm_key"]),
                         "attribution": registry_module.AMBIGUOUS},
                        R.S_UNREACHABLE, entry["reason"])
                self.repo.set_url_state(row["id"], R.U_SCRAPED)

            if wave:
                state.max_depth = max(state.max_depth, depth)
                self._run_wave(state, wave, registry, progress_hook)
            depth += 1

        for extra_depth in range(max_depth + 1,
                                 int(self.config["performerMaxUrlDepth"]) + 2):
            for row in self.repo.pending_urls(state.run_id, extra_depth):
                self.repo.set_url_state(
                    row["id"], R.U_SKIPPED_DEPTH,
                    "beyond performerMaxUrlDepth (%s)"
                    % self.config["performerMaxUrlDepth"])

    def _source_of_result(self, result_id):
        result = self.repo.result(result_id)
        return result["source_id"] if result else None

    # -- one wave of sources ------------------------------------------------

    def _run_wave(self, state, sources, registry, progress_hook):
        if not sources:
            return
        selection = self.selection()
        budget = float(self.config["runTimeBudget"] or 0)
        deadline = (state.started + budget) if budget else None
        ceiling = int(self.config["maxSourcesPerRun"])

        planned = []
        for source in sources:
            if state.sources + len(planned) >= ceiling:
                state.stop_reason = state.stop_reason or "maxSourcesPerRun reached"
                break
            source["source_key"] = registry_module.source_key(source)
            planned.append(source)

        def on_start(source):
            source_id = self.repo.begin_source(state.run_id, state.performer_id, source)
            if source_id is None:
                logs.debug("performer %s: skipping already-attempted %s"
                          % (state.performer_id, source["source_key"]))
            return source_id

        def work(source, source_id):
            if source_id is None:
                return None
            return self._invoke(source, selection)

        def on_done(outcome):
            self._persist(state, outcome, registry)
            if state.done % HEARTBEAT_EVERY == 0:
                self.repo.heartbeat(state.run_id, state.progress())
                if progress_hook:
                    progress_hook(state)

        def on_skip(source, reason):
            self.repo.add_terminal_source(state.run_id, state.performer_id, source,
                                          R.S_SKIPPED, reason)

        executor.run(planned, work, on_start, on_done,
                     concurrency=int(self.config["maxConcurrentScrapers"]),
                     deadline=deadline, on_skip=on_skip)
        self.repo.heartbeat(state.run_id, state.progress())

    def _invoke(self, source, selection):
        """The network call for one source. Runs on a worker thread: no database.

        `M_URL` is the one method with no `ScraperSourceInput` at all - Stash picks
        the handler itself (`scrapePerformerURL`). Every other method - a name
        search against a scraper_id, a name search against a stash-box, or an aimed
        URL fragment - goes through `scrapeSinglePerformer`, and `graphql_source` /
        `performer_graphql_input` already know which `{scraper_id}` or
        `{stash_box_endpoint}` and which input shape each one needs.
        """
        from . import stash
        timeout = float(self.config["scraperTimeout"])
        try:
            if source["method"] == registry_module.M_URL:
                return self.client.scrape_performer_url(source["url"], selection,
                                                        timeout=timeout)
            return self.client.scrape_single_performer(
                registry_module.graphql_source(source),
                registry_module.performer_graphql_input(source), selection,
                timeout=timeout)
        except stash.StashTimeout as exc:
            raise TimeoutError(str(exc)) from exc

    # -- persistence --------------------------------------------------------

    def _persist(self, state, outcome, registry):
        source_id = outcome.context
        source = outcome.item
        if source_id is None:
            return
        state.done += 1
        state.sources += 1

        if outcome.timed_out:
            self.repo.finish_source(source_id, R.S_TIMEOUT, outcome.duration_ms, 0,
                                    outcome.error)
            state.errors += 1
            return
        if outcome.error is not None:
            self.repo.finish_source(source_id, R.S_ERROR, outcome.duration_ms, 0,
                                    outcome.error)
            state.errors += 1
            logs.warning("%s: %s" % (source.get("name"), outcome.error))
            return

        payloads = [one for one in (outcome.value or []) if isinstance(one, dict)]
        cap = int(self.config["maxResultsPerSource"])
        stored = 0
        for ordinal, payload in enumerate(payloads[:cap]):
            if _is_empty(payload):
                continue
            cleaned, image_refs = self._externalise_images(payload)
            result_id = self.repo.add_result(state.run_id, source_id, ordinal, cleaned)
            self.repo.add_result_images(result_id, image_refs)
            stored += 1
            state.results += 1
            self._record_urls(state, source, result_id, payload, registry)

        if stored:
            self.repo.finish_source(source_id, R.S_OK, outcome.duration_ms, stored)
            state.ok += 1
            logs.info("%s: %d result(s)" % (source.get("name"), stored))
        else:
            self.repo.finish_source(source_id, R.S_NO_RESULT, outcome.duration_ms, 0)

    def _externalise_images(self, payload):
        """(payload, [{url|sha256}, ...]).

        `ScrapedPerformer.images` (plural) is the field to prefer; the deprecated
        singular `image` is used only if a scraper still sends that instead. Every
        photo given as an http(s) URL is kept as a URL and never downloaded; every one
        given as a base64 data URI has its bytes stored once, keyed by sha256, exactly
        as a scene's single cover already is (requirement 42, L7) - the only
        difference here is that there can be several per result.
        """
        raw_images = payload.get("images")
        candidates = list(raw_images) if isinstance(raw_images, list) else []
        if not candidates and payload.get("image"):
            candidates = [payload["image"]]

        refs, cleaned_list = [], []
        for value in candidates:
            text = str(value or "").strip()
            if not text:
                continue
            if urls_module.is_safe(text):
                refs.append({"url": text})
                cleaned_list.append(text)
                continue
            blob = split_data_uri(text)
            if not blob:
                continue
            self.repo.put_image(blob["sha256"], blob["mime"], blob["data"])
            refs.append({"sha256": blob["sha256"]})
            cleaned_list.append("blob:" + blob["sha256"])

        cleaned = dict(payload)
        cleaned["images"] = cleaned_list
        cleaned.pop("image", None)
        return cleaned, refs


class _State:
    """Counters for one performer run."""

    def __init__(self, run_id, performer_id, snapshot):
        self.run_id = run_id
        self.performer_id = performer_id
        self.snapshot = snapshot
        self.started = time.monotonic()
        self.done = 0
        self.sources = 0
        self.ok = 0
        self.errors = 0
        self.results = 0
        self.url_total = len(snapshot.get("urls") or [])
        self.max_depth = 0
        self.stop_reason = None

    def progress(self):
        return {"sources": self.sources, "ok": self.ok, "errors": self.errors,
                "results": self.results, "urls": self.url_total,
                "depth": self.max_depth}

    def summary(self, status):
        out = self.progress()
        out.update({"run_id": self.run_id, "performer_id": self.performer_id,
                    "status": status, "stop_reason": self.stop_reason,
                    "seconds": round(time.monotonic() - self.started, 2)})
        return out


def make_runner(client, repo, config):
    logs.set_debug(config["debugLogging"])
    return PerformerRunner(client, repo, config)
