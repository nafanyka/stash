#!/usr/bin/env python3
"""ScrapeDiscovery, as a scene scraper: a thin entry point, and nothing else.

This file deliberately contains no discovery logic. There is one engine, in the
ScrapeDiscovery plugin, and this asks it a single question over GraphQL:

    runPluginOperation(plugin_id: "ScrapeDiscovery", args: {op: "scraper.entry", ...})

The plugin decides what should happen and answers with one of:

    returned       independent sources already agree - here is the scene
    no_consensus   answers exist, none corroborated well enough to offer
    queued         nothing stored yet, so a discovery scan has been started
    running        a scan for this scene is already going

Why a shim and not the engine (see docs/architecture.md section 2): a scraper runs
inside the GraphQL request, and discovery is minutes of work across hundreds of
scrapers. Worse, a fragment scraper's whole contract is to hand Stash values to save,
and Identify saves them unreviewed - the opposite of what ScrapeDiscovery is for. So
this never scrapes anything. It reports what is already known, or asks for a scan.

Requires the ScrapeDiscovery **plugin** to be installed and enabled. Scrapers - unlike
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

PLUGIN_ID = "ScrapeDiscovery"

# A plugin operation spawns a Python process on the server, and this one may also read
# the scene back, so allow more than the default. It never scrapes, so it stays quick.
TIMEOUT = 60

ENTRY = (
    "mutation SDEntry($id: ID!, $args: Map) {"
    " runPluginOperation(plugin_id: $id, args: $args) }"
)

PLUGINS = "query { plugins { id enabled } }"


def check_plugin(url, api_key):
    """Make sure the plugin is there before asking it anything.

    Worth a query of its own: asking `runPluginOperation` for a plugin Stash does not
    have crashes the resolver outright - `RunPlugin` looks the plugin up and then uses
    it without checking for nil, unlike `CreateTask` next to it - so the server answers
    "Internal system error ... nil pointer dereference" and logs a panic. Checking
    first turns the single most likely setup mistake into a sentence that says what to
    do about it, and leaves the server's log alone.

    Returns None when everything is fine, or a message explaining what is not.
    """
    data = graphql.try_call(url, PLUGINS, api_key=api_key, timeout=15)
    if data is None:
        return ("cannot reach Stash at " + url + " - see config.ini.example next to "
                "this script if Stash is not on localhost:9999 or needs an API key")
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return None  # an old Stash without the query; let the operation try anyway
    for plugin in plugins:
        if plugin.get("id") == PLUGIN_ID:
            if plugin.get("enabled"):
                return None
            return ("the ScrapeDiscovery plugin is installed but disabled - enable it "
                    "in Settings -> Plugins")
    return ("the ScrapeDiscovery plugin is not installed. This scraper is only an entry "
            "point into it; install the ScrapeDiscovery plugin from the same source and "
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


def ask(url, api_key, scene_id):
    """Put the question to the plugin. Returns its answer, or None."""
    data = graphql.try_call(
        url, ENTRY,
        {"id": PLUGIN_ID, "args": {"op": "scraper.entry", "scene_id": str(scene_id)}},
        api_key=api_key, timeout=TIMEOUT,
    )
    if data is None:
        log.error("the ScrapeDiscovery plugin could not answer. Check Settings -> Logs "
                  "for a Python error from it.")
        return None
    answer = data.get("runPluginOperation")
    if not isinstance(answer, dict):
        log.error("the ScrapeDiscovery plugin returned nothing usable")
        return None
    return answer


def report(answer):
    """Log what the plugin decided, in enough detail to be trusted or questioned."""
    action = answer.get("action")
    message = answer.get("message")

    if action == "returned":
        agreed = answer.get("consensus") or {}
        log.info("returning what %d independent source(s) agree on: %s"
                 % (agreed.get("independent_sources") or 0, agreed.get("reason") or "?"))
        log.info("  sources: " + ", ".join(agreed.get("sources") or []))
        considered = agreed.get("considered") or 0
        discarded = agreed.get("discarded_groups") or 0
        without = agreed.get("without_evidence") or 0
        log.info("  from %d stored answer(s); %d other grouping(s) rejected, %d "
                 "contributed no evidence" % (considered, discarded, without))
        log.info("  nothing has been written to the scene - Stash's merge dialog is "
                 "still yours to confirm or discard")
    elif action == "no_consensus":
        log.info(message or "no consensus")
        log.info("  open ScrapeDiscovery to see every answer and decide for yourself")
    elif action == "queued":
        log.info(message or "discovery queued")
    elif action == "running":
        log.info(message or "a scan is already running")
    elif answer.get("error"):
        log.error(str(answer["error"]))
    else:
        log.warning("unexpected answer from the plugin: " + json.dumps(answer)[:300])


def main() -> int:
    fragment = read_fragment()
    scene_id = str(fragment.get("id") or "").strip()

    url, api_key, origin = config.load(__file__)
    log.debug("asking ScrapeDiscovery at " + url + " (connection from " + origin + ")")

    if not scene_id:
        # Reachable when Stash scrapes a synthetic fragment rather than a saved scene:
        # there is no scene to look anything up for, and guessing which one was meant
        # would be worse than saying so.
        log.warning("this scrape carried no scene id, so there is nothing to look up. "
                    "Use ScrapeDiscovery from a saved scene's Discovery tab.")
        print("null")
        return 0

    problem = check_plugin(url, api_key)
    if problem:
        log.error(problem)
        print("null")
        return 0

    answer = ask(url, api_key, scene_id)
    if answer is None:
        print("null")
        return 0

    report(answer)
    scene = answer.get("scene")
    # `null` is a valid, meaningful answer: it tells Stash this source has nothing,
    # which is exactly right when nothing is corroborated yet.
    print(json.dumps(scene, ensure_ascii=False) if scene else "null")
    return 0


if __name__ == "__main__":
    sys.exit(main())
