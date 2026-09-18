"""Applying a performer review: Apply, Cancel, and Organize.

Mirrors `test_fd_apply.py`'s style for the scene engine's `apply.py`, against the
performer siblings `merge.build_performer` / `apply.preview_performer` /
`apply.commit_performer`.
"""

from __future__ import annotations

import json

import pytest
from fd_common import FakeStash, scraped_performer

from fastdiscovery import apply, merge, performer_discovery, settings as settings_module
from fastdiscovery.db import repo as R

PERFORMER = {"id": "42", "name": "Bonnie Alex", "urls": [], "alias_list": [],
             "tags": [], "stash_ids": [], "updated_at": "2026-01-01T00:00:00Z"}


def perf_config(fast_scrapers=(), **overrides):
    values = {"performerFastScrapers": json.dumps(list(fast_scrapers))}
    values.update(overrides)
    return settings_module.parse(values)


def start_run(client, repo, config=None):
    config = config or perf_config(["Babepedia"])
    runner = performer_discovery.PerformerRunner(client, repo, config)
    return runner.run_fast(42)


class TestDefaults:
    def test_current_scalar_is_selected_by_default(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alexis"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        name_row = [r for r in review["rows"] if r["field"] == "name"][0]
        current_option = [v for v in name_row["values"] if v["is_current"]][0]
        assert name_row["default"] == current_option["id"]


class TestApplyAllowsAnUnsetField:
    def test_apply_is_allowed_even_when_a_field_with_candidates_is_untouched(
            self, fd_repo):
        # requirement: red-warning fields never block Apply. Two disagreeing scraper
        # answers for `country`, CURRENT has none, and the selection simply never
        # mentions the field - Apply must still succeed (here, via Organize alone).
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex",
                                                             country="USA"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        performer = dict(PERFORMER)

        result = apply.commit_performer(fd_repo, client, run, performer,
                                         selection={}, organize=True)
        assert result["applied"] is True
        # `country` was never selected, so it is not written - and Organize alone,
        # with no field changes, must not send an empty performerUpdate either.
        assert client.performer_updates == []
        assert client.organized_calls == [("42", True)]


class TestCancel:
    def test_cancel_discards_the_whole_payload(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alex"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        assert fd_repo.results_of(run["id"])   # something is there before Cancel

        apply.reject(fd_repo, run)

        after = fd_repo.run(run["id"])
        assert after["status"] == R.REJECTED
        assert after["purged"] is True
        assert fd_repo.results_of(run["id"]) == []
        assert fd_repo.sources_of(run["id"]) == []
        assert fd_repo.urls_of(run["id"]) == []
        assert client.performer_updates == []   # nothing written to the performer


class TestApplyFailureKeepsThePayload:
    def test_a_failed_apply_leaves_the_run_reviewable(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alexis"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        performer = dict(PERFORMER)

        def boom(values):
            raise RuntimeError("stash is down")
        client.performer_update = boom

        review = merge.build_performer(fd_repo, run, performer)
        name_row = [r for r in review["rows"] if r["field"] == "name"][0]
        new_value = [v for v in name_row["values"] if not v["is_current"]][0]

        with pytest.raises(apply.ApplyError):
            apply.commit_performer(fd_repo, client, run, performer,
                                   selection={"name": new_value["id"]})

        after = fd_repo.run(run["id"])
        assert after["status"] == R.FAILED_APPLY
        assert after["purged"] is False
        assert fd_repo.results_of(run["id"])   # the payload survives, retryable


class TestOrganize:
    def test_organize_off_leaves_the_flag_untouched(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alexis"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        name_row = [r for r in review["rows"] if r["field"] == "name"][0]
        new_value = [v for v in name_row["values"] if not v["is_current"]][0]

        result = apply.commit_performer(fd_repo, client, run, dict(PERFORMER),
                                        selection={"name": new_value["id"]},
                                        organize=False)
        assert result["applied"] is True
        assert client.organized_calls == []

    def test_organize_on_uses_the_performerorganized_mechanism(self, fd_repo):
        client = FakeStash(performer=PERFORMER, responses={
            "pname:Babepedia:Bonnie Alex": scraped_performer(name="Bonnie Alexis"),
        })
        summary = start_run(client, fd_repo)
        run = fd_repo.run(summary["run_id"])
        review = merge.build_performer(fd_repo, run, PERFORMER)
        name_row = [r for r in review["rows"] if r["field"] == "name"][0]
        new_value = [v for v in name_row["values"] if not v["is_current"]][0]

        result = apply.commit_performer(fd_repo, client, run, dict(PERFORMER),
                                        selection={"name": new_value["id"]},
                                        organize=True)
        assert result["applied"] is True
        assert result["organized"] is True
        assert client.organized_calls == [("42", True)]
