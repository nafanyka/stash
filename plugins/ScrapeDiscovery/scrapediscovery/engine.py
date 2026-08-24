"""One scene's discovery run.

The engine's whole contract is that it acquires and stores, and never writes to the
scene. Everything a later stage needs is on disk when it finishes: what was asked, of
whom, when, what came back verbatim, and what that means once normalised.

The pieces it coordinates:

    snapshot   what Stash already knows about the scene (normalize.scene_snapshot)
    plan       the ordered work list (registry.Registry.plan)
    cache      which of those attempts can be answered from the database
    execute    the rest, bounded and timed (executor.run)
    persist    attempts, raw results, blobs, discovered URLs

Phase 1 records the URLs a result mentions but does not follow them; `expand_urls`
turns that on and is what phase 2 flips. The recording is not wasted either way: a URL
stored now is a URL a later reprocess can expand without asking any website again.
"""

from __future__ import annotations

import time

from . import cache, executor, logs, normalize, registry, settings
from .db import migrations, repo as R

# How often the scan tells the database it is still alive and how far it has got.
HEARTBEAT_EVERY = 5


class Engine:
    def __init__(self, client, repo, config, schema=None, reg=None):
        self.client = client
        self.repo = repo
        self.config = config
        self.schema = schema
        self.registry = reg

    # -- setup -------------------------------------------------------------

    def ensure_registry(self):
        """Load the installed scrapers and record what changed since last time."""
        if self.registry is not None:
            return self.registry
        scrapers = registry.from_list_scrapers(self.client.list_scene_scrapers())
        changes = self.repo.sync_scrapers(scrapers)
        if changes["added"] or changes["changed"]:
            logs.info("scrapers: %d installed, %d new, %d changed since the last scan"
                      % (changes["total"], len(changes["added"]), len(changes["changed"])))
        self.registry = registry.Registry(scrapers, self.config, self.client.stash_boxes())
        return self.registry

    def ensure_schema(self):
        if self.schema is None:
            from . import stash
            self.schema = stash.Schema.load(self.client, self.repo)
        return self.schema

    # -- the scan ----------------------------------------------------------

    def scan(self, scene_id, mode=None, trigger="manual", ignore_cache=False,
             expand_urls=False, progress_hook=None):
        """Discover for one scene. Returns a summary dict; never raises for a
        scraper's failure, only for something that makes the scan impossible."""
        mode = mode or self.config["defaultMode"]
        scene_id = int(scene_id)

        scene = self.client.find_scene(scene_id)
        if not scene:
            raise ValueError("scene %s does not exist" % scene_id)
        snapshot = normalize.scene_snapshot(scene)

        reg = self.ensure_registry()
        selection = self.ensure_schema().selection

        plan = reg.plan(snapshot, mode, already_tried=self.repo.scrapers_tried(scene_id))
        limit = int(self.config["maxAttemptsPerScan"] or 0)
        dropped_for_limit = 0
        if limit and len(plan) > limit:
            dropped_for_limit = len(plan) - limit
            plan = plan[:limit]

        scan_id = self.repo.start_scan(
            scene_id, trigger, mode,
            {"mode": mode, "limits": {
                "maxAttemptsPerScan": self.config["maxAttemptsPerScan"],
                "maxUrlsPerScan": self.config["maxUrlsPerScan"],
                "maxDepth": self.config["maxDepth"],
                "sceneTimeBudget": self.config["sceneTimeBudget"],
                "maxConcurrency": self.config["maxConcurrency"]},
             "ignore_cache": bool(ignore_cache),
             "expand_urls": bool(expand_urls),
             "scraper_count": len(reg.enabled())},
            snapshot)

        logs.info("scene %s: %s scan started - %d attempt(s) planned%s"
                  % (scene_id, mode, len(plan),
                     (", %d dropped by maxAttemptsPerScan" % dropped_for_limit)
                     if dropped_for_limit else ""))

        state = _ScanState(scan_id, scene_id, snapshot, len(plan))
        state.mode_is_deep = mode == settings.DEEP
        self.repo.update_scan_counts(scan_id, scraper_count=len(reg.enabled()),
                                     attempt_count=0)

        try:
            self._run_stage(state, plan, selection, ignore_cache, depth=0,
                            progress_hook=progress_hook)

            if expand_urls:
                self._expand(state, selection, ignore_cache, progress_hook)

            status = R.SCAN_WARNINGS if state.errors else R.SCAN_COMPLETED
            self.repo.finish_scan(scan_id, status, stop_reason=state.stop_reason)
        except Exception as exc:  # something structural, not a scraper
            logs.error("scene %s: scan failed - %s" % (scene_id, executor.describe_error(exc)))
            self.repo.finish_scan(scan_id, R.SCAN_FAILED,
                                  error=executor.describe_error(exc))
            self.repo.refresh_scene_state(scene_id, snapshot)
            raise

        self._finalise(state, snapshot)
        return state.summary()

    # -- one wave of attempts ----------------------------------------------

    def _run_stage(self, state, plan, selection, ignore_cache, depth, progress_hook):
        if not plan:
            return

        config = self.config
        budget = float(config["sceneTimeBudget"] or 0)
        deadline = (state.started + budget) if budget else None

        pending = []
        for item in plan:
            cached = None
            if not ignore_cache:
                policy = cache.Policy(config, (item.get("scraper") or {}).get("id"))
                cached = self.repo.find_cached_attempt(
                    state.scene_id, item["target_key"],
                    (item.get("scraper") or {}).get("fingerprint"),
                    policy.as_callable())
            if cached:
                attempt_id = self.repo.record_cached_attempt(
                    state.scan_id, state.scene_id, cached,
                    parent_id=item.get("parent_id"), depth=depth)
                state.note_cached(cached, attempt_id)
                logs.debug("cache hit: %s -> %s (%s)"
                           % (item["target_key"], cached["status"], cached["finished_at"]))
                continue
            pending.append(item)

        if state.cached:
            logs.info("scene %s: %d attempt(s) answered from cache, %d to run"
                      % (state.scene_id, state.cached, len(pending)))

        def on_start(item):
            scraper = item.get("scraper")
            return self.repo.begin_attempt(
                state.scan_id, state.scene_id, item["method"], item.get("target") or "",
                item["target_key"], scraper=scraper, parent_id=item.get("parent_id"),
                depth=item.get("depth", depth),
                input_payload=self._input_preview(item, state),
                attribution=item.get("attribution", registry.CERTAIN))

        def work(item, _attempt_id):
            return self._invoke(item, state, selection)

        def on_done(outcome):
            self._persist(state, outcome, depth)
            if progress_hook and state.done % HEARTBEAT_EVERY == 0:
                progress_hook(state)
            if state.done % HEARTBEAT_EVERY == 0:
                self.repo.heartbeat(state.scan_id, state.progress())

        def on_skip(item, reason):
            attempt_id = self.repo.begin_attempt(
                state.scan_id, state.scene_id, item["method"], item.get("target") or "",
                item["target_key"], scraper=item.get("scraper"),
                parent_id=item.get("parent_id"), depth=item.get("depth", depth),
                input_payload={"skipped": reason},
                attribution=item.get("attribution", registry.CERTAIN))
            self.repo.finish_attempt(attempt_id, R.SKIPPED, 0, 0, error=reason)
            state.skipped += 1

        result = executor.run(
            pending, work, on_start, on_done,
            concurrency=int(config["maxConcurrency"]),
            deadline=deadline,
            should_stop=lambda: self._should_stop(state),
            on_skip=on_skip,
        )
        if result["stop_reason"] != executor.COMPLETE:
            state.stop_reason = result["stop_reason"]
        self.repo.heartbeat(state.scan_id, state.progress())

    def _invoke(self, item, state, selection):
        """The network call for one attempt. Runs on a worker thread: no database."""
        method = item["method"]
        timeout = self.config.timeout_for((item.get("scraper") or {}).get("id") or "",
                                          method)
        try:
            if method == registry.M_URL:
                return self.client.scrape_scene_url(item["target"], selection,
                                                    timeout=timeout)
            source = registry.source_for(item)
            scrape_input = registry.scrape_input_for(item, state.scene_id, state.snapshot)
            return self.client.scrape_scene(source, scrape_input, selection,
                                            timeout=timeout)
        except Exception as exc:
            from . import stash
            if isinstance(exc, stash.StashTimeout):
                # Signalled as TimeoutError so the executor can tell the two apart;
                # a timeout is worth retrying much sooner than a broken scraper.
                raise TimeoutError(str(exc))
            raise

    def _input_preview(self, item, state):
        """What was actually asked, stored so a result can be explained later."""
        preview = {"method": item["method"], "band": item.get("band")}
        if item.get("target"):
            preview["target"] = item["target"]
        if item.get("handlers"):
            preview["possible_handlers"] = item["handlers"]
        if item["method"] != registry.M_URL:
            try:
                preview["input"] = registry.scrape_input_for(item, state.scene_id,
                                                             state.snapshot)
            except ValueError:
                pass
        return preview

    # -- persistence --------------------------------------------------------

    def _persist(self, state, outcome, depth):
        """Store one finished attempt and whatever it returned."""
        attempt_id = outcome.context
        item = outcome.item
        state.done += 1

        if outcome.timed_out:
            self.repo.finish_attempt(attempt_id, R.TIMEOUT, outcome.duration_ms, 0,
                                     error=outcome.error, error_kind=cache.TRANSIENT)
            state.timeouts += 1
            logs.debug("timeout: %s" % item["target_key"])
            return
        if outcome.error is not None:
            kind = cache.classify_error(outcome.error)
            self.repo.finish_attempt(attempt_id, R.ERROR, outcome.duration_ms, 0,
                                     error=outcome.error, error_kind=kind)
            state.errors += 1
            if kind == cache.PERMANENT:
                state.permanent_errors += 1
                # The common case by a wide margin: a scraper that has nothing to do
                # with this scene, failing on input it cannot use. Debug level, or one
                # scan buries the log in a hundred identical warnings.
                logs.debug("%s failed (permanent): %s" % (self._label(item), outcome.error))
            else:
                logs.warning("%s failed: %s" % (self._label(item), outcome.error))
            return

        payloads = [payload for payload in (outcome.value or []) if payload]
        stored, kept_urls, titles = 0, 0, []
        for ordinal, payload in enumerate(payloads):
            externalized, images = normalize.externalize_images(payload)
            normalized = normalize.scene_result(payload)
            if normalize.is_empty_result(normalized):
                # Some scrapers answer a fragment scrape with an object whose every
                # field is null. That is a no-match wearing a hat, and storing it as a
                # result would put an empty candidate in front of the user.
                continue
            for image in images:
                self.repo.put_blob(image["sha256"], image["mime"], image["data"])
            result_id = self.repo.add_result(
                attempt_id, ordinal, externalized, normalize.fingerprint(externalized),
                normalized, normalize.fingerprint(normalized), migrations.NORM_VERSION,
                image_sha256=images[0]["sha256"] if images else None)
            stored += 1
            if len(titles) < 3:
                titles.append(normalized.get("title") or "(untitled)")
            state.note_agreement(normalized, item)
            kept_urls += self._record_urls(state, normalized, result_id,
                                           depth + 1, payload)

        if stored:
            self.repo.finish_attempt(attempt_id, R.MATCH, outcome.duration_ms, stored)
            state.matches += 1
            shown = ", ".join(titles)
            logs.info("%s: MATCH - %d result(s)%s%s"
                      % (self._label(item), stored, (" - " + shown) if shown else "",
                         (", %d new url(s)" % kept_urls) if kept_urls else ""))
        else:
            self.repo.finish_attempt(attempt_id, R.NO_MATCH, outcome.duration_ms, 0)
            state.no_matches += 1
            logs.debug("%s: NO_MATCH" % self._label(item))

    def _record_urls(self, state, normalized, result_id, depth, raw):
        """Record every URL a result mentions, with the scrapers that could follow it.

        Recorded whether or not this scan will follow them: the handler set is what
        makes a URL result attributable at all, and a URL stored now is one a later
        reprocess can expand without touching the network.
        """
        reg = self.registry
        added = 0
        entries = list(normalized.get("urls") or [])
        if self.config["expandRelatedUrls"]:
            for entry in normalize.extract_urls(raw):
                if entry["role"] != normalize.ROLE_RELATED:
                    continue
                parsed = normalize.normalize_url(entry["url"])
                if parsed:
                    entries.append({"url": entry["url"], "normalized": parsed["normalized"],
                                    "key": parsed["key"], "host": parsed["host"]})

        for entry in entries:
            if entry["key"] in state.scene_url_keys:
                # Already on the scene: it was in the plan as a depth-0 attempt, so
                # re-adding it would double-count and could loop.
                continue
            handlers = [scraper["id"] for scraper in reg.handlers_for_url(entry["url"])]
            url_id = self.repo.add_url(
                state.scan_id, state.scene_id, entry["url"], entry["normalized"],
                entry.get("host") or "", entry["key"], depth, handlers,
                source_result_id=result_id,
                state="PENDING" if handlers else "NO_HANDLER")
            if url_id:
                added += 1
                state.urls += 1
        return added

    # -- url expansion (phase 2) -------------------------------------------

    def _expand(self, state, selection, ignore_cache, progress_hook):
        """Follow discovered URLs, breadth first, within the configured limits."""
        max_depth = int(self.config["maxDepth"] or 0)
        max_urls = int(self.config["maxUrlsPerScan"] or 0)
        depth = 1
        while depth <= max_depth:
            pending = self.repo.pending_urls(state.scan_id, limit=max_urls or 200)
            wave = [row for row in pending if row["depth"] == depth]
            if not wave:
                break
            if max_urls and state.expanded >= max_urls:
                for row in wave:
                    self.repo.set_url_state(row["id"], "SKIPPED_LIMIT")
                state.stop_reason = state.stop_reason or "maxUrlsPerScan reached"
                break

            plan = []
            for row in wave:
                if max_urls and state.expanded + len(plan) >= max_urls:
                    self.repo.set_url_state(row["id"], "SKIPPED_LIMIT")
                    continue
                item = self.registry.url_work(
                    {"url": row["url"], "key": row["norm_key"], "host": row["host"]},
                    depth,
                    parent_attempt_id=self.repo.attempt_id_of_result(
                        row["source_result_id"]))
                if item is None:
                    self.repo.set_url_state(row["id"], "NO_HANDLER")
                    continue
                item["url_row_id"] = row["id"]
                plan.append(item)

            if not plan:
                break
            logs.info("scene %s: expanding %d url(s) at depth %d"
                      % (state.scene_id, len(plan), depth))
            state.expanded += len(plan)
            for item in plan:
                self.repo.set_url_state(item["url_row_id"], "SCRAPED")
            self._run_stage(state, plan, selection, ignore_cache, depth, progress_hook)
            depth += 1

        # Whatever is still pending was left alone on purpose; say which limit did it.
        for row in self.repo.pending_urls(state.scan_id, limit=1000):
            self.repo.set_url_state(row["id"], "SKIPPED_DEPTH"
                                    if row["depth"] > max_depth else "SKIPPED_LIMIT")

    # -- stop conditions ----------------------------------------------------

    def _should_stop(self, state):
        """Whether the scan has what it came for.

        Confidence-based stopping needs candidates, which are scored after acquisition,
        so in a scan this can only use what is already known: an early stop asks for a
        strong match confirmed by more than one source, and the cheapest honest proxy
        for that during acquisition is agreement on a canonical URL. Deep Scan never
        stops early, by definition.
        """
        if state.mode_is_deep:
            return None
        threshold = float(self.config["stopOnConfidence"] or 0)
        if not threshold:
            return None
        needed = int(self.config["stopMinIndependentSources"] or 1)
        for hosts in state.url_agreement.values():
            if len(hosts) >= needed:
                return "stop condition met: %d independent sources agree on a url" % len(hosts)
        return None

    # -- wrap-up ------------------------------------------------------------

    def _finalise(self, state, snapshot):
        self.repo.update_scan_counts(
            state.scan_id, attempt_count=state.total_attempts, match_count=state.matches,
            error_count=state.errors + state.timeouts, url_count=state.urls)
        self.repo.refresh_scene_state(state.scene_id, snapshot)
        logs.info("scene %s: done - %d match, %d no-match, %d error (%d of them "
                  "permanent), %d timeout, %d cached, %d skipped, %d url(s)%s"
                  % (state.scene_id, state.matches, state.no_matches, state.errors,
                     state.permanent_errors, state.timeouts, state.cached,
                     state.skipped, state.urls,
                     (" - stopped: " + state.stop_reason) if state.stop_reason else ""))

    def _label(self, item):
        scraper = (item.get("scraper") or {}).get("name")
        if scraper:
            return scraper
        if item.get("handlers"):
            return "url via %s" % "/".join(item["handlers"][:3])
        return item["method"]


