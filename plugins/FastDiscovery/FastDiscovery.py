#!/usr/bin/env python3
"""FastDiscovery - the plugin entry point.

Stash spawns this process for three different reasons and tells them apart only by what
is on stdin:

    {"args": {"task": "<task name>"}}   a task from the job queue (runPluginTask)
    {"args": {"op": "<operation>"}}     a request from the UI (runPluginOperation)
    {"args": {"mode": "diagnose"}}      the manifest's diagnostics task

Exactly one JSON object goes to stdout, because that is how Stash reads the result:
`{"output": ...}` on success, `{"error": "..."}` on failure. Anything else printed there
corrupts the response, so every human-facing line goes to stderr through logs.py.

The work itself lives in the `fastdiscovery` package; this file only works out what was
asked, opens the database, and hands over.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastdiscovery import logs, ops, settings, stash, tasks  # noqa: E402
from fastdiscovery.db import repo as repository  # noqa: E402

logs.use_utf8()


def build_context(payload):
    """(context, note) - the client, database and configuration for this invocation."""
    url, api_key, cookie, config_dir = stash.from_plugin_input(payload)
    note = None
    if not (payload or {}).get("server_connection"):
        # Run by hand rather than by Stash: fall back to the environment, so the
        # package stays usable outside a Stash process.
        import os
        url = os.environ.get("STASH_URL") or url
        if url and not url.endswith("/graphql"):
            url = url.rstrip("/") + "/graphql"
        api_key = os.environ.get("STASH_API_KEY")
        cookie = None
        note = "no server_connection on stdin; using " + url

    client = stash.Client(url, api_key, cookie)
    config = settings.parse(client.plugin_settings(settings.PLUGIN_ID))
    logs.set_debug(config["debugLogging"])
    for problem in config.problems:
        logs.warning("settings: " + problem)

    path = config["databasePath"] or repository.default_path(
        config_dir or client.config_dir())
    repo = repository.Repo.open(path)
    return ops.Context(client, repo, config), note


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({"error": "could not read stdin: %s" % exc}))
        return 0

    try:
        payload = json.loads(raw or "{}")
    except ValueError as exc:
        print(json.dumps({"error": "plugin input was not JSON: %s" % exc}))
        return 0

    args = (payload.get("args") or {}) if isinstance(payload, dict) else {}
    context = None
    try:
        context, note = build_context(payload)
        if note:
            logs.debug(note)

        if args.get("op"):
            result = ops.dispatch(context, str(args["op"]), args)
        elif args.get("task"):
            result = tasks.run(context, str(args["task"]), args)
        elif str(args.get("mode") or "") == "diagnose":
            result = tasks.run(context, tasks.SHOW_SETTINGS, args)
        else:
            result = {
                "ok": False,
                "error": "nothing to do: pass a task (from the job queue) or an op "
                         "(from the UI)",
                "tasks": sorted(tasks.TASKS),
                "operations": sorted(ops.HANDLERS),
            }
        print(json.dumps({"output": result}, ensure_ascii=False, default=str))
    except Exception as exc:
        from fastdiscovery.executor import describe_error
        message = describe_error(exc)
        logs.error("FastDiscovery failed: " + message)
        # A plugin-level error, which the caller sees as a GraphQL error. Only for a
        # failure that makes the plugin unusable - an operation that merely could not
        # do what was asked reports that inside its own output.
        print(json.dumps({"error": message}))
    finally:
        if context is not None:
            context.repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
