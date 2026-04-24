"""
Tests for shard-stream parallel dispatch logic (shard_dispatch.py).

Design:
  The shard-stream gate decides whether a candidate UoW can be dispatched
  given a set of in-flight UoWs. Tests are named after the behavior they
  verify, not the mechanism.

Named constants used (from shard_dispatch.py):
  DEFAULT_MAX_PARALLEL = 2

Behaviors verified (issue #912 semantics — null file_scope = independent):
- No in-flight UoWs: any candidate is allowed (regardless of scope)
- max_parallel cap: blocked when in-flight count >= max_parallel
- Null file_scope candidate: ALLOWED when in-flight UoWs have annotated scopes
  (null = independent, not exclusive — issue #912 changed this)
- Null file_scope candidate: ALLOWED alongside null-scope in-flight UoWs
  (two unannotated UoWs do not block each other)
- Shard serialization: same shard_id blocks candidate
- Different shards: allowed when shard_ids differ
- File scope overlap (exact): blocked when BOTH sides have explicit scopes
- File scope overlap (prefix): blocked (directory covers file)
- No overlap: allowed
- Null in-flight scope does NOT block annotated-scope candidates (gate skips null in-flight)
- max_parallel=1 enforces strict serialization
- Prescribed UoWs accumulated within a cycle block subsequent candidates correctly

All threshold values are referenced from named constants, not magic literals.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.shard_dispatch import (
    check_shard_dispatch_eligibility,
    _scopes_overlap,
    DispatchAllowed,
    DispatchBlocked,
    DEFAULT_MAX_PARALLEL,
)


# ---------------------------------------------------------------------------
# Minimal stub for UoW objects used as executing_uows
# ---------------------------------------------------------------------------

@dataclass
class _StubUoW:
    """Minimal duck-typed UoW stub for shard gate tests."""
    id: str
    file_scope: list[str] | None = None
    shard_id: str | None = None


def _stub(
    uow_id: str,
    file_scope: list[str] | None = None,
    shard_id: str | None = None,
) -> _StubUoW:
    return _StubUoW(id=uow_id, file_scope=file_scope, shard_id=shard_id)


# ---------------------------------------------------------------------------
# Tests: _scopes_overlap (pure helper)
# ---------------------------------------------------------------------------

class TestScopesOverlap:
    def test_identical_paths_overlap(self):
        assert _scopes_overlap(
            ["src/orchestration/steward.py"],
            ["src/orchestration/steward.py"],
        )

    def test_directory_covers_file_overlap(self):
        """A directory scope covers any file under it."""
        assert _scopes_overlap(
            ["src/orchestration"],
            ["src/orchestration/steward.py"],
        )

    def test_file_under_directory_overlap(self):
        """Reverse of directory-covers-file."""
        assert _scopes_overlap(
            ["src/orchestration/steward.py"],
            ["src/orchestration"],
        )

    def test_trailing_slash_normalized(self):
        """Trailing slashes are stripped before comparison."""
        assert _scopes_overlap(
            ["src/orchestration/"],
            ["src/orchestration/steward.py"],
        )

    def test_no_overlap_different_directories(self):
        assert not _scopes_overlap(
            ["src/orchestration"],
            ["tests/unit"],
        )

    def test_no_overlap_sibling_files(self):
        assert not _scopes_overlap(
            ["src/orchestration/steward.py"],
            ["src/orchestration/registry.py"],
        )

    def test_partial_name_prefix_no_overlap(self):
        """'src/orches' does NOT overlap 'src/orchestration/' — must match a full path segment."""
        # Our prefix check uses path_b.startswith(path_a + "/") so "src/orches" + "/"
        # = "src/orches/" which is not a prefix of "src/orchestration/steward.py".
        assert not _scopes_overlap(
            ["src/orches"],
            ["src/orchestration/steward.py"],
        )

    def test_multiple_paths_one_overlaps(self):
        """Returns True if any pair overlaps — directory in scope_a covers file in scope_b."""
        assert _scopes_overlap(
            ["src/orchestration", "tests/unit"],
            ["docs/design.md", "src/orchestration/registry.py"],
        )

    def test_multiple_paths_none_overlaps(self):
        assert not _scopes_overlap(
            ["src/orchestration"],
            ["tests/unit", "docs"],
        )


# ---------------------------------------------------------------------------
# Tests: check_shard_dispatch_eligibility — gate 1 (max_parallel cap)
# ---------------------------------------------------------------------------

class TestMaxParallelCap:
    def test_no_in_flight_allows_dispatch(self):
        """Empty executing list — any candidate is allowed."""
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=[],
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_at_max_parallel_blocks_dispatch(self):
        """When executing count equals max_parallel, candidate is blocked."""
        executing = [
            _stub("uow_a", file_scope=["src/foo.py"]),
            _stub("uow_b", file_scope=["src/bar.py"]),
        ]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/baz.py"],  # no overlap — gate 1 fires first
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,  # == 2
        )
        assert isinstance(decision, DispatchBlocked)
        assert "max_parallel=2" in decision.reason

    def test_below_max_parallel_proceeds_to_next_gate(self):
        """One in-flight with max_parallel=2 — gate 1 does not fire."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"])]
        # Use non-overlapping scope so gate 4 also passes
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_max_parallel_one_enforces_strict_serialization(self):
        """max_parallel=1 blocks any candidate when one UoW is already executing."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"])]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=1,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "max_parallel=1" in decision.reason

    def test_max_parallel_zero_always_blocks(self):
        """max_parallel=0 blocks even with no in-flight UoWs (degenerate config)."""
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/foo.py"],
            candidate_shard_id=None,
            executing_uows=[],
            max_parallel=0,
        )
        assert isinstance(decision, DispatchBlocked)


# ---------------------------------------------------------------------------
# Tests: null file_scope = independent (issue #912 semantics)
# ---------------------------------------------------------------------------

class TestNullScopeIndependent:
    """
    Issue #912 changed null file_scope from "exclusive" to "independent".

    Old behavior: a null-scope candidate was blocked by any in-flight UoW;
    a null-scope in-flight UoW blocked all candidates.

    New behavior: null file_scope means "no explicit annotation" — the UoW
    proceeds unless the max_parallel cap or a shard constraint fires. Null-
    scope UoWs do not conflict with each other or with annotated UoWs.
    """

    def test_null_scope_candidate_allowed_alongside_annotated_in_flight(self):
        """Null-scope candidate is allowed when in-flight UoW has an annotated scope."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"])]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=None,
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_null_scope_candidate_allowed_when_nothing_executing(self):
        """Null-scope candidate is allowed when executing list is empty."""
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=None,
            candidate_shard_id=None,
            executing_uows=[],
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_null_scope_in_flight_does_not_block_annotated_candidate(self):
        """Null-scope in-flight UoW is independent — does not block annotated candidates."""
        executing = [_stub("uow_a", file_scope=None)]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_two_null_scope_uows_do_not_block_each_other(self):
        """Null-scope candidate + null-scope in-flight — both independent, candidate allowed."""
        executing = [_stub("uow_a", file_scope=None)]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=None,
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_null_scope_still_blocked_by_max_parallel(self):
        """Null-scope candidate is still blocked when max_parallel cap is reached."""
        executing = [
            _stub("uow_a", file_scope=None),
            _stub("uow_b", file_scope=None),
        ]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=None,
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,  # == 2, both slots filled
        )
        assert isinstance(decision, DispatchBlocked)
        assert "max_parallel" in decision.reason

    def test_null_scope_still_blocked_by_shard_constraint(self):
        """Null-scope candidate is blocked when its shard matches an in-flight UoW's shard."""
        executing = [_stub("uow_a", file_scope=None, shard_id="wos-core")]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=None,
            candidate_shard_id="wos-core",
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "wos-core" in decision.reason