class _ScanState:
    """Counters and the little bit of cross-attempt knowledge a scan needs."""

    def __init__(self, scan_id, scene_id, snapshot, planned):
        self.scan_id = scan_id
        self.scene_id = scene_id
        self.snapshot = snapshot
        self.planned = planned
        self.started = time.monotonic()
        self.done = 0
        self.matches = 0
        self.no_matches = 0
        self.errors = 0
        self.permanent_errors = 0
        self.timeouts = 0
        self.cached = 0
        self.skipped = 0
        self.urls = 0
        self.expanded = 0
        self.stop_reason = None
        self.mode_is_deep = False
        self.scene_url_keys = {entry["key"] for entry in (snapshot.get("urls") or [])}
        # canonical url -> hosts of the scrapers that reported it, for the early stop.
        self.url_agreement = {}

    @property
    def total_attempts(self):
        return self.done + self.cached + self.skipped

    def note_cached(self, cached_row, attempt_id):
        self.cached += 1
        if cached_row["status"] == R.MATCH:
            self.matches += 1

    def note_agreement(self, normalized, item):
        """Track which distinct sites reported each canonical URL.

        Hosts, not scrapers: three scrapers for one site are one witness, so counting
        scrapers would let a single source look like corroboration.
        """
        source = (item.get("host") or "") or ",".join(item.get("handlers") or []) \
            or str((item.get("scraper") or {}).get("id") or "")
        for entry in (normalized.get("urls") or []):
            self.url_agreement.setdefault(entry["key"], set()).add(source)

    def progress(self):
        return {
            "planned": self.planned, "done": self.done, "cached": self.cached,
            "matches": self.matches, "errors": self.errors + self.timeouts,
            "urls": self.urls, "skipped": self.skipped,
        }

    def fraction(self):
        if not self.planned:
            return 1.0
        return min(1.0, float(self.done + self.cached + self.skipped) / self.planned)

    def summary(self):
        out = self.progress()
        out.update({"scan_id": self.scan_id, "scene_id": self.scene_id,
                    "no_matches": self.no_matches, "timeouts": self.timeouts,
                    "stop_reason": self.stop_reason,
                    "duration_s": round(time.monotonic() - self.started, 2)})
        return out


def make_engine(client, repo, config):
    logs.set_debug(config["debugLogging"])
    return Engine(client, repo, config)
