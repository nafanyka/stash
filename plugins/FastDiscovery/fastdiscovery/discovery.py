"""One scene's discovery run: every stash-box, then every URL, recursively.

The contract is short and absolute: this module acquires and stores. It never writes to
the scene, and it never decides which answer is right. What it leaves behind is a run
whose sources, results and URL graph are all on disk, waiting for a human.

The shape of a run:

    depth 0   the scene's own URLs, and every configured stash-box
              -> the stash-box results contribute URLs to the same depth-0 pool
    depth 0   every URL in that pool, through every scraper that can reach it
    depth 1   the URLs those results returned, and so on
    ...       until nothing new turns up, or maxDepth, or a limit

Two guards make the recursion safe, and both live in the database rather than in a set
this function has to remember to update: `urls(run_id, norm_key)` is unique, so a URL
enters the frontier once however many results mention it, and `sources(run_id,
source_key)` is unique, so a (scraper, URL) pair is attempted once. A cycle
A -> B -> A therefore terminates on the second sight of A (requirement 51).

Nothing stops after a match. Every stash-box runs even if the first one identified the
scene, and every scraper that can read a URL is asked, because the entire point is to
put all of the answers in front of the user at once (requirement 3).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import time

from . import (executor, fields, logs, registry as registry_module,
               urls as urls_module)
from .db import repo as R

HEARTBEAT_EVERY = 3

_DATA_URI = re.compile(
    r"^data:(?P<mime>image/[\w.+-]+)?(?:;charset=[\w-]+)?;base64,(?P<data>.*)$",
    re.IGNORECASE | re.DOTALL)

# A scraped cover above this is not a cover. Refusing it keeps a broken scraper from
# putting eight megabytes of something into the review database.
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class SceneMissing(RuntimeError):
    """The scene the run was asked about does not exist any more."""


def split_data_uri(value):
    """{sha256, mime, data} for a base64 image data URI, or None.

    Untrusted input: the MIME type must actually be an image and the payload must
    decode, or it is refused rather than stored.
    """
    match = _DATA_URI.match(str(value or "").strip())
    if not match:
        return None
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "mime": (match.group("mime") or "image/jpeg").lower(),
            "data": data}


class Runner:
    """Executes one run. Holds no state between runs beyond what is in the database."""

    def __init__(self, client, repo, config, schema=None, registry=None):
        self.client = client
        self.repo = repo
        self.config = config
        self._schema = schema
        self._registry = registry

    # -- setup -------------------------------------------------------------

    def registry(self):
        if self._registry is None:
            scrapers = registry_module.from_list_scrapers(
                self.client.list_scene_scrapers())
            self._registry = registry_module.Registry(
                scrapers, self.client.stash_boxes(), self.config)
        return self._registry

    def selection(self):
        if self._schema is None:
            from . import stash
            self._schema = stash.Schema.load(self.client, self.repo)
        return self._schema.selection

    # -- the run -----------------------------------------------------------

    def run(self, scene_id, trigger="manual", job_id=None, replace=True,
            progress_hook=None):
        scene_id = int(scene_id)
        scene = self.client.find_scene(scene_id)
        if not scene:
            raise SceneMissing("scene %s does not exist" % scene_id)
        snapshot = fields.scene_snapshot(scene)

        if replace:
            # A rescan replaces whatever was waiting: keeping both would leave two
            # answers for one scene and no way to say which the review meant
            # (requirement 22). The confirmation happens in the UI, before we get here.
            for status in (list(R.REVIEWABLE) + [R.NO_RESULTS, R.RUNNING, R.FAILED]):
                while True:
                    previous = self.repo.latest_run(scene_id, [status])
                    if not previous:
                        break
                    self.repo.delete_run(previous["id"])

        run_id = self.repo.start_run(scene_id, trigger, self.config.as_dict(), snapshot,
                                     job_id=job_id)
        state = _State(run_id, scene_id, snapshot)
        registry = self.registry()

        logs.info("scene %s: run %s started - %d stash-box(es), %d scene url(s)"
                  % (scene_id, run_id, len(registry.stash_boxes),
                     len(snapshot["urls"])))

        # The scene is a source of the review like any other (requirement 19). It is
        # never scraped; the row exists so the source list is the whole picture.
        self.repo.add_terminal_source(
            run_id, scene_id,
            {"type": "current", "method": "", "name": "Current", "depth": 0,
             "source_key": "current", "attribution": registry_module.CERTAIN},
            R.S_OK)

        try:
            self._seed_scene_urls(state)
            self._run_wave(state, registry.box_sources(snapshot.get("search_term")),
                           progress_hook)
            self._expand_urls(state, progress_hook)
            status = self._final_status(state)
            counted = self.repo.source_counts(run_id)
            self.repo.update_run_counts(
                run_id, source_count=counted["total"], ok_source_count=counted["ok"],
                error_count=counted["errors"], url_count=self.repo.url_count(run_id),
                result_count=state.results, max_depth_reached=state.max_depth)
            self.repo.finish_run(run_id, status, stop_reason=state.stop_reason)
        except Exception as exc:
            message = executor.describe_error(exc)
            logs.error("scene %s: run %s failed - %s" % (scene_id, run_id, message))
            self.repo.finish_run(run_id, R.FAILED, error=message)
            raise

        logs.info("scene %s: run %s %s - %d source(s), %d with results, %d error(s), "
                  "%d result(s), %d url(s)%s"
                  % (scene_id, run_id, status, state.sources, state.ok, state.errors,
                     state.results, self.repo.url_count(run_id),
                     (" - stopped: " + state.stop_reason) if state.stop_reason else ""))
        return state.summary(status)

    def _final_status(self, state):
        if state.results:
            return R.READY_WITH_ERRORS if state.errors else R.READY_FOR_REVIEW
        # Nothing usable came back. That is still a finished run and still worth
        # showing - with its errors - rather than reporting a failure that did not
        # happen (requirement 29).
        return R.READY_WITH_ERRORS if state.errors else R.NO_RESULTS

    # -- urls --------------------------------------------------------------

    def _seed_scene_urls(self, state):
        """The scene's own URLs are the start of the pool (requirement 4)."""
        registry = self.registry()
        for record in state.snapshot["urls"]:
            handlers = [entry["id"] for entry in registry.handlers_for(record["url"])]
            self.repo.add_url(state.run_id, record, 0, role=urls_module.ROLE_SCENE,
                              origin="scene", handler_ids=handlers,
                              state=R.U_PENDING if handlers else R.U_NO_HANDLER,
                              note=None if handlers
                              else "no installed scraper matches this URL")

    def _record_urls(self, state, source, result_id, raw):
        """Everything a result mentions, recorded with where it came from.

        Depth is the discovery generation: a URL from a stash-box or from the scene is
        depth 0, and a URL from a scraper working at depth N is depth N + 1. Related
        URLs - a performer's homepage, a studio's site - are recorded for the graph but
        never scraped: they are not scene pages, and asking a scene scraper about one
        burns an attempt for nothing.
        """
        registry = self.registry()
        depth = 0 if source["type"] in ("current", "stashbox") \
            else int(source.get("depth") or 0) + 1
        limit = int(self.config["maxUrlsPerRun"])
        added = 0

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
                added += 1
                logs.debug("scene %s: new url at depth %d from %s - %s"
                           % (state.scene_id, depth, source.get("name"),
                              record["normalized"]))
        return added

    def _expand_urls(self, state, progress_hook):
        """Walk the URL frontier, one depth at a time, until it stops growing."""
        registry = self.registry()
        max_depth = int(self.config["maxDepth"])
        if not self.config["recursiveUrlDiscovery"]:
            # Depth 0 only: the scene's URLs and the ones the stash-boxes returned.
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
                    # Not silently dropped: Stash cannot aim a URL scrape at a specific
                    # scraper, and pretending the URL was fully covered would be a lie
                    # the review could not see through (requirement 29, L1).
                    self.repo.add_terminal_source(
                        state.run_id, state.scene_id,
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
                logs.info("scene %s: depth %d - %d scrape(s) over %d url(s)"
                          % (state.scene_id, depth, len(wave), len(pending)))
                state.max_depth = max(state.max_depth, depth)
                self._run_wave(state, wave, progress_hook)
            depth += 1

        # Anything still pending was left alone on purpose; say which limit did it.
        for extra_depth in range(max_depth + 1, int(self.config["maxDepth"]) + 2):
            for row in self.repo.pending_urls(state.run_id, extra_depth):
                self.repo.set_url_state(row["id"], R.U_SKIPPED_DEPTH,
                                        "beyond maxDepth (%s)" % self.config["maxDepth"])

    def _source_of_result(self, result_id):
        result = self.repo.result(result_id)
        return result["source_id"] if result else None

    # -- one wave of sources ------------------------------------------------

    def _run_wave(self, state, sources, progress_hook):
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
            source_id = self.repo.begin_source(state.run_id, state.scene_id, source)
            if source_id is None:
                logs.debug("scene %s: skipping already-attempted %s"
                           % (state.scene_id, source["source_key"]))
            return source_id

        def work(source, source_id):
            if source_id is None:
                return None          # already attempted in this run; nothing to do
            return self._invoke(source, state, selection)

        def on_done(outcome):
            self._persist(state, outcome)
            if state.done % HEARTBEAT_EVERY == 0:
                self.repo.heartbeat(state.run_id, state.progress())
                if progress_hook:
                    progress_hook(state)

        def on_skip(source, reason):
            self.repo.add_terminal_source(state.run_id, state.scene_id, source,
                                          R.S_SKIPPED, reason)

        executor.run(planned, work, on_start, on_done,
                     concurrency=int(self.config["maxConcurrentScrapers"]),
                     deadline=deadline, on_skip=on_skip)
        self.repo.heartbeat(state.run_id, state.progress())

    def _invoke(self, source, state, selection):
        """The network call for one source. Runs on a worker thread: no database."""
        from . import stash
        timeout = float(self.config["scraperTimeout"])
        try:
            if source["method"] == registry_module.M_URL:
                return self.client.scrape_scene_url(source["url"], selection,
                                                    timeout=timeout)
            return self.client.scrape_scene(
                registry_module.graphql_source(source),
                registry_module.graphql_input(source, state.scene_id),
                selection, timeout=timeout)
        except stash.StashTimeout as exc:
            # Raised as TimeoutError so the executor can tell it apart from a scraper
            # that is genuinely broken - which is a different thing to tell the user.
            raise TimeoutError(str(exc)) from exc

    # -- persistence --------------------------------------------------------

    def _persist(self, state, outcome):
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
            logs.warning("%s: timed out" % _label(source))
            return
        if outcome.error is not None:
            self.repo.finish_source(source_id, R.S_ERROR, outcome.duration_ms, 0,
                                    outcome.error)
            state.errors += 1
            logs.warning("%s: %s" % (_label(source), outcome.error))
            return

        payloads = [one for one in (outcome.value or []) if isinstance(one, dict)]
        cap = int(self.config["maxResultsPerSource"])
        stored, urls_found = 0, 0
        for ordinal, payload in enumerate(payloads[:cap]):
            if _is_empty(payload):
                # Some scrapers answer with an object whose every field is null. That
                # is a no-match wearing a hat, and storing it would put an empty column
                # in front of the user.
                continue
            cleaned, image_url, image_sha = self._externalise_image(payload)
            result_id = self.repo.add_result(state.run_id, source_id, ordinal, cleaned,
                                             image_url, image_sha)
            stored += 1
            state.results += 1
            urls_found += self._record_urls(state, source, result_id, payload)

        if stored:
            self.repo.finish_source(source_id, R.S_OK, outcome.duration_ms, stored)
            state.ok += 1
            logs.info("%s: %s%s" % (_label(source), _describe(payloads[0]),
                                    (", %d new url(s)" % urls_found) if urls_found
                                    else ""))
        else:
            self.repo.finish_source(source_id, R.S_NO_RESULT, outcome.duration_ms, 0)
            logs.debug("%s: nothing" % _label(source))

    def _externalise_image(self, payload):
        """(payload, image_url, image_sha256).

        An image given as an http(s) URL is kept as a URL and never downloaded: it is
        already addressable, and downloading dozens of covers to show a thumbnail grid
        would be exactly the behaviour requirement 42 rules out. An image given as a
        base64 data URI has no address, so its bytes go to the blob table - once per
        distinct image, however many sources returned it - and the payload keeps a
        reference, which is enough to rebuild the original byte for byte on apply.
        """
        image = payload.get("image")
        if not image:
            return payload, None, None
        text = str(image).strip()
        if urls_module.is_safe(text):
            return payload, text, None
        blob = split_data_uri(text)
        if not blob:
            cleaned = dict(payload)
            cleaned["image"] = None
            return cleaned, None, None
        self.repo.put_image(blob["sha256"], blob["mime"], blob["data"])
        cleaned = dict(payload)
        cleaned["image"] = {"$image": blob["sha256"], "mime": blob["mime"],
                            "bytes": len(blob["data"])}
        return cleaned, None, blob["sha256"]


def _is_empty(payload):
    for key, value in (payload or {}).items():
        if key == "__typename":
            continue
        if value not in (None, "", [], {}):
            return False
    return True


def _describe(payload):
    """A one-line summary of a result, for the log. Never the payload itself."""
    parts = []
    title = fields.clean((payload or {}).get("title"))
    if title:
        parts.append('title "%s"' % title[:80])
    if (payload or {}).get("date"):
        parts.append("date %s" % fields.clean(payload["date"]))
    for key in ("performers", "tags", "urls", "groups"):
        values = (payload or {}).get(key) or []
        if values:
            parts.append("%d %s" % (len(values), key))
    if (payload or {}).get("studio"):
        parts.append("studio")
    if (payload or {}).get("image"):
        parts.append("image")
    return ", ".join(parts) or "an empty result"


def _label(source):
    name = source.get("name") or source.get("scraper_id") or source.get("method")
    if source.get("url"):
        return "%s (%s)" % (name, source["url"][:80])
    return str(name)


class _State:
    """Counters, and the little cross-source knowledge a run needs."""

    def __init__(self, run_id, scene_id, snapshot):
        self.run_id = run_id
        self.scene_id = scene_id
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
        out.update({"run_id": self.run_id, "scene_id": self.scene_id, "status": status,
                    "stop_reason": self.stop_reason,
                    "seconds": round(time.monotonic() - self.started, 2)})
        return out


def make_runner(client, repo, config):
    logs.set_debug(config["debugLogging"])
    return Runner(client, repo, config)
