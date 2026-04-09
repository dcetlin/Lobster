"""
WorkflowArtifact — Steward→Executor contract envelope.

This module defines the typed structure for the workflow artifact: the
document a Steward writes and an Executor reads. It is the boundary
between planning (Steward) and execution (Executor).

All Steward and Executor modules import from this canonical path:

    from orchestration.workflow_artifact import WorkflowArtifact, to_json, from_json

The module is intentionally self-contained: no imports from other
orchestration modules, so it can be imported in isolation without
initializing the registry.

Disk format (S3P2-B, issue #613)
---------------------------------
Artifact files are stored as front-matter + prose (``.md``) rather than pure
JSON. This makes prescriptions inspectable without a JSON parser and
eliminates JSONDecodeError failures when the LLM emits preamble before JSON.

Format on disk:

    ---json
    {"uow_id": "...", "executor_type": "...", "constraints": [], "prescribed_skills": []}
    ---
    <instructions prose>

The ``---json`` opener (not bare ``---``) is a sentinel that distinguishes disk
artifacts from the bare-``---`` YAML front-matter that the LLM writes to
stdout. The two parsers are intentionally separate:

- ``_parse_workflow_artifact`` in steward.py — parses LLM stdout (bare ``---``).
- ``from_frontmatter`` here — parses disk artifacts (``---json``).

``to_json`` / ``from_json`` are kept for backward compatibility (inline JSON in
the registry ``workflow_artifact`` column, and any legacy ``.json`` files still
on disk).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical directory for artifact files written by the Steward.
# Tilde is expanded to an absolute path at access time via artifact_path().
_ARTIFACT_DIR_TEMPLATE = "~/lobster-workspace/orchestration/artifacts"

# The only valid executor_type at present.
EXECUTOR_TYPE_GENERAL = "general"

# All fields that must be present in a valid WorkflowArtifact.
_REQUIRED_FIELDS = frozenset({"uow_id", "executor_type", "constraints", "prescribed_skills", "instructions"})


# ---------------------------------------------------------------------------
# Struct
# ---------------------------------------------------------------------------

class WorkflowArtifact(TypedDict):
    """
    The Steward→Executor contract.

    Fields
    ------
    uow_id : str
        Links to the UoWRegistry entry this artifact was produced for.
    executor_type : str
        Which Executor handles this UoW. Currently only 'general' is valid.
    constraints : list[str]
        Hard constraints on execution (e.g. 'no-network-access').
        Use [] for no constraints.
    prescribed_skills : list[str]
        Skill IDs to activate at task start.
        None (NULL in registry) = Steward did not prescribe; use active skills.
        [] = Steward explicitly prescribes no skills; deactivate contextual skills.
        Both None and [] are treated as "no skill activation required".
    instructions : str
        Natural language guidance for the Executor's LLM dispatch.
    """

    uow_id: str
    executor_type: str
    constraints: list[str]
    prescribed_skills: list[str]
    instructions: str


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def to_json(artifact: WorkflowArtifact) -> str:
    """Serialize to JSON string for storage in registry.workflow_artifact field."""
    return json.dumps(artifact)


def from_json(json_str: str) -> WorkflowArtifact:
    """
    Deserialize from JSON string.

    Raises ValueError on any parse or validation failure. Unknown extra
    fields are silently ignored for forward compatibility.

    Raises
    ------
    ValueError
        If the input is not valid JSON, or if any required field is absent.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"WorkflowArtifact: invalid JSON — possible partial write. Original error: {e}"
        ) from e

    missing = _REQUIRED_FIELDS - set(data.keys())
    if missing:
        raise ValueError(f"WorkflowArtifact missing required fields: {missing}")

    # Reconstruct using only the known fields — ignores unknown keys.
    return WorkflowArtifact(**{k: v for k, v in data.items() if k in _REQUIRED_FIELDS})


# ---------------------------------------------------------------------------
# Artifact file path utility
# ---------------------------------------------------------------------------

def artifact_path(uow_id: str) -> Path:
    """
    Return the absolute path for an artifact file.

    Convention: ~/lobster-workspace/orchestration/artifacts/{uow_id}.md

    The tilde is expanded to an absolute path at call time via
    os.path.expanduser. The caller (Steward) must create the directory
    before writing; artifact_path() does not create it.

    The returned path must be stored in the registry as an absolute string
    (str(artifact_path(uow_id))). The Executor reads the path from the
    registry field and opens it without re-expansion.
    """
    expanded = os.path.expanduser(_ARTIFACT_DIR_TEMPLATE)
    return Path(expanded) / f"{uow_id}.md"


