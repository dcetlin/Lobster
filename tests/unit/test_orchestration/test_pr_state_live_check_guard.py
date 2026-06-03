"""
Tests for the PR-state live-check guard in handle_wos_execute() — Issue #d70896.

Root cause: wos-surface-3d94be-triage declared PR #1375 as merged without calling
`gh pr view`, causing 10+ hours of spurious looping.

The guard must:
1. Be present in the prompt between the PR-close guard and ## Instructions sections.
2. Require `gh pr view <PR_NUMBER> --repo <OWNER/REPO> --json state,mergedAt`.
3. Explicitly forbid inferring PR state from relay messages, registry state, memory,
   or oracle verdict files.
4. Cover every PR state check, not just the specific scenario that triggered the bug.

Named constants derived from the spec:

    LIVE_CHECK_COMMAND — the exact gh command agents must use
    FORBIDDEN_SOURCES  — sources agents must not use to infer PR state
    GUARD_HEADER       — section heading that identifies the guard in the prompt

These constants are tested both for presence in the prompt and for correctness
relative to the spec, so that future edits that weaken or remove the guard fail fast.
"""

from __future__ import annotations

import pytest

from src.orchestration.dispatcher_handlers import handle_wos_execute

# ---------------------------------------------------------------------------
# Named constants from the spec (issue uow_20260602_d70896)
# ---------------------------------------------------------------------------

#: The gh command agents must call to verify PR state — no variations accepted.
LIVE_CHECK_COMMAND = "gh pr view <PR_NUMBER> --repo <OWNER/REPO> --json state,mergedAt"

#: The JSON field that must read "MERGED" for a PR to be considered merged.
MERGED_STATE_FIELD = 'state == "MERGED"'

#: Section heading that identifies this guard in the prompt.
GUARD_HEADER = "## PR-state live-check guard (REQUIRED)"

#: Sources that must be explicitly forbidden by the guard.
FORBIDDEN_SOURCES = [
    "relay messages",
    "Registry state",
    "UoW records",
    "memory",
    "Oracle verdict files",
    "cached state",
]

# Minimal test inputs — content does not affect prompt structure.
_TEST_UOW_ID = "uow_20260602_test001"
_TEST_INSTRUCTIONS = "Open a PR and verify it is merged."
_TEST_OUTPUT_REF = "/tmp/uow_20260602_test001.result.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def executor_prompt() -> str:
    """Return the prompt built by handle_wos_execute for a representative UoW."""
    return handle_wos_execute(_TEST_UOW_ID, _TEST_INSTRUCTIONS, _TEST_OUTPUT_REF)


# ---------------------------------------------------------------------------
# Tests: guard presence and ordering
# ---------------------------------------------------------------------------

def test_pr_state_live_check_guard_present(executor_prompt: str) -> None:
    """Guard section heading must appear in the executor prompt."""
    assert GUARD_HEADER in executor_prompt, (
        f"Executor prompt is missing '{GUARD_HEADER}'. "
        "Agents will infer PR state from relay text without this guard."
    )


def test_live_check_guard_appears_after_pr_close_guard(executor_prompt: str) -> None:
    """Live-check guard must appear after the PR-close guard, not before it."""
    pr_close_pos = executor_prompt.find("## PR-close guard (REQUIRED)")
    live_check_pos = executor_prompt.find(GUARD_HEADER)

    assert pr_close_pos != -1, "PR-close guard section not found"
    assert live_check_pos != -1, f"'{GUARD_HEADER}' not found"
    assert live_check_pos > pr_close_pos, (
        "Live-check guard appears before the PR-close guard — wrong order. "
        "It must follow the PR-close guard to preserve prompt structure."
    )


def test_live_check_guard_appears_before_instructions(executor_prompt: str) -> None:
    """Live-check guard must appear before the ## Instructions section."""
    live_check_pos = executor_prompt.find(GUARD_HEADER)
    instructions_pos = executor_prompt.find("## Instructions")

    assert live_check_pos != -1, f"'{GUARD_HEADER}' not found"
    assert instructions_pos != -1, "## Instructions section not found"
    assert live_check_pos < instructions_pos, (
        "Live-check guard appears after ## Instructions — wrong order. "
        "Guards must precede the instructions they govern."
    )


# ---------------------------------------------------------------------------
# Tests: gh command requirement
# ---------------------------------------------------------------------------

