"""
Unit tests for WorkflowArtifact — workflow_artifact.py

Tests are written before implementation (TDD). They cover:
- Import path stability
- Round-trip serialization (all 5 required fields)
- from_json() raises ValueError on missing fields (all 5 missing, single missing)
- from_json() ignores unknown extra fields (forward compatibility)
- from_json() raises ValueError (not json.JSONDecodeError) on malformed JSON
- prescribed_skills=[] round-trips as [] (not None)
- Artifact path utility: tilde expansion, absolute path
- Audit log storability: to_json() → note → from_json() round-trip
"""

import json
import os
import pytest


# ---------------------------------------------------------------------------
# Import path stability — must never break even if file is moved
# ---------------------------------------------------------------------------

def test_import_path_stable():
    """The canonical import path must work. Catches accidental file renames."""
    from orchestration.workflow_artifact import WorkflowArtifact, to_json, from_json
    assert WorkflowArtifact is not None
    assert callable(to_json)
    assert callable(from_json)


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

def test_round_trip_all_fields():
    """Create WorkflowArtifact with all 5 fields set to non-default values, verify round-trip."""
    from orchestration.workflow_artifact import WorkflowArtifact, to_json, from_json

    artifact: WorkflowArtifact = {
        "uow_id": "uow_20260330_abc123",
        "executor_type": "general",
        "constraints": ["no-network-access", "max-5-minutes"],
        "prescribed_skills": ["systematic-debugging"],
        "instructions": "Implement the feature described in the UoW summary.",
    }

    serialized = to_json(artifact)
    recovered = from_json(serialized)

    assert recovered["uow_id"] == "uow_20260330_abc123"
    assert recovered["executor_type"] == "general"
    assert recovered["constraints"] == ["no-network-access", "max-5-minutes"]
    assert recovered["prescribed_skills"] == ["systematic-debugging"]
    assert recovered["instructions"] == "Implement the feature described in the UoW summary."


def test_round_trip_prescribed_skills_empty_list():
    """prescribed_skills=[] must round-trip as [] (not None)."""
    from orchestration.workflow_artifact import WorkflowArtifact, to_json, from_json

    artifact: WorkflowArtifact = {
        "uow_id": "uow_20260330_def456",
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": "No skills required.",
    }

    serialized = to_json(artifact)
    recovered = from_json(serialized)

    assert recovered["prescribed_skills"] == []
    assert recovered["prescribed_skills"] is not None


# ---------------------------------------------------------------------------
# from_json() validation — missing fields
# ---------------------------------------------------------------------------

def test_from_json_empty_object_raises_value_error():
    """from_json('{}') raises ValueError naming all 5 missing fields."""
    from orchestration.workflow_artifact import from_json

    with pytest.raises(ValueError) as exc_info:
        from_json("{}")

    error_message = str(exc_info.value)
    for field in ["uow_id", "executor_type", "constraints", "prescribed_skills", "instructions"]:
        assert field in error_message, f"Missing field '{field}' not mentioned in error: {error_message}"


def test_from_json_four_of_five_fields_raises_value_error():
    """from_json() with 4 of 5 fields raises ValueError naming the single missing field."""
    from orchestration.workflow_artifact import from_json

    # All fields except 'instructions'
    data = json.dumps({
        "uow_id": "uow_20260330_abc123",
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
    })

    with pytest.raises(ValueError) as exc_info:
        from_json(data)

    assert "instructions" in str(exc_info.value)


def test_from_json_missing_uow_id_raises_value_error():
    """from_json() with missing uow_id raises ValueError naming uow_id."""
    from orchestration.workflow_artifact import from_json

    data = json.dumps({
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": "Do something.",
    })

    with pytest.raises(ValueError) as exc_info:
        from_json(data)

    assert "uow_id" in str(exc_info.value)


# ---------------------------------------------------------------------------
# from_json() forward compatibility — unknown extra fields
# ---------------------------------------------------------------------------

