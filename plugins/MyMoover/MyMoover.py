#!/usr/bin/env python3
"""MyMoover - the plugin entry point.

Same protocol as this repo's other plugins (see FastDiscovery.py): Stash puts a JSON
payload on stdin, `{"args": {"op": "<name>", ...}}` for a call from the UI
(`runPluginOperation`), and exactly one JSON object goes to stdout - `{"output": ...}`
on success, `{"error": "..."}` for a failure that makes the whole call unusable. Every
human-facing line goes to stderr through stash_common.log, since stdout is the result
channel.

There is no `tasks:` branch: every operation here is meant to run synchronously from a
modal, and Stash's job queue (`runPluginTask`) is for background work with a progress
bar, which this plugin deliberately does not use (see requirement 20 in the design
notes / README) - a bulk move of a normal selection is fast enough to run inline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stash_common import config as stash_config  # noqa: E402
from stash_common import log  # noqa: E402

from mymoover import ops, settings, stash  # noqa: E402


def build_context(payload):
    url, api_key, cookie = stash_config.from_plugin_input(payload)
    client = stash.Client(url, api_key, cookie)
    raw_settings = client.plugin_settings(settings.PLUGIN_ID)
    effective = settings.parse(raw_settings)
    return ops.Context(client, effective)


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

    try:
        context = build_context(payload)
        if args.get("op"):
            result = ops.dispatch(context, str(args["op"]), args)
        else:
            result = {
                "ok": False,
                "error": "nothing to do: MyMoover only answers runPluginOperation "
                         "calls that carry an 'op'",
                "operations": sorted(ops.HANDLERS),
            }
        print(json.dumps({"output": result}, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001 - one boundary, must never crash silently
        message = "MyMoover failed: %s: %s" % (type(exc).__name__, exc)
        log.error(message)
        print(json.dumps({"error": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
