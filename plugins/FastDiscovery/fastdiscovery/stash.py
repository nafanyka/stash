"""The Stash API adapter - the only module in the plugin that contains GraphQL.

Every operation here exists in Stash v0.31.1 and was checked against the schema and
resolvers at that tag; none is invented. The notes that matter to callers:

* **one scraper per request, always.** `internal/api/resolver_query_scraper.go` returns
  the scraper's error for the whole query, so batching two scrapers into one request
  would let a broken one take out a good one. One request per source is what makes
  requirement 29 - a failed source never fails the run - true by construction.
* **the request timeout is the scraper timeout.** Script scrapers run under
  `stashExec.CommandContext(ctx, ...)`, so abandoning the HTTP request cancels the
  request context and kills the scraper process. Nothing else can stop a hung scraper.
* **`scrapeSceneURL` picks the handler itself**, by iterating a Go map, so with several
  matching scrapers the winner is neither deterministic nor reported. Callers get the
  matching set from registry.py and record the attribution honestly instead.
* **stash-boxes are Stash's**, not ours: the endpoints come from
  `configuration.general.stashBoxes` and each is invoked through
  `scrapeSingleScene(source: {stash_box_endpoint: ...})`, which is Stash's own
  fingerprint lookup. The `api_key` that query also returns is never read, stored or
  logged (requirement 2, 43).

Standard library only, deliberately: the interpreter Stash shells out to is often a
bare system Python, and an ImportError looks exactly like a broken plugin.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

from . import logs

DEFAULT_TIMEOUT = 30.0


class StashError(RuntimeError):
    """Transport failure, HTTP error, or a non-empty GraphQL `errors` array."""


class StashTimeout(StashError):
    """The request was abandoned, which also cancelled it on the server."""


# Everything ScrapedScene offers in 0.31.1. `Schema` trims this to what the running
# server actually declares, so an older or newer Stash degrades instead of erroring.
_SCENE_FIELDS = (
    "title", "code", "details", "director", "url", "urls", "date", "image",
    "remote_site_id", "duration",
    "file { size duration video_codec audio_codec width height framerate bitrate }",
    "studio { stored_id name url urls image details aliases remote_site_id"
    " parent { stored_id name } }",
    "tags { stored_id name description remote_site_id }",
    "performers { stored_id name disambiguation gender url urls birthdate country"
    " ethnicity aliases image details remote_site_id }",
    "groups { stored_id name date duration director urls synopsis"
    " studio { stored_id name } }",
    "movies { stored_id name date duration director url synopsis }",
    "fingerprints { algorithm hash duration }",
)

# What FastDiscovery needs to know about a scene from Stash itself. `paths.screenshot`
# is the current cover, which is one of the images the review offers.
SCENE_FIELDS = """
id title code details director date urls organized rating100 updated_at
paths { screenshot }
files { id path basename size duration width height fingerprints { type value } }
studio { id name }
performers { id name disambiguation gender }
tags { id name }
groups { group { id name } scene_index }
stash_ids { endpoint stash_id }
"""

# The cheap shape, for lists of many scenes.
SCENE_BRIEF = "id title date paths { screenshot } studio { id name } files { basename }"


class Client:
    """A thin GraphQL client for one Stash server."""

    def __init__(self, url, api_key=None, cookie=None, default_timeout=DEFAULT_TIMEOUT):
        self.url = url
        self.api_key = api_key
        self.cookie = cookie
        self.default_timeout = default_timeout
        self._version = None

    # -- transport ---------------------------------------------------------

    def call(self, query, variables=None, timeout=None):
        """Run an operation and return `data`, or raise.

        A timeout raises StashTimeout rather than StashError so a caller can record it
        as TIMEOUT: the difference matters to a user reading the source list.
        """
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.cookie:
            headers["Cookie"] = self.cookie

        request = urllib.request.Request(self.url, data=payload, headers=headers,
                                         method="POST")
        limit = timeout or self.default_timeout
        try:
            with urllib.request.urlopen(request, timeout=limit) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except socket.timeout as exc:
            raise StashTimeout("timed out after %ss" % limit) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except OSError:
                pass
            hint = " (API key missing or wrong?)" if exc.code in (401, 403) else ""
            raise StashError(("HTTP %s from %s%s %s"
                              % (exc.code, self.url, hint, detail)).strip()) from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), socket.timeout):
                raise StashTimeout("timed out connecting to %s" % self.url) from exc
            raise StashError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise StashError("%s: %s" % (self.url, exc)) from exc

        if body.get("errors"):
            raise StashError("; ".join(str(one.get("message", "?"))
                                       for one in body["errors"]))
        data = body.get("data")
        if data is None:
            raise StashError("response carried no data")
        return data

    def try_call(self, query, variables=None, timeout=None):
        """For probing a query shape that may not exist on this Stash version."""
        try:
            return self.call(query, variables, timeout)
        except StashError:
            return None

    # -- server ------------------------------------------------------------

    def version(self):
        if self._version is None:
            data = self.try_call("query { version { version hash build_time } }",
                                 timeout=15)
            self._version = (data or {}).get("version") or {}
        return self._version

    def plugin_settings(self, plugin_id):
        data = self.try_call("query { configuration { plugins } }", timeout=15)
        plugins = ((data or {}).get("configuration") or {}).get("plugins") or {}
        values = plugins.get(plugin_id)
        return values if isinstance(values, dict) else {}

    def save_plugin_settings(self, plugin_id, values):
        return self.call(
            "mutation($id: ID!, $input: Map!)"
            " { configurePlugin(plugin_id: $id, input: $input) }",
            {"id": plugin_id, "input": values}, timeout=30)

    def config_dir(self):
        """Where Stash keeps its configuration, for the default database location.

        A plugin is handed this on stdin as `server_connection.Dir`; this query is the
        fallback for something running outside a plugin invocation. Both paths it can
        offer may be relative to Stash's own working directory, and a relative path is
        no use to another process, so anything without a directory part is discarded.
        """
        data = self.try_call(
            "query { configuration { general { databasePath configFilePath } } }",
            timeout=15)
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        for path in (general.get("configFilePath"), general.get("databasePath")):
            path = str(path or "")
            head = path.rsplit("\\", 1)[0] if "\\" in path else path.rsplit("/", 1)[0]
            if head and head != path:
                return head
        return ""

    # -- sources -----------------------------------------------------------

    def stash_boxes(self):
        """Every stash-box configured in Stash, newest configuration each time.

        Only `endpoint` and `name` are selected. `api_key` is on the same type and is
        deliberately not asked for: FastDiscovery never handles a stash-box credential,
        because Stash already does (requirement 2).
        """
        data = self.try_call(
            "query { configuration { general { stashBoxes { endpoint name } } } }",
            timeout=15)
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        return [box for box in (general.get("stashBoxes") or []) if box.get("endpoint")]

    def list_scene_scrapers(self):
        """Every installed scraper that can produce a scene, with its URL patterns."""
        data = self.call(
            "query { listScrapers(types: [SCENE]) { id name"
            " scene { urls supported_scrapes } } }", timeout=60)
        return data.get("listScrapers") or []

    # -- scenes ------------------------------------------------------------

    def find_scene(self, scene_id):
        data = self.call("query($id: ID!) { findScene(id: $id) { %s } }" % SCENE_FIELDS,
                         {"id": str(scene_id)}, timeout=30)
        return data.get("findScene")

    def find_scenes_brief(self, scene_ids):
        """Just enough about several scenes to list them on the results page."""
        ids = [str(one) for one in (scene_ids or [])]
        if not ids:
            return []
        data = self.try_call(
            "query($ids: [ID!]) { findScenes(ids: $ids,"
            " filter: {per_page: -1}) { scenes { %s } } }" % SCENE_BRIEF,
            {"ids": ids}, timeout=60)
        return ((data or {}).get("findScenes") or {}).get("scenes") or []

    # -- scraping ----------------------------------------------------------

    def scrape_scene(self, source, scrape_input, selection, timeout=None):
        """`scrapeSingleScene` for one source. Returns a list of raw payloads.

        `source` is {"scraper_id": id} or {"stash_box_endpoint": url}; `scrape_input`
        is exactly one of scene_id, scene_input or query - Stash's resolver checks them
        in that order and ignores the rest, so sending two would hide which one ran.
        """
        query = ("query FDScrape($source: ScraperSourceInput!,"
                 " $input: ScrapeSingleSceneInput!) {"
                 " scrapeSingleScene(source: $source, input: $input) { %s } }" % selection)
        data = self.call(query, {"source": source, "input": scrape_input}, timeout=timeout)
        return data.get("scrapeSingleScene") or []

    def scrape_scene_url(self, url, selection, timeout=None):
        """`scrapeSceneURL`. Returns a list of zero or one payload.

        Stash chooses the scraper and does not say which; see the module docstring.
        Normalised to a list so callers treat URL and fragment results the same way.
        """
        query = ("query FDScrapeURL($url: String!) { scrapeSceneURL(url: $url)"
                 " { %s } }" % selection)
        data = self.call(query, {"url": url}, timeout=timeout)
        result = data.get("scrapeSceneURL")
        return [result] if result else []

    # -- entity lookup, for resolving a review selection -------------------

    def find_performers_by_names(self, names):
        """name -> [{id, name, disambiguation}] for the names given, matched exactly.

        One request for the lot: apply resolves a handful of names and a request each
        would be a handful of process-blocking round trips.
        """
        return self._find_by_names(
            "findPerformers", "performer_filter",
            "performers { id name disambiguation }", names)

    def find_tags_by_names(self, names):
        return self._find_by_names("findTags", "tag_filter", "tags { id name }", names)

    def find_studios_by_names(self, names):
        return self._find_by_names("findStudios", "studio_filter",
                                   "studios { id name }", names)

    def find_groups_by_names(self, names):
        return self._find_by_names("findGroups", "group_filter",
                                   "groups { id name }", names)

    def _find_by_names(self, query_name, filter_arg, selection, names):
        """name -> the records carrying exactly that name.

        `filter` is the paging argument and is a `FindFilterType` for every one of these
        queries - it is not the entity's own filter type, which goes in inline above it.
        Declaring the variable as, say, `TagFilterType` is a GraphQL *validation* error:
        the server rejects the whole query before running it, and this client turns a
        rejected query into an empty answer. Every name lookup then reports that nothing
        exists, which reads exactly like a library that really has nothing - until Apply
        tries to create it and Stash says it is already there.
        """
        found = {}
        for name in {str(one).strip() for one in (names or []) if str(one).strip()}:
            data = self.try_call(
                "query($name: String!, $filter: FindFilterType) { %s(%s: {name:"
                " {value: $name, modifier: EQUALS}}, filter: $filter) { %s } }"
                % (query_name, filter_arg, selection),
                {"name": name, "filter": {"per_page": 10}}, timeout=20)
            if data is None:
                # Not "no such record": the question never got an answer. Said out loud,
                # because the cost of failing quietly here is entities created twice.
                logs.warning("%s could not be asked about %r - name matching is degraded"
                          % (query_name, name))
                found[name] = []
                continue
            container = data.get(query_name) or {}
            rows = next((value for value in container.values()
                         if isinstance(value, list)), [])
            found[name] = rows
        return found

    # -- entity creation, only ever for an explicitly selected candidate ---

    def create_performer(self, values):
        data = self.call(
            "mutation($input: PerformerCreateInput!) { performerCreate(input: $input)"
            " { id name } }", {"input": values}, timeout=60)
        return data.get("performerCreate")

    def create_tag(self, values):
        data = self.call(
            "mutation($input: TagCreateInput!) { tagCreate(input: $input) { id name } }",
            {"input": values}, timeout=60)
        return data.get("tagCreate")

    def create_studio(self, values):
        data = self.call(
            "mutation($input: StudioCreateInput!) { studioCreate(input: $input)"
            " { id name } }", {"input": values}, timeout=60)
        return data.get("studioCreate")

    def create_group(self, values):
        data = self.call(
            "mutation($input: GroupCreateInput!) { groupCreate(input: $input)"
            " { id name } }", {"input": values}, timeout=60)
        return data.get("groupCreate")

    # -- writing -----------------------------------------------------------

    def scene_update(self, values):
        """The one write FastDiscovery performs, and only from Apply.

        Everything selected goes in a single `sceneUpdate`: the fields it does not
        mention are untouched, and the set-like fields it does mention are replaced by
        exactly the set the review showed as ticked. That is the reviewed intent, and
        one mutation means the scene cannot end up half-applied (requirement 55).
        """
        data = self.call(
            "mutation($input: SceneUpdateInput!) { sceneUpdate(input: $input)"
            " { id updated_at } }", {"input": values}, timeout=120)
        return data.get("sceneUpdate")

    # -- jobs --------------------------------------------------------------

    def run_plugin_task(self, plugin_id, task_name, args=None):
        """Queue a plugin task and return its job id.

        How the UI starts a run: the work belongs in Stash's job queue, where it gets a
        progress bar and a stop button, not in the request that asked for it.
        """
        data = self.call(
            "mutation($id: ID!, $task: String, $args: Map)"
            " { runPluginTask(plugin_id: $id, task_name: $task, args_map: $args) }",
            {"id": plugin_id, "task": task_name, "args": args or {}}, timeout=30)
        return data.get("runPluginTask")

    def find_job(self, job_id):
        data = self.try_call(
            "query($input: FindJobInput!) { findJob(input: $input)"
            " { id status progress description error } }",
            {"input": {"id": str(job_id)}}, timeout=15)
        return (data or {}).get("findJob")

    def stop_job(self, job_id):
        data = self.try_call("mutation($id: ID!) { stopJob(job_id: $id) }",
                             {"id": str(job_id)}, timeout=15)
        return bool((data or {}).get("stopJob"))


class Schema:
    """Which of the ScrapedScene fields we would like actually exist on this server.

    A selection naming a field the running Stash does not define fails the whole query,
    so the selection is built from introspection rather than hope. The answer is cached
    against the server's build hash, so the cost is one introspection per Stash upgrade
    rather than one per run. Introspection is also what lets a field added by a future
    Stash show up in the review automatically (fields.extra_fields).
    """

    INTROSPECT = 'query { __type(name: "ScrapedScene") { fields { name } } }'

    FALLBACK = ("title date url urls details image code director studio { name }"
                " performers { name } tags { name }")

    def __init__(self, field_names):
        self.field_names = set(field_names or ())

    @property
    def selection(self):
        if not self.field_names:
            return self.FALLBACK
        parts = []
        for entry in _SCENE_FIELDS:
            name = entry.split(" ", 1)[0].split("{", 1)[0].strip()
            if name in self.field_names:
                parts.append(entry)
        return " ".join(parts) or self.FALLBACK

    @classmethod
    def load(cls, client, repo=None):
        stamp = (client.version() or {}).get("hash") or "unknown"
        if repo is not None:
            cached = repo.meta_get("scraped_scene_fields")
            if cached and repo.meta_get("scraped_scene_fields_version") == stamp:
                try:
                    return cls(json.loads(cached))
                except (TypeError, ValueError):
                    pass
        data = client.try_call(cls.INTROSPECT, timeout=30)
        names = [field["name"] for field
                 in (((data or {}).get("__type") or {}).get("fields") or [])]
        if repo is not None and names:
            repo.meta_set("scraped_scene_fields", json.dumps(sorted(names)))
            repo.meta_set("scraped_scene_fields_version", stamp)
        return cls(names)


def from_plugin_input(payload):
    """(url, api_key, cookie, config_dir) from what Stash puts on a plugin's stdin.

    A plugin is handed `server_connection`, including a session cookie, so it needs no
    API key of its own. The listen address is not necessarily reachable as written -
    0.0.0.0 is the usual case - so it is rewritten to localhost.
    """
    connection = (payload or {}).get("server_connection") or {}
    scheme = str(connection.get("Scheme") or "http").lower()
    host = str(connection.get("Host") or "").strip()
    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "localhost"
    port = connection.get("Port") or 9999

    jar = connection.get("SessionCookie") or {}
    cookie = None
    if jar.get("Name") and jar.get("Value"):
        cookie = "%s=%s" % (jar["Name"], jar["Value"])

    return ("%s://%s:%s/graphql" % (scheme, host, port),
            os.environ.get("STASH_API_KEY"), cookie,
            str(connection.get("Dir") or ""))