# ---------------------------------------------------------------------------
# Tests: gate 3 — shard serialization
# ---------------------------------------------------------------------------

class TestShardSerialization:
    def test_same_shard_blocks_candidate(self):
        """Candidate with same shard_id as in-flight UoW is blocked."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"], shard_id="wos-core")]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id="wos-core",
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "wos-core" in decision.reason

    def test_different_shards_allowed(self):
        """Candidate with different shard_id than all in-flight UoWs is allowed."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"], shard_id="wos-core")]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id="tests",
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_null_shard_id_candidate_not_blocked_by_shard_check(self):
        """Candidate with null shard_id is not blocked by gate 3 (no shard constraint)."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"], shard_id="wos-core")]
        # Gate 3 is skipped for null candidate shard — gate 4 (file scope) decides
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_null_in_flight_shard_not_matched(self):
        """In-flight UoW with null shard_id does not match a non-null candidate shard."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"], shard_id=None)]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id="tests",
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)


# ---------------------------------------------------------------------------
# Tests: gate 4 — file scope overlap
# ---------------------------------------------------------------------------

class TestFileScopeOverlap:
    def test_exact_file_overlap_blocks(self):
        executing = [_stub("uow_a", file_scope=["src/orchestration/steward.py"])]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "overlap" in decision.reason

    def test_directory_prefix_overlap_blocks(self):
        """Candidate file under in-flight directory is blocked."""
        executing = [_stub("uow_a", file_scope=["src/orchestration"])]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchBlocked)

    def test_no_overlap_allows_dispatch(self):
        executing = [_stub("uow_a", file_scope=["src/orchestration"])]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["tests/unit"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchAllowed)

    def test_multiple_in_flight_one_overlap_blocks(self):
        """Any in-flight overlap is sufficient to block the candidate."""
        executing = [
            _stub("uow_a", file_scope=["tests/unit"]),
            _stub("uow_b", file_scope=["src/orchestration"]),
        ]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=3,  # cap not reached
        )
        assert isinstance(decision, DispatchBlocked)

    def test_multiple_in_flight_none_overlap_allows(self):
        executing = [
            _stub("uow_a", file_scope=["tests/unit"]),
            _stub("uow_b", file_scope=["docs"]),
        ]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=3,
        )
        assert isinstance(decision, DispatchAllowed)


