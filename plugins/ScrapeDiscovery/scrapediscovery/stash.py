"""The Stash API adapter - the only module in the plugin that contains GraphQL.

Everything here was checked against a live Stash v0.31.1 and the v0.31.1 sources; no
operation is invented. Notes that matter to callers:

* one scraper per request, always. A scraper error makes the whole GraphQL query fail
  (internal/api/resolver_query_scraper.go), and one installed scraper on the test
  instance panics the resolver outright, so batching scrapers would let one bad
  scraper take out a whole batch of good ones.
* the request timeout *is* the scraper timeout. Script scrapers run under
  `stashExec.CommandContext(ctx, ...)`, so abandoning the HTTP request cancels the
  request context and kills the scraper process. Nothing else can stop a hung scraper.
* `scrapeSceneURL` picks the handler itself, and does so by iterating a Go map, so
  with several matching scrapers the choice is not deterministic and is not reported.
  Callers get the matching set from registry.py instead and record attribution
  accordingly.

Standard library only, deliberately: the interpreter Stash shells out to is often a
bare system Python, and an ImportError looks exactly like a broken plugin.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30.0


class StashError(RuntimeError):
    """Transport failure, HTTP error, or a non-empty GraphQL `errors` array."""


class StashTimeout(StashError):
    """The request was abandoned, which also cancelled it on the server."""


# ---------------------------------------------------------------- field sets

# Everything ScrapedScene offers in 0.31.1. `Schema` trims this to what the running
# server actually declares, so an older Stash degrades instead of erroring.
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

# The scene fields ScrapeDiscovery needs from Stash itself.
SCENE_QUERY_FIELDS = """
id title code details director date urls organized rating100 created_at updated_at
files { id path basename size duration width height video_codec audio_codec
        frame_rate bit_rate fingerprints { type value } }
