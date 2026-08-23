"""Minimal GraphQL client for the local Stash server.

Standard library only on purpose: the Python interpreter Stash shells out to is
often a bare system install with no `requests`, and a scraper that dies on an
ImportError is indistinguishable from a broken scraper in the UI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import log

TIMEOUT = 15


class GraphQLError(RuntimeError):
    """Transport failure, HTTP error, or a non-empty `errors` array."""


def call(url: str, query: str, variables: dict | None = None, api_key: str | None = None) -> dict:
    """Run `query` and return the `data` object, or raise GraphQLError."""
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["ApiKey"] = api_key

    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except OSError:
            pass
        hint = " (API key missing or wrong?)" if exc.code in (401, 403) else ""
        raise GraphQLError(f"HTTP {exc.code} from {url}{hint} {detail}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GraphQLError(f"cannot reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GraphQLError(f"{url} returned a non-JSON body") from exc

    if body.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in body["errors"])
        raise GraphQLError(messages)
    data = body.get("data")
    if data is None:
        raise GraphQLError("response carried no data")
    return data


def try_call(url: str, query: str, variables: dict | None = None, api_key: str | None = None):
    """Same as `call` but returns None and logs instead of raising.

    Handy for probing a query shape that may not exist on the running Stash
    version, where a failure is expected rather than exceptional.
    """
    try:
        return call(url, query, variables, api_key)
    except GraphQLError as exc:
        log.debug(f"graphql probe failed: {exc}")
        return None
