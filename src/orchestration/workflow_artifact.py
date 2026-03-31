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

    Convention: ~/lobster-workspace/orchestration/artifacts/{uow_id}.json

    The tilde is expanded to an absolute path at call time via
    os.path.expanduser. The caller (Steward) must create the directory
    before writing; artifact_path() does not create it.

    The returned path must be stored in the registry as an absolute string
    (str(artifact_path(uow_id))). The Executor reads the path from the
    registry field and opens it without re-expansion.
    """
    expanded = os.path.expanduser(_ARTIFACT_DIR_TEMPLATE)
    return Path(expanded) / f"{uow_id}.json"
