#!/usr/bin/env python3
"""
PostToolUse hook: Validate WorkflowArtifact front-matter after Write calls.

Fires after Write tool calls that write to the orchestration artifacts directory.
Validates that the written file contains a valid WorkflowArtifact front-matter
envelope with all required fields present and executor_type in the allowed set.

This enforces schema at the commit boundary so malformed prescription artifacts
are caught before the Executor reads them. Without this hook, validation only
fires at parse time in from_frontmatter() — too late to give actionable feedback.

Disk format (S3P2-B, issue #613):

    ---json
    {"uow_id": "...", "executor_type": "...", "constraints": [], "prescribed_skills": []}
    ---
    <instructions prose>

The ``---json`` sentinel distinguishes disk artifacts from the LLM stdout
format (bare ``---``). This hook validates only disk artifacts (the new .md
format). Legacy .json files are not validated by this hook.

Exit codes:
  0  - Validation passed (or file is not a workflow artifact — not our concern)
  2  - Validation failed — Claude Code shows stderr to Claude and retries

Scope: fires only for Write calls to paths matching
  */orchestration/artifacts/*.md
  (never for archived/ paths — cleanup arc writes are not prescriptions)
"""

import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Required fields for a valid WorkflowArtifact JSON envelope.
_REQUIRED_ENVELOPE_FIELDS = frozenset({
    "uow_id",
    "executor_type",
    "constraints",
    "prescribed_skills",
})

# Valid executor types — must match the set in workflow_artifact.py and executor.py.
_VALID_EXECUTOR_TYPES = frozenset({
    "general",
    "functional-engineer",
    "lobster-ops",
})

# Pattern for artifact paths we validate.
# Matches: */orchestration/artifacts/<uow_id>.md
# Excludes: */orchestration/artifacts/archived/* (cleanup arc output)
_ARTIFACT_PATH_RE = re.compile(
    r"/orchestration/artifacts/(?!archived/)[^/]+\.md$"
)

# Sentinel that opens a disk artifact front-matter block.
_FRONTMATTER_OPENER = "---json"
_FRONTMATTER_CLOSER = "---"


# ---------------------------------------------------------------------------
# Pure helpers — testable in isolation
# ---------------------------------------------------------------------------

def _is_artifact_path(file_path: str) -> bool:
    """Return True if this Write target is a workflow artifact we should validate."""
    return bool(_ARTIFACT_PATH_RE.search(file_path))


def _validate_artifact(content: str, file_path: str) -> list[str]:
    """
    Validate WorkflowArtifact front-matter + prose content.

    Returns a list of error strings. Empty list means valid.
    Pure function — no side effects.
    """
    errors: list[str] = []
    lines = content.splitlines()

    # Find the ---json opener.
    opener_idx: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip() == _FRONTMATTER_OPENER:
            opener_idx = i
            break

    if opener_idx is None:
        errors.append(
            f"Missing '---json' opener. WorkflowArtifact .md files must begin with:\n"
            f"  ---json\n"
            f"  {{...JSON envelope...}}\n"
            f"  ---\n"
            f"  <instructions>"
        )
        return errors

    # The JSON envelope is on the line immediately after the opener.
    envelope_idx = opener_idx + 1
    if envelope_idx >= len(lines):
        errors.append(
            "No JSON envelope line found after '---json' opener."
        )
        return errors

    # Find the closing --- delimiter.
    closer_idx: int | None = None
    for i in range(envelope_idx + 1, len(lines)):
        if lines[i].rstrip() == _FRONTMATTER_CLOSER:
            closer_idx = i
            break

    if closer_idx is None:
        errors.append(
            "Missing closing '---' after JSON envelope line."
        )
        return errors

    # Parse the JSON envelope.
    envelope_line = lines[envelope_idx]
    try:
        envelope = json.loads(envelope_line)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON in envelope line: {e}")
        return errors

    if not isinstance(envelope, dict):
        errors.append("JSON envelope must be an object, not a list or scalar.")
        return errors

    # Check required envelope fields.
    missing = _REQUIRED_ENVELOPE_FIELDS - set(envelope.keys())
    if missing:
        errors.append(
            f"WorkflowArtifact JSON envelope missing required fields: {sorted(missing)}\n"
            f"Required fields are: {sorted(_REQUIRED_ENVELOPE_FIELDS)}"
        )

    # Validate uow_id is non-empty.
    uow_id = envelope.get("uow_id", "")
    if not uow_id:
        errors.append("'uow_id' must be present and non-empty.")

    # Validate executor_type value.
    executor_type = envelope.get("executor_type", "")
    if executor_type and executor_type not in _VALID_EXECUTOR_TYPES:
        errors.append(
            f"Invalid executor_type: {executor_type!r}\n"
            f"Valid values: {sorted(_VALID_EXECUTOR_TYPES)}"
        )

    # Validate list fields.
    prescribed_skills = envelope.get("prescribed_skills")
    if prescribed_skills is not None and not isinstance(prescribed_skills, list):
        errors.append(
            f"prescribed_skills must be a list, got {type(prescribed_skills).__name__}"
        )

    constraints = envelope.get("constraints")
    if constraints is not None and not isinstance(constraints, list):
        errors.append(
            f"constraints must be a list, got {type(constraints).__name__}"
        )

    return errors


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # Malformed hook input — don't block
        return 0

    tool_name = data.get("tool_name", "")
    if tool_name != "Write":
        return 0

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    content = tool_input.get("content", "")

    if not _is_artifact_path(file_path):
        return 0

    errors = _validate_artifact(content, file_path)
    if not errors:
        return 0

    # Validation failed — exit 2 to block and show error to Claude
    error_msg = (
        f"WorkflowArtifact validation FAILED for {file_path}:\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\n\nCorrect the artifact before writing. "
        "Required format:\n"
        "  ---json\n"
        '  {"uow_id": "...", "executor_type": "...", "constraints": [], "prescribed_skills": []}\n'
        "  ---\n"
        "  <instructions prose>\n"
        f"Valid executor_type values: {sorted(_VALID_EXECUTOR_TYPES)}"
    )
    print(error_msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
