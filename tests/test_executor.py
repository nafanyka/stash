"""Bounded concurrency, deadlines and early stopping.

The executor is what keeps a scan from launching hundreds of scrapers at once, and its
division of labour is load-bearing: worker threads do only the network call, and every
callback that touches the database runs on the calling thread.
"""

from __future__ import annotations

import threading
import time

from scrapediscovery import executor


def collect(items, worker, **kwargs):
    """Run and return (summary, outcomes, skipped)."""
    outcomes, skipped = [], []
    summary = executor.run(
        items, worker,
        on_start=lambda item: {"item": item},
        on_done=outcomes.append,
        on_skip=lambda item, reason: skipped.append((item, reason)),
        **kwargs)
    return summary, outcomes, skipped


class TestBasics:
    def test_every_item_runs_and_the_context_comes_back(self):
        summary, outcomes, skipped = collect([1, 2, 3], lambda item, ctx: item * 10)
        assert summary == {"completed": 3, "skipped": 0, "stop_reason": executor.COMPLETE}
        assert sorted(one.value for one in outcomes) == [10, 20, 30]
        assert all(one.context["item"] == one.item for one in outcomes)
        assert skipped == []

    def test_an_empty_work_list_is_fine(self):
        summary, outcomes, _skipped = collect([], lambda item, ctx: item)
        assert summary["completed"] == 0
        assert outcomes == []

    def test_duration_is_measured_per_item(self):
        def slow(item, ctx):
            time.sleep(0.05)
            return item

        _summary, outcomes, _skipped = collect([1], slow)
        assert outcomes[0].duration_ms >= 40


class TestFailureIsolation:
    def test_a_raising_item_becomes_an_outcome_not_an_exception(self):
        def worker(item, ctx):
            if item == 2:
                raise RuntimeError("boom")
            return item

        summary, outcomes, _skipped = collect([1, 2, 3], worker)
        # One scraper's failure must never end a scan.
        assert summary["completed"] == 3
        failed = [one for one in outcomes if not one.ok]
        assert len(failed) == 1
        assert failed[0].error == "boom"
        assert failed[0].timed_out is False

    def test_a_timeout_is_distinguishable_from_an_error(self):
        # The two are cached very differently, so they must not be conflated.
        def worker(item, ctx):
            raise TimeoutError("timed out after 30s")

        _summary, outcomes, _skipped = collect([1], worker)
        assert outcomes[0].timed_out is True
        assert outcomes[0].ok is False

    def test_error_text_is_flattened_and_bounded(self):
        message = executor.describe_error(RuntimeError("a\nb   c" + "x" * 2000))
        assert "\n" not in message
        assert len(message) <= 600


class TestConcurrency:
    def test_no_more_than_the_limit_run_at_once(self):
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def worker(item, ctx):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.02)
            with lock:
                state["now"] -= 1
            return item

        summary, _outcomes, _skipped = collect(list(range(12)), worker, concurrency=3)
        assert summary["completed"] == 12
        assert state["peak"] <= 3

    def test_callbacks_all_run_on_the_calling_thread(self):
        # This is what makes it safe for on_done to write to sqlite: a connection
        # belongs to the thread that opened it.
        main = threading.current_thread().ident
        seen = {"start": set(), "done": set(), "worker": set()}

        def worker(item, ctx):
            seen["worker"].add(threading.current_thread().ident)
            return item

        executor.run(
            list(range(6)), worker,
            on_start=lambda item: seen["start"].add(threading.current_thread().ident),
            on_done=lambda outcome: seen["done"].add(threading.current_thread().ident),
            concurrency=3)

        assert seen["start"] == {main}
        assert seen["done"] == {main}
        assert seen["worker"] - {main}  # the work itself did move off it


class TestStopping:
    def test_a_stop_condition_leaves_the_rest_undispatched(self):
        ran = []

        def worker(item, ctx):
            ran.append(item)
            return item

        summary, _outcomes, skipped = collect(
            list(range(50)), worker, concurrency=1,
            should_stop=lambda: "found what we came for" if len(ran) >= 3 else None)

        assert summary["stop_reason"] == "found what we came for"
        assert len(ran) < 50
        # Nothing vanishes silently: what was not tried is reported with the reason.
        assert len(skipped) == 50 - len(ran)
        assert skipped[0][1] == "found what we came for"

    def test_a_deadline_stops_dispatching(self):
        def worker(item, ctx):
            time.sleep(0.03)
            return item

        summary, _outcomes, skipped = collect(
            list(range(40)), worker, concurrency=2,
            deadline=time.monotonic() + 0.1)

        assert summary["stop_reason"] == executor.DEADLINE
        assert skipped
        assert summary["completed"] + summary["skipped"] == 40

    def test_an_already_passed_deadline_dispatches_nothing(self):
        ran = []
        summary, _outcomes, skipped = collect(
            [1, 2, 3], lambda item, ctx: ran.append(item),
            deadline=time.monotonic() - 1)
        assert ran == []
        assert summary["completed"] == 0
        assert len(skipped) == 3

    def test_no_stop_condition_means_everything_runs(self):
        summary, _outcomes, skipped = collect(list(range(20)),
                                              lambda item, ctx: item,
                                              should_stop=lambda: None)
        assert summary["completed"] == 20
        assert skipped == []