def test_from_json_ignores_unknown_fields():
    """from_json() with all 5 required fields plus unknown extras must succeed."""
    from orchestration.workflow_artifact import from_json

    data = json.dumps({
        "uow_id": "uow_20260330_abc123",
        "executor_type": "general",
        "constraints": [],
        "prescribed_skills": [],
        "instructions": "Do something.",
        # Phase 3 might add these — must be ignored in Phase 2
        "priority": "high",
        "deadline_at": "2026-04-01T00:00:00Z",
    })

    result = from_json(data)
    assert result["uow_id"] == "uow_20260330_abc123"
    assert "priority" not in result
    assert "deadline_at" not in result


# ---------------------------------------------------------------------------
# from_json() error handling — malformed JSON
# ---------------------------------------------------------------------------

def test_from_json_malformed_json_raises_value_error_not_json_error():
    """from_json() on malformed JSON must raise ValueError (not json.JSONDecodeError)."""
    from orchestration.workflow_artifact import from_json

    with pytest.raises(ValueError) as exc_info:
        from_json("{this is not valid json}")

    # Must NOT be a json.JSONDecodeError
    assert type(exc_info.value) is ValueError
    # Error message must reference partial write context
    error_message = str(exc_info.value)
    assert "WorkflowArtifact" in error_message
    assert "partial write" in error_message


def test_from_json_malformed_json_includes_original_error():
    """from_json() on malformed JSON error message includes the original parse error."""
    from orchestration.workflow_artifact import from_json

    with pytest.raises(ValueError) as exc_info:
        from_json("not json at all")

    # The original error message must be chained/included
    error_message = str(exc_info.value)
    assert "Original error" in error_message


# ---------------------------------------------------------------------------
# Artifact path utilities
# ---------------------------------------------------------------------------

def test_artifact_path_expands_tilde():
    """artifact_path(uow_id) must return an absolute path (no tilde prefix)."""
    from orchestration.workflow_artifact import artifact_path

    path = artifact_path("uow_20260330_abc123")
    assert not str(path).startswith("~"), f"Path must not start with ~: {path}"
    assert os.path.isabs(str(path)), f"Path must be absolute: {path}"


def test_artifact_path_uses_uow_id_as_filename():
    """artifact_path(uow_id) must end with {uow_id}.json."""
    from orchestration.workflow_artifact import artifact_path

    uow_id = "uow_20260330_abc123"
    path = artifact_path(uow_id)
    assert str(path).endswith(f"{uow_id}.json"), f"Path must end with {uow_id}.json: {path}"


def test_artifact_path_uses_canonical_directory():
    """artifact_path must point into the orchestration/artifacts/ directory."""
    from orchestration.workflow_artifact import artifact_path

    path = artifact_path("uow_20260330_abc123")
    assert "orchestration/artifacts" in str(path), f"Path must contain orchestration/artifacts: {path}"


# ---------------------------------------------------------------------------
# Audit log storability
# ---------------------------------------------------------------------------

def test_audit_log_round_trip():
    """
    WorkflowArtifact serialized via to_json(), stored as a note string,
    then from_json() on retrieval must produce an equal struct.
    """
    from orchestration.workflow_artifact import WorkflowArtifact, to_json, from_json

    original: WorkflowArtifact = {
        "uow_id": "uow_20260330_audit99",
        "executor_type": "general",
        "constraints": ["constraint-a"],
        "prescribed_skills": ["systematic-debugging", "verification-before-completion"],
        "instructions": "Validate the pipeline end-to-end.",
    }

    # Simulate storing in audit_log.note
    note_value = to_json(original)
    assert isinstance(note_value, str)

    # Simulate reading back from audit_log.note
    recovered = from_json(note_value)

    assert recovered["uow_id"] == original["uow_id"]
    assert recovered["executor_type"] == original["executor_type"]
    assert recovered["constraints"] == original["constraints"]
    assert recovered["prescribed_skills"] == original["prescribed_skills"]
    assert recovered["instructions"] == original["instructions"]
