"""
Robust JSON parsing for LLM-generated prescription outputs.

This module provides multi-level fallback parsing for prescription JSON
returned from the LLM. The parser tries increasingly lenient strategies
to extract valid prescription data when the LLM fails to produce clean JSON.

Fallback levels:
0: Strict JSON parsing (json.loads)
1: Strip markdown code fences
2: Extract JSON block from prose (regex search)
3: Extract individual fields from prose
4: Return deterministic template if all else fails

Each fallback level logs its success/failure for observability.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("steward")


@dataclass
class PrescriptionParseResult:
    """Result of prescription JSON parsing with fallback tracking."""
    success: bool
    data: dict[str, Any] | None
    fallback_level: int
    error_message: str | None = None
    raw_output: str | None = None

    def __repr__(self) -> str:
        if self.success:
            return (
                f"PrescriptionParseResult(success=True, fallback_level={self.fallback_level}, "
                f"fields={list(self.data.keys()) if self.data else None})"
            )
        else:
            return (
                f"PrescriptionParseResult(success=False, fallback_level={self.fallback_level}, "
                f"error={self.error_message!r})"
            )


def _extract_json_block(text: str) -> str | None:
    """
    Extract a JSON object block from prose using regex.

    Searches for { ... } patterns and tries to parse the largest match.
    Returns the first valid JSON object found, or None if no valid block exists.

    Pure function — no side effects beyond regex matching.
    """
    # Find all potential JSON blocks { ... }
    # Uses a greedy match to capture the largest block
    pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(pattern, text)

    if not matches:
        return None

    # Try each match from largest to smallest (longest first)
    sorted_matches = sorted(matches, key=len, reverse=True)
    for candidate in sorted_matches:
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue

    return None


def _extract_fields_from_prose(text: str) -> dict[str, Any] | None:
    """
    Extract individual prescription fields from prose when structured
    parsing fails.

    Looks for field names and values using regex patterns:
    - "instructions": "..." or "instructions": value
    - "success_criteria_check": "..."
    - "estimated_cycles": number

    Returns a dict with extracted fields, or None if no usable fields found.

    Pure function — regex matching only.
    """
    fields: dict[str, Any] = {}

    # Extract instructions field
    instructions_match = re.search(
        r'"instructions"\s*:\s*"([^"]*(?:\\"[^"]*)*)"',
        text,
        re.DOTALL
    )
    if instructions_match:
        fields["instructions"] = instructions_match.group(1)
    else:
        # Try for instructions without quotes (if value is unquoted)
        instructions_match = re.search(
            r'"instructions"\s*:\s*"(.+?)"(?:,|\})',
            text,
            re.DOTALL
        )
        if instructions_match:
            fields["instructions"] = instructions_match.group(1)

    # Extract success_criteria_check field
    criteria_match = re.search(
        r'"success_criteria_check"\s*:\s*"([^"]*(?:\\"[^"]*)*)"',
        text,
        re.DOTALL
    )
    if criteria_match:
        fields["success_criteria_check"] = criteria_match.group(1)

    # Extract estimated_cycles field (must be numeric)
    cycles_match = re.search(
        r'"estimated_cycles"\s*:\s*(\d+)',
        text,
        re.DOTALL
    )
    if cycles_match:
        try:
            fields["estimated_cycles"] = int(cycles_match.group(1))
        except (ValueError, TypeError):
            pass

    # Return only if we extracted at least the instructions field
    return fields if fields.get("instructions") else None


def parse_prescription_json(
    raw_output: str,
    uow_id: str,
) -> PrescriptionParseResult:
    """
    Parse LLM prescription output with multi-level fallback resilience.

    Attempts to extract a valid prescription JSON dict from raw LLM output
    using increasingly lenient strategies:

    Level 0: Strict json.loads() — if the output is valid JSON, return it
    Level 1: Strip markdown — remove ```json ... ``` wrappers, then parse
    Level 2: Extract JSON block — search for { ... } in prose, parse largest
    Level 3: Extract fields — regex-match individual fields from prose
    Level 4: Deterministic template — return a sensible default if all fail

    Args:
        raw_output: The raw text output from the LLM
        uow_id: Unit of Work ID for logging context

    Returns:
        PrescriptionParseResult with success bool, parsed dict, and fallback_level

    Pure function — parses text immutably; only side effect is logging.
    """
    if not raw_output or not raw_output.strip():
        log.warning(
            "parse_prescription_json: empty output for %s",
            uow_id,
        )
        return PrescriptionParseResult(
            success=False,
            data=None,
            fallback_level=-1,
            error_message="empty output",
            raw_output=raw_output,
        )

    # Store original for logging/inspection
    original_output = raw_output
    working_text = raw_output.strip()

    # Level 0: Strict JSON parsing
    try:
        parsed = json.loads(working_text)
        if isinstance(parsed, dict) and parsed.get("instructions"):
            log.info(
                "parse_prescription_json: Level 0 (strict JSON) succeeded for %s",
                uow_id,
            )
            return PrescriptionParseResult(
                success=True,
                data=parsed,
                fallback_level=0,
                raw_output=original_output,
            )
    except json.JSONDecodeError as exc:
        log.debug(
            "parse_prescription_json: Level 0 (strict JSON) failed for %s — %s",
            uow_id, exc,
        )

    # Level 1: Strip markdown code fences
    if working_text.startswith("```"):
        lines = working_text.splitlines()
        working_text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

        try:
            parsed = json.loads(working_text)
            if isinstance(parsed, dict) and parsed.get("instructions"):
                log.info(
                    "parse_prescription_json: Level 1 (strip markdown) succeeded for %s",
                    uow_id,
                )
                return PrescriptionParseResult(
                    success=True,
                    data=parsed,
                    fallback_level=1,
                    raw_output=original_output,
                )
        except json.JSONDecodeError as exc:
            log.debug(
                "parse_prescription_json: Level 1 (strip markdown) failed for %s — %s",
                uow_id, exc,
            )

    # Level 2: Extract JSON block from prose
    json_block = _extract_json_block(working_text)
    if json_block:
        try:
            parsed = json.loads(json_block)
            if isinstance(parsed, dict) and parsed.get("instructions"):
                log.info(
                    "parse_prescription_json: Level 2 (extract JSON block) succeeded for %s",
                    uow_id,
                )
                return PrescriptionParseResult(
                    success=True,
                    data=parsed,
                    fallback_level=2,
                    raw_output=original_output,
                )
        except json.JSONDecodeError as exc:
            log.debug(
                "parse_prescription_json: Level 2 (extract JSON block) failed for %s — %s",
                uow_id, exc,
            )

    # Level 3: Extract individual fields from prose
    fields = _extract_fields_from_prose(working_text)
    if fields and fields.get("instructions"):
        log.info(
            "parse_prescription_json: Level 3 (extract fields) succeeded for %s",
            uow_id,
        )
        # Normalize: ensure all required fields exist
        normalized = {
            "instructions": fields.get("instructions", ""),
            "success_criteria_check": fields.get("success_criteria_check", ""),
            "estimated_cycles": fields.get("estimated_cycles", 1),
        }
        return PrescriptionParseResult(
            success=True,
            data=normalized,
            fallback_level=3,
            raw_output=original_output,
        )

    # Level 4: Deterministic template (fallback for all failures)
    log.warning(
        "parse_prescription_json: All fallback levels failed for %s — "
        "returning deterministic template",
        uow_id,
    )
    deterministic_data = {
        "instructions": (
            "No specific prescription could be generated due to LLM output parsing failure. "
            "Please examine the issue manually and provide explicit instructions. "
            "Executor: perform a minimal diagnostic pass and report what you observe."
        ),
        "success_criteria_check": (
            "Check if a clear issue diagnosis was produced. "
            "Look for any error logs or diagnostic output."
        ),
        "estimated_cycles": 1,
    }
    return PrescriptionParseResult(
        success=False,
        data=deterministic_data,
        fallback_level=4,
        error_message="all fallback levels exhausted",
        raw_output=original_output,
    )


def validate_prescription_schema(data: dict[str, Any]) -> tuple[bool, str]:
    """
    Validate that a parsed prescription dict has correct schema.

    Returns (is_valid, error_message) — is_valid=True if all fields present
    and correctly typed; error_message describes first validation failure
    encountered.

    Pure function — validates immutably.
    """
    required_fields = ["instructions", "success_criteria_check", "estimated_cycles"]

    for field in required_fields:
        if field not in data:
            return False, f"missing required field: {field}"

    if not isinstance(data["instructions"], str) or not data["instructions"].strip():
        return False, "instructions must be a non-empty string"

    if not isinstance(data["success_criteria_check"], str):
        return False, "success_criteria_check must be a string"

    if not isinstance(data["estimated_cycles"], int) or data["estimated_cycles"] < 1:
        return False, "estimated_cycles must be an integer >= 1"

    return True, ""