studio { id name }
performers { id name disambiguation gender }
tags { id name }
groups { group { id name } }
stash_ids { endpoint stash_id }
"""


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

        Timeouts raise StashTimeout so a caller can record TIMEOUT rather than ERROR:
        the difference matters, because a timeout is worth retrying sooner than a
        scraper that is genuinely broken.
        """
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.cookie:
            headers["Cookie"] = self.cookie

        request = urllib.request.Request(self.url, data=payload, headers=headers,
                                         method="POST")
        try:
            with urllib.request.urlopen(request,
                                        timeout=timeout or self.default_timeout) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except socket.timeout as exc:
            raise StashTimeout("timed out after %ss" % (timeout or self.default_timeout)) from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except OSError:
                pass
            hint = " (API key missing or wrong?)" if exc.code in (401, 403) else ""
            message = "HTTP %s from %s%s %s" % (exc.code, self.url, hint, detail)
            raise StashError(message.strip()) from exc
        except urllib.error.URLError as exc:
            # urllib wraps a socket timeout here when it happens during connect.
            if isinstance(getattr(exc, "reason", None), socket.timeout):
                raise StashTimeout("timed out connecting to %s" % self.url) from exc
            raise StashError("cannot reach %s: %s" % (self.url, exc.reason)) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise StashError("%s: %s" % (self.url, exc)) from exc

        if body.get("errors"):
            raise StashError("; ".join(str(e.get("message", "?")) for e in body["errors"]))
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
            data = self.try_call("query { version { version hash build_time } }", timeout=15)
            self._version = (data or {}).get("version") or {}
        return self._version

    def plugin_settings(self, plugin_id):
        """The raw settings map Stash holds for a plugin, or {}."""
        data = self.try_call("query { configuration { plugins } }", timeout=15)
        plugins = ((data or {}).get("configuration") or {}).get("plugins") or {}
        values = plugins.get(plugin_id)
        return values if isinstance(values, dict) else {}

    def save_plugin_settings(self, plugin_id, values):
        return self.call(
            "mutation($id: ID!, $input: Map!) { configurePlugin(plugin_id: $id, input: $input) }",
            {"id": plugin_id, "input": values},
        )

    def config_dir(self):
        """Where Stash keeps its configuration, for the default database location.

        Plugins are handed this on stdin as `server_connection.Dir`, already absolute;
        this query is only the fallback for something running outside a plugin
        invocation. Both paths it can offer may be relative to Stash's own working
        directory - the test instance reports `configFilePath` as plain "config.yml" -
        and a relative path is no use to a different process, so anything without a
        directory component is discarded rather than guessed at.
        """
        data = self.try_call(
            "query { configuration { general { databasePath configFilePath } } }", timeout=15)
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        for path in (general.get("configFilePath"), general.get("databasePath")):
            path = str(path or "")
            head = path.rsplit("\\", 1)[0] if "\\" in path else path.rsplit("/", 1)[0]
            if head and head != path:
                return head
        return ""

    # -- scrapers ----------------------------------------------------------

    def list_scene_scrapers(self):
        """Every installed scraper that can produce a scene, with its URL patterns."""
        data = self.call(
            "query { listScrapers(types: [SCENE]) { id name"
            " scene { urls supported_scrapes } } }",
            timeout=60,
        )
        return data.get("listScrapers") or []

    def stash_boxes(self):
        data = self.try_call(
            "query { configuration { general { stashBoxes { endpoint name } } } }",
            timeout=15)
        general = ((data or {}).get("configuration") or {}).get("general") or {}
        return [box for box in (general.get("stashBoxes") or []) if box.get("endpoint")]

    def installed_scraper_packages(self):
        """The scraper packages Stash installed from a source.

        A package id equals the scraper id in practice - verified on a live instance,
        779 of 780 scene scrapers matched, the exception being the built-in autotagger,
        which is not a package and cannot be uninstalled. The mapping is still built by
        lookup rather than assumed, so a scraper without a package is simply reported as
        not uninstallable.
        """
        data = self.try_call(
            "query { installedPackages(type: Scraper)"
            " { package_id name version sourceURL } }", timeout=60)
        return (data or {}).get("installedPackages") or []

    def uninstall_scraper_package(self, package_id, source_url):
        """Remove one scraper package. Returns the job id Stash runs it under.

        Destructive, and asynchronous: Stash deletes the files the package installed
        (`pkg/pkg/manager.go`) in a job, so the caller has a job to watch and the
        scraper stays loaded until `reload_scrapers` runs.
        """
        data = self.call(
            "mutation($packages: [PackageSpecInput!]!) {"
            " uninstallPackages(type: Scraper, packages: $packages) }",
            {"packages": [{"id": str(package_id), "sourceURL": str(source_url)}]},
            timeout=60,
        )
        return data.get("uninstallPackages")

    def reload_scrapers(self):
        """Make Stash re-read the scrapers directory, so a removed one disappears."""
        data = self.try_call("mutation { reloadScrapers }", timeout=60)
        return bool((data or {}).get("reloadScrapers"))

    # -- scenes ------------------------------------------------------------

    def find_scene(self, scene_id):
        data = self.call(
            "query($id: ID!) { findScene(id: $id) { %s } }" % SCENE_QUERY_FIELDS,
            {"id": str(scene_id)}, timeout=30,
        )
        return data.get("findScene")

    def find_scenes(self, scene_filter=None, page=1, per_page=100, sort="id",
                    direction="ASC", ids=None):
        variables = {
            "filter": {"page": int(page), "per_page": int(per_page), "sort": sort,
                       "direction": direction},
            "scene_filter": scene_filter or {},
        }
        query = ("query($filter: FindFilterType, $scene_filter: SceneFilterType,"
                 " $ids: [ID!]) { findScenes(filter: $filter,"
                 " scene_filter: $scene_filter, ids: $ids)"
                 " { count scenes { %s } } }" % SCENE_QUERY_FIELDS)
        if ids:
            variables["ids"] = [str(one) for one in ids]
        data = self.call(query, variables, timeout=120)
        return data.get("findScenes") or {"count": 0, "scenes": []}

    def scene_ids_with_tag(self, tag_ids, page=1, per_page=500, exclude_organized=False):
        """Scene ids carrying any of `tag_ids`. Ids only - batch runs read them by the
        thousand and the full scene is fetched per scene, when it is its turn."""
        scene_filter = {
            "tags": {"value": [str(one) for one in tag_ids], "modifier": "INCLUDES",
                     "depth": -1},
        }
        if exclude_organized:
            scene_filter["organized"] = False
        data = self.call(
            "query($filter: FindFilterType, $scene_filter: SceneFilterType)"
            " { findScenes(filter: $filter, scene_filter: $scene_filter)"
            " { count scenes { id } } }",
            {"filter": {"page": int(page), "per_page": int(per_page), "sort": "id",
                        "direction": "ASC"},
             "scene_filter": scene_filter},
            timeout=120,
        )
        result = data.get("findScenes") or {}
        return result.get("count") or 0, [scene["id"] for scene in (result.get("scenes") or [])]

    # -- scraping ----------------------------------------------------------

    def scrape_scene(self, source, scrape_input, selection, timeout=None):
        """`scrapeSingleScene` for one source. Returns a list of raw payloads.

        `source` is {"scraper_id": id} or {"stash_box_endpoint": url}; `scrape_input`
        is exactly one of scene_id, scene_input or query - Stash's resolver checks
        them in that order and ignores the rest.
        """
        query = ("query SDScrape($source: ScraperSourceInput!,"
                 " $input: ScrapeSingleSceneInput!) {"
                 " scrapeSingleScene(source: $source, input: $input) { %s } }" % selection)
        data = self.call(query, {"source": source, "input": scrape_input}, timeout=timeout)
        return data.get("scrapeSingleScene") or []

    def scrape_scene_url(self, url, selection, timeout=None):
        """`scrapeSceneURL`. Returns a list of zero or one raw payload.

        Stash chooses the scraper; see the module docstring. Normalised to a list so
        callers handle URL and fragment results the same way.
        """
        query = ("query SDScrapeURL($url: String!) { scrapeSceneURL(url: $url)"
                 " { %s } }" % selection)
        data = self.call(query, {"url": url}, timeout=timeout)
        result = data.get("scrapeSceneURL")
        return [result] if result else []

    # -- tags --------------------------------------------------------------

    def find_tag_by_name(self, name):
        data = self.try_call(
            "query($name: String!) { findTags(tag_filter: {name: {value: $name,"
            " modifier: EQUALS}}, filter: {per_page: 5}) { tags { id name } } }",
            {"name": str(name)}, timeout=15,
        )
        tags = ((data or {}).get("findTags") or {}).get("tags") or []
        for tag in tags:
            if str(tag.get("name", "")).lower() == str(name).lower():
                return tag
        return None

    def create_tag(self, name):
        data = self.call(
            "mutation($name: String!) { tagCreate(input: {name: $name}) { id name } }",
            {"name": str(name)}, timeout=30,
        )
        return data.get("tagCreate")

    def find_performer_by_name(self, name):
        data = self.try_call(
            "query($name: String!) { findPerformers(performer_filter: {name:"
            " {value: $name, modifier: EQUALS}}, filter: {per_page: 5})"
            " { performers { id name disambiguation } } }",
            {"name": str(name)}, timeout=15,
        )
        performers = ((data or {}).get("findPerformers") or {}).get("performers") or []
        return performers[0] if performers else None

    def find_studio_by_name(self, name):
        data = self.try_call(
            "query($name: String!) { findStudios(studio_filter: {name: {value: $name,"
            " modifier: EQUALS}}, filter: {per_page: 5}) { studios { id name } } }",
            {"name": str(name)}, timeout=15,
        )
        studios = ((data or {}).get("findStudios") or {}).get("studios") or []
        return studios[0] if studios else None

    # -- writing (used only by the apply engine) ---------------------------

    def scene_update(self, values):
        """Singular fields only. `urls`, `tag_ids` and `performer_ids` here replace
        the whole list, so the apply engine uses bulk_scene_update for those."""
        data = self.call(
            "mutation($input: SceneUpdateInput!) { sceneUpdate(input: $input)"
            " { id } }", {"input": values}, timeout=60,
        )
        return data.get("sceneUpdate")

    def bulk_scene_add(self, scene_id, tag_ids=None, performer_ids=None, urls=None):
        """Add to set-like fields without touching what is already there.

        `bulkSceneUpdate` takes a mode per field, so ADD is done by the server against
        the current row. That removes the read-modify-write race a plain sceneUpdate
        would have, and makes it impossible to drop a value the user added meanwhile.
        """
        values = {"ids": [str(scene_id)]}
        if tag_ids:
            values["tag_ids"] = {"ids": [str(one) for one in tag_ids], "mode": "ADD"}
        if performer_ids:
            values["performer_ids"] = {"ids": [str(one) for one in performer_ids],
                                       "mode": "ADD"}
        if urls:
            values["urls"] = {"values": list(urls), "mode": "ADD"}
        if len(values) == 1:
            return None
        data = self.call(
            "mutation($input: BulkSceneUpdateInput!) { bulkSceneUpdate(input: $input)"
            " { id } }", {"input": values}, timeout=60,
        )
        return data.get("bulkSceneUpdate")

    def bulk_scene_remove_tags(self, scene_id, tag_ids):
        if not tag_ids:
            return None
        data = self.call(
            "mutation($input: BulkSceneUpdateInput!) { bulkSceneUpdate(input: $input)"
            " { id } }",
            {"input": {"ids": [str(scene_id)],
                       "tag_ids": {"ids": [str(one) for one in tag_ids],
                                   "mode": "REMOVE"}}},
            timeout=60,
        )
        return data.get("bulkSceneUpdate")

    # -- jobs --------------------------------------------------------------

    def run_plugin_task(self, plugin_id, task_name, args=None):
        """Queue a plugin task and return its job id.

        This is how the UI starts a scan: the work belongs in Stash's job queue, where
        it gets a progress bar and a stop button, not in the request that asked for it.
        """
        data = self.call(
            "mutation($id: ID!, $task: String, $args: Map)"
            " { runPluginTask(plugin_id: $id, task_name: $task, args_map: $args) }",
            {"id": plugin_id, "task": task_name, "args": args or {}}, timeout=30,
        )
        return data.get("runPluginTask")

    def find_job(self, job_id):
        data = self.try_call(
            "query($input: FindJobInput!) { findJob(input: $input)"
            " { id status progress description error subTasks } }",
            {"input": {"id": str(job_id)}}, timeout=15,
        )
        return (data or {}).get("findJob")

    def stop_job(self, job_id):
        data = self.try_call(
            "mutation($id: ID!) { stopJob(job_id: $id) }", {"id": str(job_id)}, timeout=15)
        return bool((data or {}).get("stopJob"))

    def job_queue(self):
        data = self.try_call(
            "query { jobQueue { id status progress description } }", timeout=15)
        return (data or {}).get("jobQueue") or []