# ---------------------------------------------------------------------------
# Tests: gate priority (first failure wins)
# ---------------------------------------------------------------------------

class TestGatePriority:
    def test_max_parallel_fires_before_scope_check(self):
        """max_parallel cap fires even when scopes don't overlap."""
        executing = [
            _stub("uow_a", file_scope=["src/foo.py"]),
            _stub("uow_b", file_scope=["src/bar.py"]),
        ]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/baz.py"],  # no overlap
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=2,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "max_parallel" in decision.reason

    def test_shard_check_fires_before_scope_overlap(self):
        """Shard gate fires before file_scope overlap gate."""
        executing = [_stub("uow_a", file_scope=["src/foo.py"], shard_id="wos-core")]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/different.py"],  # no overlap — scope gate would pass
            candidate_shard_id="wos-core",              # shard gate fires first
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        assert isinstance(decision, DispatchBlocked)
        assert "wos-core" in decision.reason

    def test_null_in_flight_scope_skipped_in_overlap_check(self):
        """Null in-flight scope is skipped in gate 3 — annotated candidate is allowed."""
        executing = [_stub("uow_a", file_scope=None)]
        decision = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/totally_different.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=DEFAULT_MAX_PARALLEL,
        )
        # Old behavior: blocked (exclusive). New behavior: allowed (independent).
        assert isinstance(decision, DispatchAllowed)


# ---------------------------------------------------------------------------
# Tests: within-cycle accumulation (simulating the steward loop behavior)
# ---------------------------------------------------------------------------

class TestWithinCycleAccumulation:
    """
    These tests simulate what happens inside run_steward_cycle when multiple
    UoWs are processed in a single heartbeat. After each Prescribed result, the
    steward appends the prescribed UoW to _executing_uows so that subsequent
    candidates see the updated count.
    """

    def test_second_dispatch_blocked_after_first_fills_slot(self):
        """
        With max_parallel=1, prescribing one UoW blocks the next.
        Simulates the steward accumulating executing_uows within a cycle.
        """
        # Initially nothing executing
        executing = []
        # First candidate — allowed
        d1 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/foo.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=1,
        )
        assert isinstance(d1, DispatchAllowed)

        # Simulate "prescribed" — add to in-flight list
        executing = executing + [_stub("uow_a", file_scope=["src/foo.py"])]

        # Second candidate (no overlap) — now blocked because max_parallel=1 is full
        d2 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=1,
        )
        assert isinstance(d2, DispatchBlocked)

    def test_two_non_overlapping_dispatched_in_same_cycle(self):
        """
        With max_parallel=2, two non-overlapping UoWs can both be dispatched.
        """
        executing = []

        # First candidate
        d1 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/foo.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=2,
        )
        assert isinstance(d1, DispatchAllowed)

        executing = executing + [_stub("uow_a", file_scope=["src/foo.py"])]

        # Second candidate — different scope, max_parallel not yet reached
        d2 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/bar.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=2,
        )
        assert isinstance(d2, DispatchAllowed)

        executing = executing + [_stub("uow_b", file_scope=["src/bar.py"])]

        # Third candidate — max_parallel=2 is now full
        d3 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/baz.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=2,
        )
        assert isinstance(d3, DispatchBlocked)

    def test_overlapping_scope_blocked_after_first_dispatch(self):
        """
        After dispatching a UoW with a certain scope, a candidate with overlapping
        scope is blocked even if max_parallel not yet reached.
        """
        executing = []

        # Dispatch first UoW
        d1 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=3,
        )
        assert isinstance(d1, DispatchAllowed)

        executing = executing + [_stub("uow_a", file_scope=["src/orchestration"])]

        # Second candidate overlaps the first's scope — blocked by gate 4
        d2 = check_shard_dispatch_eligibility(
            candidate_file_scope=["src/orchestration/steward.py"],
            candidate_shard_id=None,
            executing_uows=executing,
            max_parallel=3,
        )
        assert isinstance(d2, DispatchBlocked)
        assert "overlap" in d2.reason


# ---------------------------------------------------------------------------
# Tests: DEFAULT_MAX_PARALLEL constant
# ---------------------------------------------------------------------------

class TestDefaultMaxParallel:
    def test_default_is_two(self):
        """DEFAULT_MAX_PARALLEL must be 2 per spec."""
        assert DEFAULT_MAX_PARALLEL == 2
