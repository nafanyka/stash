"""Running a work list with bounded concurrency, a deadline, and early stops.

The shape here is deliberate: worker threads do nothing but the network call, and
every database write happens on the calling thread. A sqlite3 connection belongs to
the thread that made it, and more importantly a transaction must never be open across
a scraper call - a scraper can take a minute, and holding a write lock for that long
would block the UI reading the same file (docs/architecture.md section 50).

Concurrency is a sliding window rather than "submit everything": the stop condition is
re-checked after every completion, so a scan that has already found what it needs stops
without having queued three hundred more scrapes. Cancellation is cooperative through
`should_stop`; Stash's own cancel is a process kill, which needs no cooperation, and
the write discipline above is what keeps the database consistent when it happens.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

# Why a run ended. `COMPLETE` means the work list was exhausted.
COMPLETE = "complete"
DEADLINE = "time budget exhausted"
LIMIT = "attempt limit reached"


class Outcome:
    """What one work item produced. Failure is data, not an exception."""

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


def run(items, worker, on_start, on_done, concurrency=3, deadline=None,
        should_stop=None, on_skip=None):
    """Run `items` through `worker`, at most `concurrency` at a time.

    * `on_start(item)` runs on this thread before an item is dispatched, and returns a
      context handed back later - this is where the attempt row is opened.
    * `worker(item, context)` runs on a worker thread and must only do the network
      call. Anything it raises becomes `Outcome.error`, so one broken scraper cannot
      end the run.
    * `on_done(outcome)` runs on this thread as each item finishes, in completion
      order - this is where results are persisted.
    * `should_stop()` runs on this thread after every completion; a truthy return is
      recorded as the stop reason and no further item is dispatched.
    * `on_skip(item, reason)` runs for every item that was never dispatched, so a scan
      can record them as SKIPPED rather than silently pretending the list was shorter.

    Returns {"completed": n, "skipped": n, "stop_reason": str}.
    """
    items = list(items)
    concurrency = max(1, int(concurrency))
    completed = 0
    stop_reason = COMPLETE
    index = 0
    in_flight = {}

    def dispatch(pool):
        nonlocal index
        item = items[index]
        index += 1
        context = on_start(item)
        started = time.monotonic()

        def task():
            try:
                return Outcome(item, context, value=worker(item, context))
            except TimeoutError as exc:  # noqa: F841 - message kept below
                return Outcome(item, context, error=str(exc) or "timed out", timed_out=True)
            except Exception as exc:  # one scraper's failure, not the scan's
                return Outcome(item, context, error=describe_error(exc))

        future = pool.submit(task)
        in_flight[future] = started
        return future

    def out_of_time():
        return deadline is not None and time.monotonic() >= deadline

    with ThreadPoolExecutor(max_workers=concurrency,
                            thread_name_prefix="sd-scrape") as pool:
        while index < len(items) and len(in_flight) < concurrency and not out_of_time():
            dispatch(pool)

        while in_flight:
            # Wait for whichever finishes first, then immediately top the window up.
            done, _ = _wait_first(in_flight)
            started = in_flight.pop(done)
            outcome = done.result()
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            on_done(outcome)
            completed += 1

            if stop_reason == COMPLETE:
                if out_of_time():
                    stop_reason = DEADLINE
                elif should_stop is not None:
                    reason = should_stop()
                    if reason:
                        stop_reason = reason if isinstance(reason, str) else "stop condition met"

            while stop_reason == COMPLETE and index < len(items) \
                    and len(in_flight) < concurrency:
                dispatch(pool)

    skipped = len(items) - index
    if skipped and on_skip is not None:
        for item in items[index:]:
            on_skip(item, stop_reason)

    return {"completed": completed, "skipped": skipped, "stop_reason": stop_reason}


def _wait_first(in_flight):
    """The first future to finish, without busy-waiting.

    `concurrent.futures.wait` with FIRST_COMPLETED does exactly this; it lives in a
    helper only so the loop above reads as one thing per line.
    """
    from concurrent.futures import FIRST_COMPLETED, wait
    done, pending = wait(list(in_flight), return_when=FIRST_COMPLETED)
    return next(iter(done)), pending


def describe_error(exc) -> str:
    """A one-line, log-safe description of an exception.

    Scraper errors arrive as server messages that can be long and can contain the URL
    that was scraped; they are truncated, and never formatted with a traceback, which
    would put local paths into the database.
    """
    text = str(exc).strip() or exc.__class__.__name__
    text = " ".join(text.split())
    return text[:600]
