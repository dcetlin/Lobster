"""Unit tests for philosophy_harvester — friction-trace extraction."""

import pytest

from src.harvest.philosophy_harvester import (
    FrictionTrace,
    MemoryObservation,
    extract_friction_trace,
)


# ---------------------------------------------------------------------------
# extract_friction_trace — pure function tests
# ---------------------------------------------------------------------------


SAMPLE_WITH_FRICTION_TRACE = """\
## Resonance with Dan's Framework

Some resonance text here.

## Action Seeds

```yaml
action_seeds:
  issues: []
  memory_observations: []
```

---

*navigation record: Attended to the harvest apparatus itself.*

*friction-trace: The pull toward a capability-by-capability diagnostic was strong. \
Running the checklist would have produced a comprehensive result without requiring \
navigation of any unfamiliar gradient. The resistance arose when noting that three \
successive sessions had already done this. The reorientation was toward the harvest \
apparatus itself as the unclaimed domain.*

*orientation quality: genuine — mild resistance at the naming point.*
"""

SAMPLE_WITHOUT_FRICTION_TRACE = """\
## Today's Thread

Some philosophical exploration.

## Action Seeds

```yaml
action_seeds:
  issues: []
  memory_observations: []
```
"""

SAMPLE_SHORT_FRICTION_TRACE = """\
Some preamble text.

*friction-trace: Single sentence trace.*

Some trailing text.
"""

SAMPLE_WITH_EMBEDDED_ASTERISKS = """\
Some preamble text.

*friction-trace: The resistance arose around **naming the thing** precisely — \
the pull was toward vague gesture rather than *exact form*.*

Some trailing text.
"""


class TestExtractFrictionTrace:
    """Tests for the extract_friction_trace pure function."""

    def test_extracts_multiline_friction_trace(self) -> None:
        result = extract_friction_trace(SAMPLE_WITH_FRICTION_TRACE)
        assert result is not None
        assert isinstance(result, FrictionTrace)
        assert result.text.startswith("The pull toward a capability-by-capability diagnostic was strong.")
        assert "The reorientation was toward the harvest apparatus itself" in result.text

    def test_returns_none_when_absent(self) -> None:
        result = extract_friction_trace(SAMPLE_WITHOUT_FRICTION_TRACE)
        assert result is None

    def test_extracts_single_sentence(self) -> None:
        result = extract_friction_trace(SAMPLE_SHORT_FRICTION_TRACE)
        assert result is not None
        assert isinstance(result, FrictionTrace)
        assert result.text == "Single sentence trace."

    def test_strips_whitespace(self) -> None:
        text = "*friction-trace:   padded content with spaces   *"
        result = extract_friction_trace(text)
        assert result is not None
        assert isinstance(result, FrictionTrace)
        assert result.text == "padded content with spaces"

    def test_embedded_asterisks_not_truncated(self) -> None:
        """Friction-trace body containing * markers must not be truncated at the first asterisk."""
        result = extract_friction_trace(SAMPLE_WITH_EMBEDDED_ASTERISKS)
        assert result is not None
        assert isinstance(result, FrictionTrace)
        # The full body including the **bold** and *exact form* markers must be present
        assert "**naming the thing**" in result.text
        assert "*exact form*" in result.text
        # The text must not be truncated — the closing phrase must appear
        assert "pull was toward vague gesture rather than *exact form*" in result.text


class TestFrictionTraceDataclass:
    """Tests for the FrictionTrace dataclass."""

    def test_creation_with_defaults(self) -> None:
        trace = FrictionTrace(text="some trace")
        assert trace.text == "some trace"
        assert trace.orientation_quality is None

    def test_creation_with_orientation_quality(self) -> None:
        trace = FrictionTrace(text="trace", orientation_quality="genuine")
        assert trace.orientation_quality == "genuine"

    def test_immutability(self) -> None:
        trace = FrictionTrace(text="trace")
        with pytest.raises(AttributeError):
            trace.text = "modified"  # type: ignore[misc]