# ---------------------------------------------------------------------------
# Front-matter serialization (disk format, S3P2-B)
# ---------------------------------------------------------------------------

# Sentinel that opens a disk artifact front-matter block.
# Distinct from bare "---" used by LLM stdout to prevent parser confusion.
_FRONTMATTER_OPENER = "---json"
_FRONTMATTER_CLOSER = "---"

# Fields included in the JSON envelope (everything except instructions).
_ENVELOPE_FIELDS = ("uow_id", "executor_type", "constraints", "prescribed_skills")


def to_frontmatter(artifact: WorkflowArtifact) -> str:
    """
    Serialize a WorkflowArtifact to front-matter + prose format for disk storage.

    Output format::

        ---json
        {"uow_id": "...", "executor_type": "...", "constraints": [], "prescribed_skills": []}
        ---
        <instructions prose>

    The JSON envelope is a single compact line (no pretty-printing) so that
    the validate-workflow-artifact hook can parse it with ``json.loads``.
    The instructions field follows verbatim after the closing ``---``.

    Pure function — no side effects.
    """
    envelope = {k: artifact[k] for k in _ENVELOPE_FIELDS}  # type: ignore[literal-required]
    envelope_line = json.dumps(envelope, separators=(",", ":"))
    return f"{_FRONTMATTER_OPENER}\n{envelope_line}\n{_FRONTMATTER_CLOSER}\n{artifact['instructions']}"


def from_frontmatter(text: str) -> WorkflowArtifact:
    """
    Deserialize a WorkflowArtifact from front-matter + prose format.

    Expects the exact format produced by ``to_frontmatter``:

        ---json
        <compact JSON envelope>
        ---
        <instructions prose>

    Raises
    ------
    ValueError
        If ``---json`` opener is absent, the JSON envelope is malformed, or
        ``executor_type`` / ``uow_id`` are missing or empty.

    Unknown extra keys in the JSON envelope are silently ignored for forward
    compatibility.
    """
    lines = text.splitlines()

    # Find the ---json opener.
    opener_idx: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip() == _FRONTMATTER_OPENER:
            opener_idx = i
            break

    if opener_idx is None:
        raise ValueError(
            "WorkflowArtifact from_frontmatter: missing '---json' opener — "
            "file may be corrupt or in the wrong format"
        )

    # The JSON envelope is on the line immediately after the opener.
    envelope_idx = opener_idx + 1
    if envelope_idx >= len(lines):
        raise ValueError(
            "WorkflowArtifact from_frontmatter: '---json' opener found but "
            "no JSON envelope line follows"
        )

    # Find the closing --- delimiter.
    closer_idx: int | None = None
    for i in range(envelope_idx + 1, len(lines)):
        if lines[i].rstrip() == _FRONTMATTER_CLOSER:
            closer_idx = i
            break

    if closer_idx is None:
        raise ValueError(
            "WorkflowArtifact from_frontmatter: missing closing '---' after JSON envelope"
        )

    # Parse the JSON envelope.
    envelope_line = lines[envelope_idx]
    try:
        envelope = json.loads(envelope_line)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"WorkflowArtifact from_frontmatter: invalid JSON envelope — "
            f"possible partial write. Original error: {e}"
        ) from e

    if not isinstance(envelope, dict):
        raise ValueError(
            "WorkflowArtifact from_frontmatter: JSON envelope must be an object"
        )

    # Validate required envelope fields.
    uow_id = envelope.get("uow_id", "")
    if not uow_id:
        raise ValueError(
            "WorkflowArtifact from_frontmatter: 'uow_id' is missing or empty "
            "in the JSON envelope"
        )

    executor_type = envelope.get("executor_type", "")
    if not executor_type:
        raise ValueError(
            "WorkflowArtifact from_frontmatter: 'executor_type' is missing or empty "
            "in the JSON envelope"
        )

    # Instructions are everything after the closing ---.
    # Join with newline; strip only leading newline from the separator.
    instructions_lines = lines[closer_idx + 1:]
    instructions = "\n".join(instructions_lines)
    # Strip a single leading newline that to_frontmatter inserts before prose.
    if instructions.startswith("\n"):
        instructions = instructions[1:]

    return WorkflowArtifact(
        uow_id=uow_id,
        executor_type=executor_type,
        constraints=envelope.get("constraints", []),
        prescribed_skills=envelope.get("prescribed_skills", []),
        instructions=instructions,
    )
