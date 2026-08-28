"""Running a wave of sources with bounded concurrency and a deadline.

The shape is deliberate: worker threads do nothing but the network call, and every
database write happens on the calling thread. A sqlite3 connection belongs to the
thread that made it, and more importantly a transaction must never be open across a
scraper call - a scraper can take a minute, and holding a write lock that long would
block the UI reading the same file (requirement 30).

Failure is data, not an exception: one scraper erroring must never end the run
(requirement 29), so the worker's exception becomes a field on the outcome.
"""

from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

COMPLETE = "complete"
DEADLINE = "time budget exhausted"


class Outcome:
    __slots__ = ("item", "context", "value", "error", "timed_out", "duration_ms")

    def __init__(self, item, context, value=None, error=None, timed_out=False,
                 duration_ms=0):
        self.item = item
        self.context = context
        self.value = value
        self.error = error
        self.timed_out = timed_out
        self.duration_ms = duration_ms

    @property
    def ok(self):
        return self.error is None and not self.timed_out


def run(items, worker, on_start, on_done, concurrency=3, deadline=None, on_skip=None):
    """Run `items` through `worker`, at most `concurrency` at a time.

    * `on_start(item)` runs here, before dispatch, and returns a context handed back
      later - this is where the source row is opened;
    * `worker(item, context)` runs on a worker thread and must only do the network
      call;
    * `on_done(outcome)` runs here as each item finishes, in completion order - this is
      where results are persisted;
    * `on_skip(item, reason)` runs for anything never dispatched, so the run records it
      instead of quietly pretending the list was shorter.
    """
    items = list(items)
    concurrency = max(1, int(concurrency))
    completed = 0
    index = 0
    stop_reason = COMPLETE
    in_flight = {}

    def out_of_time():
        return deadline is not None and time.monotonic() >= deadline

    def dispatch(pool):
        nonlocal index
        item = items[index]
        index += 1
        context = on_start(item)
        started = time.monotonic()

        def task():
            try:
                return Outcome(item, context, value=worker(item, context))
            except TimeoutError as exc:
                return Outcome(item, context, error=str(exc) or "timed out",
                               timed_out=True)
            except Exception as exc:  # one source's failure, not the run's
                return Outcome(item, context, error=describe_error(exc))

        in_flight[pool.submit(task)] = started

    with ThreadPoolExecutor(max_workers=concurrency,
                            thread_name_prefix="fd-scrape") as pool:
        while index < len(items) and len(in_flight) < concurrency and not out_of_time():
            dispatch(pool)

        while in_flight:
            done, _pending = wait(list(in_flight), return_when=FIRST_COMPLETED)
            future = next(iter(done))
            started = in_flight.pop(future)
            outcome = future.result()
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            on_done(outcome)
            completed += 1

            if stop_reason == COMPLETE and out_of_time():
                stop_reason = DEADLINE
            while stop_reason == COMPLETE and index < len(items) \
                    and len(in_flight) < concurrency:
                dispatch(pool)

    skipped = len(items) - index
    if skipped and on_skip is not None:
        for item in items[index:]:
            on_skip(item, stop_reason)
    return {"completed": completed, "skipped": skipped, "stop_reason": stop_reason}


def describe_error(exc) -> str:
    """A one-line, log-safe description of an exception.

    Scraper errors arrive as server messages that can be long and can quote the request
    they failed on, so they are collapsed, truncated and stripped of anything that
    looks like a credential. Never a traceback: that would put local paths into the
    database and the log.
    """
    from . import logs
    text = str(exc).strip() or exc.__class__.__name__
    return logs.sanitise(" ".join(text.split()))[:600]


def describe_origin(exc) -> str:
    """Where an exception came from, as `module.py:line in function`, ours only.

    A bug in FastDiscovery reaches the user as one line in Stash's log, and a message
    like "unhashable type: 'list'" without a location is nearly unactionable. This adds
    the innermost frames that belong to this package - not a full traceback, which
    would put absolute server paths in the log for no extra information.
    """
    import os
    import traceback

    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        name = os.path.basename(frame.filename)
        if frame.filename.replace("\\", "/").find("/fastdiscovery/") < 0 \
                and name != "FastDiscovery.py":
            continue
        frames.append("%s:%d in %s" % (name, frame.lineno, frame.name))
    if not frames:
        return ""
    return " <- ".join(reversed(frames[-3:]))
