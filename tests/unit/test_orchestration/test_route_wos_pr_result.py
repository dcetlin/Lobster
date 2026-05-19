"""
Unit tests for route_wos_pr_result() in dispatcher_handlers.py.

Four cases from the PR #1223 description:
  1. WOS PR (task_id starts with "wos-") → coordinator spawned
  2. Non-WOS PR (task_id does not start with "wos-") → fallthrough
  3. Missing / malformed fields handled gracefully → fallthrough, no exception
  4. Correct routing when both WOS and non-WOS PRs share the same result batch
     (multiple independent calls behave consistently per task_id prefix)

route_wos_pr_result is a pure function: no I/O, no side effects.
All test cases verify the returned dict; no mocking required.
"""

from __future__ import annotations

import pytest

from src.orchestration.dispatcher_handlers import route_wos_pr_result


# ---------------------------------------------------------------------------
# Constants used across multiple tests — imported from production module.
# ---------------------------------------------------------------------------

_WOS_PR_URL = "https://github.com/dcetlin/Lobster/pull/999"
_NON_WOS_PR_URL = "https://github.com/dcetlin/Lobster/pull/888"
_WOS_TASK_ID = "wos-executor-fix-pr999"
_NON_WOS_TASK_ID = "fix-pr-888-r1"
_CHAT_ID = 12345
_RESULT_TEXT = "PR opened: https://github.com/dcetlin/Lobster/pull/999\nImplementation complete."


# ---------------------------------------------------------------------------
# Case 1: WOS PR with task_id starting "wos-" → coordinator spawned
# ---------------------------------------------------------------------------


class TestWosPrRoutesToCoordinator:
    """WOS-originated PRs must spawn the wos-pr-coordinator agent."""

    def test_action_is_spawn_subagent(self) -> None:
        """Return action must be 'spawn_subagent' for a WOS task_id."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "spawn_subagent"

    def test_agent_type_is_lobster_generalist(self) -> None:
        """Agent type must be 'lobster-generalist' for coordinator dispatch."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["agent_type"] == "lobster-generalist"

    def test_task_id_encodes_pr_number(self) -> None:
        """Coordinator task_id must contain the PR number from the URL."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert "999" in result["task_id"]

    def test_task_id_prefixed_wos_pr_coord(self) -> None:
        """Coordinator task_id follows the 'wos-pr-coord-{number}' pattern."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["task_id"] == "wos-pr-coord-999"

    def test_prompt_contains_pr_number(self) -> None:
        """Coordinator prompt must reference the PR number."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert "999" in result["prompt"]

    def test_prompt_contains_pr_url(self) -> None:
        """Coordinator prompt must include the PR URL verbatim."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert _WOS_PR_URL in result["prompt"]

    def test_prompt_contains_repo(self) -> None:
        """Coordinator prompt must include the repo identifier parsed from the URL."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert "dcetlin/Lobster" in result["prompt"]

    def test_prompt_contains_wos_pr_coordinator_reference(self) -> None:
        """Coordinator prompt must point to the agent definition file."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert "wos-pr-coordinator" in result["prompt"]

    def test_prompt_has_yaml_frontmatter(self) -> None:
        """Coordinator prompt must start with YAML frontmatter (dispatch template gate)."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["prompt"].startswith("---\n")

    def test_result_text_truncated_in_prompt(self) -> None:
        """result_text included in coordinator prompt must be capped at 500 chars."""
        long_text = "x" * 1000
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=long_text,
        )
        # The raw long_text should not appear verbatim (it's 1000 chars; only 500 inserted)
        assert long_text not in result["prompt"]
        assert long_text[:500] in result["prompt"]

    def test_wos_prefix_exact_match(self) -> None:
        """task_id must start with 'wos-' — bare 'wos' without dash falls through."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id="wos_executor_fix",  # underscore, not dash
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"


# ---------------------------------------------------------------------------
# Case 2: Non-WOS PR → falls through to existing path, action is "fallthrough"
# ---------------------------------------------------------------------------


class TestNonWosPrFallsThrough:
    """Non-WOS PRs must return action='fallthrough', leaving existing path intact."""

    def test_non_wos_task_id_falls_through(self) -> None:
        """A task_id not starting with 'wos-' must produce action='fallthrough'."""
        result = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id=_NON_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result == {"action": "fallthrough"}

    def test_fallthrough_result_has_no_extra_keys(self) -> None:
        """Fallthrough result must contain only the 'action' key — no leakage."""
        result = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id=_NON_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert set(result.keys()) == {"action"}

    def test_task_id_oracle_prefix_falls_through(self) -> None:
        """task_id 'oracle-pr-888' (oracle agent, not WOS) must fall through."""
        result = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id="oracle-pr-888-review",
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"


