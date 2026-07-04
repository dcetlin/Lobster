"""
Unit tests for the pure functions in scheduled-tasks/canon-digest.py.

canon-digest is a Type B (cron-direct) script: it replaces the retired
`canon-reconciler` Type A subagent job. It does NOT mutate the artifact
registry — lobster-meta (Step 2.75/2.76) and lobster-hygiene (Step 2c) now
own registry writes. canon-digest only reads the registry + jobs.json and
formats a weekly Telegram summary, matching the Type B definition: "the same
result would be produced on every invocation regardless of model choice."

Named after behaviors, not mechanisms.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).parents[3] / "scheduled-tasks" / "canon-digest.py"
)


def _load_canon_digest():
    spec = importlib.util.spec_from_file_location("canon_digest", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


canon_digest = _load_canon_digest()
summarize_registry = canon_digest.summarize_registry
compute_job_diff = canon_digest.compute_job_diff
format_digest = canon_digest.format_digest


# ---------------------------------------------------------------------------
# summarize_registry
# ---------------------------------------------------------------------------

def _artifact(id_, state, **kw):
    base = {
        "id": id_,
        "artifact_class": kw.pop("artifact_class", "workstream"),
        "path": kw.pop("path", None),
        "owner": kw.pop("owner", "Dan"),
        "state": state,
        "last_activity": kw.pop("last_activity", "2026-01-01"),
        "convergence_target": kw.pop("convergence_target", None),
        "expiry": kw.pop("expiry", None),
        "notes": kw.pop("notes", ""),
    }
    base.update(kw)
    return base


def test_summarize_registry_counts_entries_by_state():
    artifacts = [
        _artifact("a", "seed"),
        _artifact("b", "cadence"),
        _artifact("c", "active_wip"),
        _artifact("d", "orphan"),
        _artifact("e", "orphan"),
    ]
    result = summarize_registry(artifacts)
    assert result["counts_by_state"] == {
        "seed": 1,
        "cadence": 1,
        "active_wip": 1,
        "orphan": 2,
    }


def test_summarize_registry_lists_orphans_by_id():
    artifacts = [
        _artifact("workstreams/foo", "orphan"),
        _artifact("workstreams/bar", "seed"),
        _artifact("repos/baz", "orphan"),
    ]
    result = summarize_registry(artifacts)
    assert result["orphans"] == ["repos/baz", "workstreams/foo"]


def test_summarize_registry_flags_stale_notes_from_meta_or_hygiene():
    artifacts = [
        _artifact("a", "cadence", notes="STALE-CADENCE detected 2026-07-01"),
        _artifact("b", "active_wip", notes="STALE-WIP detected 2026-07-02"),
        _artifact("c", "active_wip", notes="nothing notable"),
    ]
    result = summarize_registry(artifacts)
    assert result["stale_flagged"] == ["a", "b"]


def test_summarize_registry_flags_unowned_non_orphan_entries():
    artifacts = [
        _artifact("a", "active_wip", owner="unowned"),
        _artifact("b", "orphan", owner="unowned"),
        _artifact("c", "cadence", owner="Dan"),
    ]
    result = summarize_registry(artifacts)
    assert result["needs_owner"] == ["a"]


def test_summarize_registry_handles_empty_artifact_list():
    result = summarize_registry([])
    assert result["counts_by_state"] == {}
    assert result["orphans"] == []
    assert result["stale_flagged"] == []
    assert result["needs_owner"] == []


# ---------------------------------------------------------------------------
# compute_job_diff
#
# Registry `id` values for artifact_class=scheduled_job embed the artifact
# state as a path segment, e.g. `jobs/cadence/lobster-meta` or
# `jobs/orphan/negentropic-sweep` (the live registry uses this convention for
# all 34 scheduled_job entries — see data/artifact-registry.json). The real
# jobs.json key is always the *last* path segment, not "everything after the
# first slash" (a two-segment `jobs/<key>` id is also supported, since some
# callers may register a job without a state sub-segment).
# ---------------------------------------------------------------------------

def test_compute_job_diff_finds_live_jobs_missing_from_registry():
    registry_job_ids = {"jobs/cadence/lobster-meta", "jobs/cadence/lobster-hygiene"}
    live_job_keys = {"lobster-meta", "lobster-hygiene", "new-unregistered-job"}
    result = compute_job_diff(registry_job_ids, live_job_keys)
    assert result["unregistered"] == ["new-unregistered-job"]


def test_compute_job_diff_finds_registry_entries_with_no_live_match():
    registry_job_ids = {"jobs/cadence/lobster-meta", "jobs/orphan/deleted-job"}
    live_job_keys = {"lobster-meta"}
    result = compute_job_diff(registry_job_ids, live_job_keys)
    assert result["stale_registry_entries"] == ["deleted-job"]


def test_compute_job_diff_ignores_non_job_registry_ids():
    registry_job_ids = {"workstreams/foo", "jobs/cadence/lobster-meta"}
    live_job_keys = {"lobster-meta"}
    result = compute_job_diff(registry_job_ids, live_job_keys)
    assert result["unregistered"] == []
    assert result["stale_registry_entries"] == []


def test_compute_job_diff_empty_when_fully_reconciled():
    registry_job_ids = {"jobs/cadence/a", "jobs/orphan/b"}
    live_job_keys = {"a", "b"}
    result = compute_job_diff(registry_job_ids, live_job_keys)
    assert result["unregistered"] == []
    assert result["stale_registry_entries"] == []


def test_compute_job_diff_also_supports_flat_two_segment_ids():
    """A bare `jobs/<key>` id (no state sub-segment) must resolve the same
    way as the three-segment convention — the last path segment is always
    the job key."""
    registry_job_ids = {"jobs/lobster-meta"}
    live_job_keys = {"lobster-meta"}
    result = compute_job_diff(registry_job_ids, live_job_keys)
    assert result["unregistered"] == []
    assert result["stale_registry_entries"] == []


# ---------------------------------------------------------------------------
# format_digest
# ---------------------------------------------------------------------------

def test_format_digest_reports_zero_violations_when_registry_clean():
    summary = {
        "counts_by_state": {"seed": 5, "cadence": 3},
        "orphans": [],
        "stale_flagged": [],
        "needs_owner": [],
    }
    job_diff = {"unregistered": [], "stale_registry_entries": []}
    text = format_digest("2026-07-05", summary, job_diff, total=8)
    assert "2026-07-05" in text
    assert "No action needed" in text
    assert "8" in text


def test_format_digest_lists_violations_when_present():
    summary = {
        "counts_by_state": {"seed": 5, "orphan": 2},
        "orphans": ["workstreams/foo", "repos/bar"],
        "stale_flagged": ["jobs/lobster-hygiene"],
        "needs_owner": ["workstreams/baz"],
    }
    job_diff = {"unregistered": ["new-job"], "stale_registry_entries": ["dead-job"]}
    text = format_digest("2026-07-05", summary, job_diff, total=7)
    assert "workstreams/foo" in text
    assert "repos/bar" in text
    assert "jobs/lobster-hygiene" in text
    assert "workstreams/baz" in text
    assert "new-job" in text
    assert "dead-job" in text
    assert "No action needed" not in text