def test_live_check_guard_requires_gh_pr_view_command(executor_prompt: str) -> None:
    """Guard must specify the exact gh pr view command with --json state,mergedAt."""
    assert LIVE_CHECK_COMMAND in executor_prompt, (
        f"Guard does not contain the required command: '{LIVE_CHECK_COMMAND}'. "
        "Agents will not know which flags to use when verifying PR state."
    )


def test_live_check_guard_requires_merged_state_check(executor_prompt: str) -> None:
    """Guard must instruct agents to check that state == 'MERGED' in the JSON output."""
    assert MERGED_STATE_FIELD in executor_prompt, (
        f"Guard does not instruct agents to check '{MERGED_STATE_FIELD}'. "
        "Agents may misread non-MERGED states without this explicit check."
    )


def test_live_check_guard_treats_gh_failure_as_not_merged(executor_prompt: str) -> None:
    """Guard must specify that gh pr view failure means 'treat as NOT merged'."""
    assert "NOT merged" in executor_prompt or "not merged" in executor_prompt, (
        "Guard does not specify how to handle gh pr view failures. "
        "Agents may assume a PR is merged when the gh call fails."
    )


# ---------------------------------------------------------------------------
# Tests: forbidden inference sources
# ---------------------------------------------------------------------------

def test_live_check_guard_forbids_relay_message_inference(executor_prompt: str) -> None:
    """Guard must forbid inferring PR state from relay messages or Telegram text."""
    guard_section = _extract_guard_section(executor_prompt)
    assert "relay" in guard_section.lower() or "telegram" in guard_section.lower(), (
        "Guard does not forbid inferring PR state from relay messages. "
        "This is the exact source of the bug (wos-surface-3d94be-triage / PR #1375)."
    )


def test_live_check_guard_forbids_registry_state_inference(executor_prompt: str) -> None:
    """Guard must forbid inferring PR state from registry state or UoW records."""
    guard_section = _extract_guard_section(executor_prompt)
    assert "registry" in guard_section.lower() or "uow" in guard_section.lower(), (
        "Guard does not forbid inferring PR state from registry or UoW state. "
        "Agents may read UoW fields as a proxy for PR state without this prohibition."
    )


def test_live_check_guard_forbids_memory_inference(executor_prompt: str) -> None:
    """Guard must forbid inferring PR state from memory."""
    guard_section = _extract_guard_section(executor_prompt)
    assert "memory" in guard_section.lower(), (
        "Guard does not forbid inferring PR state from memory. "
        "Agents may use stale cached state from prior sessions."
    )


def test_live_check_guard_forbids_oracle_verdict_inference(executor_prompt: str) -> None:
    """Guard must forbid inferring PR state from oracle verdict files."""
    guard_section = _extract_guard_section(executor_prompt)
    assert "oracle" in guard_section.lower(), (
        "Guard does not forbid inferring PR state from oracle verdict files. "
        "Oracle files record review state, not merge state — they must not be conflated."
    )


def test_live_check_guard_scope_is_universal(executor_prompt: str) -> None:
    """Guard must declare it applies to every PR state check, not just some."""
    guard_section = _extract_guard_section(executor_prompt)
    # The guard should explicitly say it applies "regardless of phrasing" or "every"
    assert ("every" in guard_section.lower() or "regardless" in guard_section.lower()), (
        "Guard does not declare universal scope. "
        "Agents may skip the live check when instructions are phrased differently."
    )


# ---------------------------------------------------------------------------
# Tests: guard does not break existing guards
# ---------------------------------------------------------------------------

def test_pr_close_guard_still_present_after_new_guard(executor_prompt: str) -> None:
    """Adding the live-check guard must not remove or truncate the PR-close guard."""
    assert "## PR-close guard (REQUIRED)" in executor_prompt, (
        "PR-close guard was removed or truncated when the live-check guard was inserted."
    )


def test_instructions_still_present_after_new_guard(executor_prompt: str) -> None:
    """The ## Instructions section must still be present and non-empty."""
    assert "## Instructions" in executor_prompt, (
        "## Instructions section was removed or truncated by the live-check guard insertion."
    )
    instructions_pos = executor_prompt.find("## Instructions")
    content_after = executor_prompt[instructions_pos + len("## Instructions"):].strip()
    assert content_after, "## Instructions section is present but empty after the guard was added."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_guard_section(prompt: str) -> str:
    """Extract just the live-check guard section from the prompt for scoped assertions."""
    start = prompt.find(GUARD_HEADER)
    if start == -1:
        return ""
    # Find the next ## heading after the guard to bound the section
    end = prompt.find("\n## ", start + len(GUARD_HEADER))
    if end == -1:
        return prompt[start:]
    return prompt[start:end]