# ---------------------------------------------------------------------------
# Case 3: Missing / malformed fields handled gracefully — no exception raised
# ---------------------------------------------------------------------------


class TestMissingOrMalformedFields:
    """Malformed inputs must return fallthrough, never raise exceptions."""

    def test_none_task_id_falls_through(self) -> None:
        """task_id=None must produce fallthrough, not an AttributeError."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=None,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"

    def test_empty_task_id_falls_through(self) -> None:
        """task_id='' (empty string) must produce fallthrough."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id="",
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"

    def test_malformed_pr_url_falls_through(self) -> None:
        """A PR URL that cannot be parsed must produce fallthrough, not ValueError."""
        result = route_wos_pr_result(
            pr_url="not-a-valid-url",
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"

    def test_pr_url_with_non_integer_pr_number_falls_through(self) -> None:
        """PR URL ending in a non-integer segment must produce fallthrough."""
        result = route_wos_pr_result(
            pr_url="https://github.com/dcetlin/Lobster/pull/not-a-number",
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "fallthrough"

    def test_pr_url_with_trailing_slash_parsed_correctly(self) -> None:
        """PR URL with trailing slash must still parse to the correct PR number."""
        result = route_wos_pr_result(
            pr_url="https://github.com/dcetlin/Lobster/pull/999/",
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "spawn_subagent"
        assert "999" in result["task_id"]

    def test_empty_result_text_does_not_raise(self) -> None:
        """result_text='' must not cause any exception."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text="",
        )
        assert result["action"] == "spawn_subagent"

    def test_integer_chat_id_passthrough(self) -> None:
        """Integer chat_id must be accepted and included in the prompt."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=99999,
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "spawn_subagent"
        assert "99999" in result["prompt"]

    def test_string_chat_id_passthrough(self) -> None:
        """String chat_id (Slack channel IDs) must be accepted and included in the prompt."""
        result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id="C0123456789",
            result_text=_RESULT_TEXT,
        )
        assert result["action"] == "spawn_subagent"
        assert "C0123456789" in result["prompt"]


# ---------------------------------------------------------------------------
# Case 4: Correct routing when both WOS and non-WOS PRs are in the same batch
#
# The function is stateless — it routes each call independently based on the
# task_id prefix. Calling it for WOS and non-WOS inputs in sequence must
# produce the correct action for each call, with no cross-call state leakage.
# ---------------------------------------------------------------------------


class TestMixedBatchRouting:
    """WOS and non-WOS calls interleaved must route independently and correctly."""

    def test_wos_call_in_batch_spawns_coordinator(self) -> None:
        """A WOS call in a mixed batch must still produce spawn_subagent."""
        wos_result = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert wos_result["action"] == "spawn_subagent"

    def test_non_wos_call_in_batch_falls_through(self) -> None:
        """A non-WOS call in a mixed batch must still produce fallthrough."""
        non_wos_result = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id=_NON_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert non_wos_result["action"] == "fallthrough"

    def test_wos_and_non_wos_calls_produce_independent_results(self) -> None:
        """Interleaving WOS and non-WOS calls must not affect each other's routing."""
        # Call order: WOS → non-WOS → WOS — middle call must not infect neighbors
        r1 = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        r2 = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id=_NON_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        r3 = route_wos_pr_result(
            pr_url=_WOS_PR_URL,
            task_id=_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert r1["action"] == "spawn_subagent"
        assert r2["action"] == "fallthrough"
        assert r3["action"] == "spawn_subagent"

    def test_distinct_wos_prs_get_distinct_coordinator_task_ids(self) -> None:
        """Two WOS PRs with different PR numbers must produce different task_ids."""
        r_999 = route_wos_pr_result(
            pr_url="https://github.com/dcetlin/Lobster/pull/999",
            task_id="wos-executor-fix-pr999",
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        r_1000 = route_wos_pr_result(
            pr_url="https://github.com/dcetlin/Lobster/pull/1000",
            task_id="wos-executor-fix-pr1000",
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert r_999["task_id"] != r_1000["task_id"]
        assert r_999["task_id"] == "wos-pr-coord-999"
        assert r_1000["task_id"] == "wos-pr-coord-1000"

    def test_non_wos_fallthrough_has_no_coordinator_artifacts(self) -> None:
        """Fallthrough result must not leak any coordinator fields into the dict."""
        non_wos_result = route_wos_pr_result(
            pr_url=_NON_WOS_PR_URL,
            task_id=_NON_WOS_TASK_ID,
            chat_id=_CHAT_ID,
            result_text=_RESULT_TEXT,
        )
        assert "prompt" not in non_wos_result
        assert "task_id" not in non_wos_result
        assert "agent_type" not in non_wos_result
