#!/usr/bin/env python3
"""
PostToolUse hook: Validate WorkflowArtifact JSON schema after Write calls.

Fires after Write tool calls that write to the orchestration artifacts directory.
Validates that the written file contains a valid WorkflowArtifact envelope with
all required fields present and executor_type in the allowed set.

This enforces schema at the commit boundary so that hard-cap cleanup does not
archive malformed prescription artifacts. Without this hook, the validator
exists in workflow_artifact.py but only fires at parse time (too late).

Exit codes:
  0  - Validation passed (or file is not a workflow artifact — not our concern)
  2  - Validation failed — Claude Code shows stderr to Claude and retries

Scope: fires only for Write calls to paths matching
  */orchestration/artifacts/*.json
  (never for archived/ paths — cleanup arc writes are not prescriptions)
"""

import json
import re
import sys
from pathlib import Path


# Required fields for a valid WorkflowArtifact
_REQUIRED_FIELDS = frozenset({
    "uow_id",
    "executor_type",
    "constraints",
    "prescribed_skills",
    "instructions",
})

# Valid executor types — must match the set in workflow_artifact.py
_VALID_EXECUTOR_TYPES = frozenset({
    "general",
    "functional-engineer",
    "lobster-ops",
})

# Pattern for artifact paths we validate.
# Matches: */orchestration/artifacts/<uow_id>.json
# Excludes: */orchestration/artifacts/archived/* (cleanup arc output)
_ARTIFACT_PATH_RE = re.compile(
    r"/orchestration/artifacts/(?!archived/)[^/]+\.json$"
)


def _is_artifact_path(file_path: str) -> bool:
    """Return True if this Write target is a workflow artifact we should validate."""
    return bool(_ARTIFACT_PATH_RE.search(file_path))


def _validate_artifact(content: str, file_path: str) -> list[str]:
    """
    Validate WorkflowArtifact JSON content.

    Returns a list of error strings. Empty list means valid.
    Pure function — no side effects.
    """
    errors: list[str] = []

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON in workflow artifact: {e}")
        return errors

    if not isinstance(data, dict):
        errors.append("WorkflowArtifact must be a JSON object, not a list or scalar.")
        return errors

    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        errors.append(
            f"WorkflowArtifact missing required fields: {sorted(missing)}\n"
            f"Required fields are: {sorted(_REQUIRED_FIELDS)}"
        )

    executor_type = data.get("executor_type")
    if executor_type is not None and executor_type not in _VALID_EXECUTOR_TYPES:
        errors.append(
            f"Invalid executor_type: {executor_type!r}\n"
            f"Valid values: {sorted(_VALID_EXECUTOR_TYPES)}"
        )

    prescribed_skills = data.get("prescribed_skills")
    if prescribed_skills is not None and not isinstance(prescribed_skills, list):
        errors.append(
            f"prescribed_skills must be a list, got {type(prescribed_skills).__name__}"
        )

    constraints = data.get("constraints")
    if constraints is not None and not isinstance(constraints, list):
        errors.append(
            f"constraints must be a list, got {type(constraints).__name__}"
        )

    instructions = data.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        errors.append(
            f"instructions must be a string, got {type(instructions).__name__}"
        )

    return errors


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
        + "\n\nCorrect the artifact JSON before writing. "
        "Required fields: uow_id, executor_type, constraints, prescribed_skills, instructions. "
        f"Valid executor_type values: {sorted(_VALID_EXECUTOR_TYPES)}"
    )
    print(error_msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
