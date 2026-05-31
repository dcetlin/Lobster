"""
Unit tests for src/harvest/cultivator.py

Tests verify behavior (what the system does) not mechanism (how lines of code produce it).
Named after the behavior under test, not the function being called.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.harvest.cultivator import (
    CultivatorConfig,
    CultivatorInput,
    PearlCandidate,
    SeedCandidate,
    classify_item,
    parse_session_output,
    route_pearl,
    route_seed,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_pending_dir(tmp_path: Path) -> Path:
    d = tmp_path / "pending-bootup-candidates"
    d.mkdir()
    return d


@pytest.fixture
def dry_run_config(tmp_pending_dir: Path) -> CultivatorConfig:
    return CultivatorConfig(
        repo="SiderealPress/lobster",
        pending_bootup_dir=tmp_pending_dir,
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# classify_item — pearl signals
# ---------------------------------------------------------------------------

class TestClassifyItemPearlSignals:
    def test_recognition_prefix_is_pearl(self):
        assert classify_item("Recognition: the accountability chain terminates at phase coherence") == "pearl"

    def test_i_now_see_is_pearl(self):
        assert classify_item("I now see that attunement is the governing form, not enforcement") == "pearl"

    def test_this_settles_is_pearl(self):
        assert classify_item("This settles the question of where the accountability chain terminates") == "pearl"

    def test_phenomenological_vocabulary_without_action_verb_is_pearl(self):
        # "presencing" is in _PHILOSOPHICAL_TERMS, no action verb
        assert classify_item("The presencing of the system's own phase coherence is the termination point") == "pearl"

    def test_what_i_understand_now_is_pearl(self):
        assert classify_item("What I understand now: frontier sensitivity is cultivated, not encoded") == "pearl"


# ---------------------------------------------------------------------------
# classify_item — seed signals
# ---------------------------------------------------------------------------

class TestClassifyItemSeedSignals:
    def test_implement_verb_is_seed(self):
        assert classify_item("Implement cross-layer coherence signal in vision.yaml") == "seed"

    def test_build_verb_is_seed(self):
        assert classify_item("Build the coherence probe at reply time") == "seed"

    def test_fix_verb_is_seed(self):
        assert classify_item("Fix the failing tests in test_wos_execute.py") == "seed"

    def test_add_verb_is_seed(self):
        assert classify_item("Add design-seed label to newly filed issues") == "seed"

    def test_create_verb_is_seed(self):
        assert classify_item("Create a pending bootup candidate file for each pearl") == "seed"

    def test_investigate_verb_is_seed(self):
        assert classify_item("Investigate why the oracle audit is silent on register mismatches") == "seed"


# ---------------------------------------------------------------------------
# classify_item — default-to-seed on ambiguous
# ---------------------------------------------------------------------------

class TestClassifyItemDefaultsToSeed:
    def test_ambiguous_text_defaults_to_seed(self):
        # No clear pearl or seed signal — default is seed
        result = classify_item("Consider the accountability architecture")
        assert result == "seed"

    def test_empty_item_defaults_to_seed(self):
        assert classify_item("") == "seed"

    def test_generic_observation_defaults_to_seed(self):
        # No action verb, no pearl signal — defaults to seed
        result = classify_item("The system has four layers")
        assert result == "seed"


# ---------------------------------------------------------------------------
# parse_session_output — Path A (YAML block present)
# ---------------------------------------------------------------------------

class TestParseSessionOutputPathA:
    def test_yaml_block_issues_become_seeds(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            Some text.

            ```yaml
            action_seeds:
              issues:
                - title: "Design: cross-layer coherence signal"
                  body: "The issue body text"
                  labels: ["design", "enhancement"]
              bootup_candidates: []
              memory_observations: []
            ```
        """))
        result = parse_session_output(md)
        assert len(result.seeds) == 1
        assert result.seeds[0].title == "Design: cross-layer coherence signal"
        assert "design" in result.seeds[0].labels

    def test_yaml_block_bootup_candidates_become_pearls(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            ```yaml
            action_seeds:
              issues: []
              bootup_candidates:
                - context: "user.base.bootup"
                  text: "Accountability terminates at phase coherence"
                  rationale: "Tested across four sessions"
              memory_observations: []
            ```
        """))
        result = parse_session_output(md)
        assert len(result.pearls) == 1
        assert result.pearls[0].pearl_type == "bootup"
        assert "Accountability terminates" in result.pearls[0].text

    def test_yaml_block_memory_observations_passed_through(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            ```yaml
            action_seeds:
              issues: []
              bootup_candidates: []
              memory_observations:
                - text: "Pattern: every structural solution generates the same problem one level up"
                  type: "pattern_observation"
            ```
        """))
        result = parse_session_output(md)
        assert len(result.memory_entries) == 1
        assert "Pattern:" in result.memory_entries[0].text

    def test_yaml_block_returns_cultivator_input_with_correct_source_path(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            ```yaml
            action_seeds:
              issues: []
              bootup_candidates: []
              memory_observations: []
            ```
        """))
        result = parse_session_output(md)
        assert isinstance(result, CultivatorInput)
        assert result.source_path == md


# ---------------------------------------------------------------------------
# parse_session_output — Path B (prose sections, no YAML block)
# ---------------------------------------------------------------------------

class TestParseSessionOutputPathB:
    def test_insights_section_items_extracted_as_pearls(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            ## Insights

            - Accountability terminates at phase coherence, not enforcement
            - The fundamental frequency is a capacity, not a standard
        """))
        result = parse_session_output(md)
        assert len(result.pearls) >= 1
        texts = [p.text for p in result.pearls]
        assert any("Accountability" in t for t in texts)

    def test_action_items_section_extracted_as_seeds(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            ## Action items

            - Implement the cross-layer coherence signal
            - Build a pending bootup candidate writer
        """))
        result = parse_session_output(md)
        assert len(result.seeds) >= 1
        titles = [s.title for s in result.seeds]
        assert any("Implement" in t for t in titles)

    def test_recognition_paragraph_opener_extracted_as_pearl(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            Recognition: the accountability chain cannot terminate in a mechanism.

            Some other paragraph.
        """))
        result = parse_session_output(md)
        assert len(result.pearls) >= 1
        assert any("Recognition" in p.text for p in result.pearls)

    def test_i_now_see_paragraph_extracted_as_pearl(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text(textwrap.dedent("""\
            # Session

            I now see that every structural solution generates the same problem one level up.
        """))
        result = parse_session_output(md)
        assert len(result.pearls) >= 1

    def test_no_classifiable_content_returns_empty_input(self, tmp_path: Path):
        md = tmp_path / "session.md"
        md.write_text("# Session\n\nSome generic text with no structure.\n")
        result = parse_session_output(md)
        assert isinstance(result, CultivatorInput)
        assert result.source_path == md


# ---------------------------------------------------------------------------
# route_pearl — write-path routing (dry_run=True)
# ---------------------------------------------------------------------------

class TestRoutePearlDryRun:
    def test_bootup_pearl_returns_pending_file_path(self, dry_run_config: CultivatorConfig):
        candidate = PearlCandidate(
            text="Accountability terminates at phase coherence",
            pearl_type="bootup",
        )
        result = route_pearl(candidate, dry_run_config, dry_run=True)
        assert result.action == "pending_file"
        assert result.output_path is not None
        assert "pending-bootup-candidates" in str(result.output_path)

    def test_insight_pearl_returns_pending_file_path(self, dry_run_config: CultivatorConfig):
        candidate = PearlCandidate(
            text="The fundamental frequency is a capacity for attending, not a standard",
            pearl_type="insight",
        )
        result = route_pearl(candidate, dry_run_config, dry_run=True)
        assert result.action == "pending_file"
        assert result.output_path is not None

    def test_frontier_pearl_returns_frontier_action(self, dry_run_config: CultivatorConfig):
        candidate = PearlCandidate(
            text="The presencing of frontier domains requires active cultivation",
            pearl_type="frontier",
        )
        result = route_pearl(candidate, dry_run_config, dry_run=True)
        assert result.action == "frontier"
        assert result.output_path is not None

    def test_dry_run_does_not_write_files(
        self, dry_run_config: CultivatorConfig, tmp_pending_dir: Path
    ):
        candidate = PearlCandidate(
            text="I now see that attunement is the governing form",
            pearl_type="insight",
        )
        route_pearl(candidate, dry_run_config, dry_run=True)
        # In dry_run mode, no files should be written
        written_files = list(tmp_pending_dir.iterdir())
        assert len(written_files) == 0


# ---------------------------------------------------------------------------
# route_seed — seed issue spec construction (dry_run=True, no gh call)
# ---------------------------------------------------------------------------

class TestRouteSeedDryRun:
    def test_seed_spec_includes_design_seed_label(self, dry_run_config: CultivatorConfig):
        candidate = SeedCandidate(
            title="Implement cross-layer coherence signal",
            body="Design: what artifact encodes whether orientation layers are in phase?",
        )
        result = route_seed(candidate, dry_run_config, dry_run=True)
        assert "design-seed" in result.issue_spec["labels"]

    def test_seed_spec_includes_source_in_body(self, dry_run_config: CultivatorConfig):
        candidate = SeedCandidate(
            title="Build coherence probe at reply time",
            body="The probe should test alignment before sending any reply.",
        )
        result = route_seed(candidate, dry_run_config, dry_run=True)
        assert "Source:" in result.issue_spec["body"]

    def test_seed_spec_preserves_candidate_labels(self, dry_run_config: CultivatorConfig):
        candidate = SeedCandidate(
            title="Add vision_ref gate enforcement",
            body="The gate should block non-aligned routing decisions.",
            labels=("design", "vision-object"),
        )
        result = route_seed(candidate, dry_run_config, dry_run=True)
        assert "design" in result.issue_spec["labels"]
        assert "vision-object" in result.issue_spec["labels"]
        assert "design-seed" in result.issue_spec["labels"]  # always added

    def test_seed_spec_title_matches_candidate(self, dry_run_config: CultivatorConfig):
        candidate = SeedCandidate(
            title="Investigate oracle accountability gap",
            body="Why does the oracle audit remain silent on register mismatches?",
        )
        result = route_seed(candidate, dry_run_config, dry_run=True)
        assert result.issue_spec["title"] == "Investigate oracle accountability gap"

    def test_dry_run_does_not_call_gh(
        self, dry_run_config: CultivatorConfig, monkeypatch: pytest.MonkeyPatch
    ):
        """route_seed in dry_run mode must not call gh or any subprocess."""
        import subprocess as sp
        calls: list = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("subprocess.run should not be called in dry_run mode")

        monkeypatch.setattr(sp, "run", fake_run)

        candidate = SeedCandidate(
            title="Create structural enforcement for commitment devices",
            body="Structural enforcement rather than advisory context.",
        )
        route_seed(candidate, dry_run_config, dry_run=True)
        assert len(calls) == 0

    def test_dry_run_returns_none_url_and_number(self, dry_run_config: CultivatorConfig):
        candidate = SeedCandidate(
            title="Design: proprioceptive pulse artifact",
            body="Makes phase-reference signal continuously visible.",
        )
        result = route_seed(candidate, dry_run_config, dry_run=True)
        assert result.issue_url is None
        assert result.issue_number is None


# ---------------------------------------------------------------------------
# End-to-end integration: real philosophy session file with --dry-run
# ---------------------------------------------------------------------------

class TestEndToEndRealSessionFile:
    SESSIONS_DIR = Path(__file__).parent.parent.parent.parent / "philosophy" / "sessions"

    def _find_any_session(self) -> Path | None:
        if not self.SESSIONS_DIR.exists():
            return None
        for f in sorted(self.SESSIONS_DIR.iterdir()):
            if f.suffix == ".md":
                return f
        return None

    def test_dry_run_on_real_session_no_exceptions(
        self, tmp_pending_dir: Path
    ):
        session_file = self._find_any_session()
        if session_file is None:
            pytest.skip("No philosophy session files found in philosophy/sessions/")

        config = CultivatorConfig(
            repo="SiderealPress/lobster",
            pending_bootup_dir=tmp_pending_dir,
            dry_run=True,
        )

        # Must not raise
        pearl_results, seed_results, memory_entries, errors = __import__(
            "src.harvest.cultivator", fromlist=["cultivate"]
        ).cultivate(session_file, config)

        # No errors expected in dry-run
        assert not errors, f"Unexpected errors: {errors}"

    def test_dry_run_on_real_session_returns_cultivator_input(
        self, tmp_pending_dir: Path
    ):
        session_file = self._find_any_session()
        if session_file is None:
            pytest.skip("No philosophy session files found in philosophy/sessions/")

        result = parse_session_output(session_file)
        assert isinstance(result, CultivatorInput)
        assert result.source_path == session_file.resolve()

    def test_yaml_block_session_produces_seeds_and_memory(
        self, tmp_pending_dir: Path
    ):
        """The 2026-04-08-2000 session has a YAML block with issues and memory_observations."""
        sessions_dir = self.SESSIONS_DIR
        if not sessions_dir.exists():
            pytest.skip("No philosophy sessions directory found")

        session_file = sessions_dir / "2026-04-08-2000-philosophy-explore.md"
        if not session_file.exists():
            pytest.skip("Target session file not found")

        result = parse_session_output(session_file)
        # This session has 2 issues → 2 seeds, 0 bootup candidates, 3 memory observations
        assert len(result.seeds) == 2
        assert len(result.memory_entries) == 3
        assert len(result.pearls) == 0  # no bootup_candidates in this file
