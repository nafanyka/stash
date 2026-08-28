#!/usr/bin/env python3
"""FastDiscovery, as a scene scraper: a thin entry point, and nothing else.

This file contains no discovery logic. There is one engine, in the FastDiscovery
plugin, and this asks it a single question over GraphQL:

    runPluginOperation(plugin_id: "FastDiscovery", args: {op: "scraper.entry", ...})

and then always prints `null`.

That last part is the whole design, not a shortcoming. A scene scraper's return value
is what Stash offers to write into the scene, and *Identify* writes it with no review
at all. FastDiscovery exists to put every source's answer in front of a person before
anything is written, so handing a merged scene back here would defeat it. Being
registered as a scraper is worth it anyway: it puts FastDiscovery in the *Scrape
with...* menu, where people already reach for it, and from there it starts a run and
tells you where to review it.

Requires the FastDiscovery **plugin** to be installed and enabled. Scrapers - unlike
plugins - are not handed the server's connection details, so see config.ini.example if
Stash is not on localhost:9999 or has authentication enabled.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config, graphql, log  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

PLUGIN_ID = "FastDiscovery"

# A plugin operation spawns a Python process on the server. This one never scrapes, so
# it stays quick, but it does open a database and queue a job.
TIMEOUT = 60

ENTRY = ("mutation FDEntry($id: ID!, $args: Map) {"
         " runPluginOperation(plugin_id: $id, args: $args) }")

PLUGINS = "query { plugins { id enabled } }"


def check_plugin(url, api_key):
    """Make sure the plugin is there before asking it anything.

    Worth a query of its own: asking `runPluginOperation` for a plugin Stash does not
    have crashes the resolver outright - `RunPlugin` looks the plugin up and then uses
    it without checking for nil - so the server answers "Internal system error ... nil
    pointer dereference" and logs a panic. Checking first turns the single most likely
    setup mistake into a sentence saying what to do about it, and leaves the server's
    log alone.
    """
    data = graphql.try_call(url, PLUGINS, api_key=api_key, timeout=15)
    if data is None:
        return ("cannot reach Stash at " + url + " - see config.ini.example next to "
                "this script if Stash is not on localhost:9999 or needs an API key")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return None      # an older Stash without the query; let the operation try
    for plugin in plugins:
        if plugin.get("id") == PLUGIN_ID:
            if plugin.get("enabled"):
                return None
            return ("the FastDiscovery plugin is installed but disabled - enable it in "
                    "Settings -> Plugins")
    return ("the FastDiscovery plugin is not installed. This scraper is only an entry "
            "point into it; install the FastDiscovery plugin from the same source and "
            "reload plugins.")


def read_fragment():
    """The scene fragment Stash puts on stdin.

    A bare JSON object, not wrapped: `sceneInputFromScene` in pkg/scraper/script.go
    marshals the scene directly, and includes `id` when the scrape was started from a
    real scene - which is the case for both the edit panel and Identify.
    """
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        log.error("could not read the scene fragment: " + str(exc))
        return {}
    try:
        parsed = json.loads(raw or "{}")
    except ValueError as exc:
        log.error("the scene fragment was not JSON: " + str(exc))
        return {}
    return parsed if isinstance(parsed, dict) else {}


def report(answer):
    action = answer.get("action")
    message = answer.get("message")
    if action in ("ready", "running", "queued"):
        log.info(message or action)
        log.info("  FastDiscovery never writes to a scene from a scrape - nothing has "
                 "changed, and nothing will until you press Apply in its review.")
    elif answer.get("error"):
        log.error(str(answer["error"]))
    else:
        log.warning("unexpected answer from the plugin: " + json.dumps(answer)[:300])


def main() -> int:
    fragment = read_fragment()
    scene_id = str(fragment.get("id") or "").strip()

    url, api_key, origin = config.load(__file__)
    log.debug("asking FastDiscovery at " + url + " (connection from " + origin + ")")

    if not scene_id:
        # Reachable when Stash scrapes a synthetic fragment rather than a saved scene:
        # there is no scene to discover for, and guessing which one was meant would be
        # worse than saying so.
        log.warning("this scrape carried no scene id, so there is nothing to discover. "
                    "Use FastDiscovery from a saved scene's FastDiscovery tab.")
        print("null")
        return 0

    problem = check_plugin(url, api_key)
    if problem:
        log.error(problem)
        print("null")
        return 0

    data = graphql.try_call(
        url, ENTRY,
        {"id": PLUGIN_ID, "args": {"op": "scraper.entry", "scene_id": scene_id}},
        api_key=api_key, timeout=TIMEOUT)
    if data is None:
        log.error("the FastDiscovery plugin could not answer. Check Settings -> Logs "
                  "for a Python error from it.")
        print("null")
        return 0

    answer = data.get("runPluginOperation")
    if isinstance(answer, dict):
        report(answer)
    else:
        log.error("the FastDiscovery plugin returned nothing usable")

    # Always null, always deliberately: see the module docstring.
    print("null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