class Schema:
    """Which of the fields we would like to ask for this server actually has.

    A `ScrapedScene` selection naming a field the running Stash does not define fails
    the whole query, so the selection is built from introspection rather than hope.
    The answer is cached in the database against the server version, so the cost is
    one introspection per Stash upgrade rather than one per scan.
    """

    INTROSPECT = """
    query { __type(name: "ScrapedScene") { fields { name } } }
    """

    def __init__(self, fields):
        self.fields = set(fields or ())

    @property
    def selection(self):
        """The ScrapedScene selection to use, trimmed to supported fields."""
        if not self.fields:
            # Introspection failed; fall back to the fields that have existed for as
            # long as scene scraping has, so a scan still runs.
            return "title date url details image studio { name } performers { name } tags { name }"
        parts = []
        for entry in _SCENE_FIELDS:
            name = entry.split(" ", 1)[0].split("{", 1)[0].strip()
            if name in self.fields:
                parts.append(entry)
        return " ".join(parts) or "title date url"

    def to_json(self):
        return {"fields": sorted(self.fields)}

    @classmethod
    def load(cls, client, repo=None):
        version = (client.version() or {}).get("hash") or "unknown"
        if repo is not None:
            cached = repo.meta_get("scraped_scene_fields")
            stamp = repo.meta_get("scraped_scene_fields_version")
            if cached and stamp == version:
                try:
                    return cls(json.loads(cached))
                except (TypeError, ValueError):
                    pass
        data = client.try_call(cls.INTROSPECT, timeout=30)
        fields = [field["name"] for field in
                  (((data or {}).get("__type") or {}).get("fields") or [])]
        if repo is not None and fields:
            repo.meta_set("scraped_scene_fields", json.dumps(sorted(fields)))
            repo.meta_set("scraped_scene_fields_version", version)
        return cls(fields)


def from_plugin_input(payload):
    """(url, api_key, cookie, config_dir, plugin_dir) from what Stash puts on stdin.

    A plugin is handed `server_connection`, including a session cookie, so it needs no
    API key of its own. The listen address is not necessarily reachable as written -
    0.0.0.0 is the usual case - so it is rewritten to localhost.
    """
    import os

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

    url = "%s://%s:%s/graphql" % (scheme, host, port)
    return (url, os.environ.get("STASH_API_KEY"), cookie,
            str(connection.get("Dir") or ""), str(connection.get("PluginDir") or ""))
