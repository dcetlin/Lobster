"""
Tests for src/protocol/agent_channel_schema.py — the single canonical schema
module for the agent channel protocol (source="local-claude") — and for the
discoverability artifacts generated from it:

- scripts/generate_agent_channel_docs.py (the generator itself, in --check mode)
- scripts/lobster-chat.py --schema / --help (the embedded, generated block)
- docs/reference/agent-channel-schema.md (the generated doc)

These tests exist to catch drift: if someone edits the schema module but
forgets to regenerate, or hand-edits one of the generated artifacts directly,
these should fail.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.protocol.agent_channel_schema import (  # noqa: E402
    ACK_ENVELOPE,
    FILE_LOCATIONS,
    INBOUND_ENVELOPE,
    PROTOCOL_VERSION,
    REPLY_ENVELOPE,
    REQUEST_ID_MAX_LEN,
    REQUEST_ID_PATTERN,
    SOURCE,
    json_schema,
    render_cli_help_epilog,
    render_markdown,
)


class TestSchemaModuleStructure:
    def test_source_is_local_claude(self):
        assert SOURCE == "local-claude"

    def test_protocol_version_is_a_string(self):
        assert isinstance(PROTOCOL_VERSION, str) and PROTOCOL_VERSION

    def test_request_id_pattern_matches_known_valid_and_invalid_ids(self):
        pattern = re.compile(REQUEST_ID_PATTERN)
        for valid in ["1732900000-a1b2c3d4", "abc", "A_B-9"]:
            assert pattern.match(valid), f"expected {valid!r} to be valid"
        for invalid in ["../etc/passwd", "a/b", "a.json", "a b", ""]:
            assert not pattern.match(invalid), f"expected {invalid!r} to be invalid"

    def test_request_id_max_len_is_positive(self):
        assert REQUEST_ID_MAX_LEN > 0

    def test_request_id_pattern_rejects_max_len_boundary_overflow(self):
        # The pattern itself has no length cap — REQUEST_ID_MAX_LEN is
        # enforced separately by src/mcp/reliability.py's sanitize_request_id.
        # This just confirms a max-length string is otherwise pattern-valid,
        # so the length check is doing real work, not papering over a
        # pattern that would already reject it.
        pattern = re.compile(REQUEST_ID_PATTERN)
        assert pattern.match("a" * REQUEST_ID_MAX_LEN)

    def test_envelopes_declare_every_field_required_or_not(self):
        for envelope in (INBOUND_ENVELOPE, ACK_ENVELOPE, REPLY_ENVELOPE):
            for name, field in envelope.items():
                assert "required" in field, f"{name} missing 'required'"
                assert "type" in field, f"{name} missing 'type'"
                assert "description" in field and field["description"], f"{name} missing description"

    def test_inbound_envelope_source_field_is_const_local_claude(self):
        assert INBOUND_ENVELOPE["source"]["const"] == SOURCE

    def test_file_locations_cover_request_ack_reply(self):
        assert set(FILE_LOCATIONS.keys()) == {"request", "ack", "reply"}
        for loc in FILE_LOCATIONS.values():
            assert "<request_id>" in loc["path"]

    def test_agent_field_is_optional(self):
        # The "agent" field is a cosmetic, additive identity label (populated
        # by lobster-chat --agent) — unlike every other inbound field, it
        # must NOT be required, so existing callers that omit it keep working.
        assert INBOUND_ENVELOPE["agent"]["required"] is False


class TestJsonSchemaRendering:
    def test_json_schema_is_json_serializable(self):
        s = json_schema()
        json.dumps(s)  # must not raise

    def test_json_schema_top_level_keys(self):
        s = json_schema()
        assert s["source"] == SOURCE
        assert s["version"] == PROTOCOL_VERSION
        assert set(s["envelopes"].keys()) == {"request", "ack", "reply"}
        assert s["request_id_rules"]["pattern"] == REQUEST_ID_PATTERN
        assert s["request_id_rules"]["max_length"] == REQUEST_ID_MAX_LEN

    def test_json_schema_request_envelope_required_fields(self):
        s = json_schema()
        required = set(s["envelopes"]["request"]["required"])
        # Derived from the source of truth rather than hardcoded: as of this
        # change, "agent" is the one optional inbound field, so required
        # fields are no longer simply "all of them".
        expected_required = {name for name, f in INBOUND_ENVELOPE.items() if f.get("required")}
        assert required == expected_required
        assert "agent" not in required

    def test_addressing_covers_dan_and_agent(self):
        s = json_schema()
        assert "dan" in s["addressing"]
        assert "agent" in s["addressing"]
        assert "invariant" in s["addressing"]

    def test_error_and_ack_semantics_nonempty(self):
        s = json_schema()
        assert len(s["error_and_ack_semantics"]) >= 3


class TestMarkdownRendering:
    def test_render_markdown_mentions_generated_provenance(self):
        md = render_markdown()
        assert "GENERATED FILE" in md
        assert "src/protocol/agent_channel_schema.py" in md

    def test_render_markdown_documents_all_envelope_fields(self):
        md = render_markdown()
        for name in {**INBOUND_ENVELOPE, **ACK_ENVELOPE, **REPLY_ENVELOPE}:
            assert f"`{name}`" in md, f"field {name} not documented in generated markdown"

    def test_render_cli_help_epilog_is_short_and_nonempty(self):
        epilog = render_cli_help_epilog()
        assert epilog
        # Deliberately short — a first-time reader's --help, not the full doc.
        assert len(epilog) < 1500


class TestGeneratedArtifactsInSync:
    """Drift check: fails if the schema module changed but the generated
    artifacts (docs/reference/agent-channel-schema.md, the embedded block in
    scripts/lobster-chat.py) were not regenerated to match."""

    def test_generator_check_mode_reports_in_sync(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "generate_agent_channel_docs.py"), "--check"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            "Generated agent-channel artifacts are stale relative to "
            "src/protocol/agent_channel_schema.py. Run "
            "`uv run scripts/generate_agent_channel_docs.py` to regenerate.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


class TestLobsterChatCli:
    """Exercises the actual generated --schema/--help surface of lobster-chat.py."""

    CLI = REPO_ROOT / "scripts" / "lobster-chat.py"

    def test_schema_flag_prints_valid_json_matching_module(self):
        result = subprocess.run(
            [sys.executable, str(self.CLI), "--schema"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        printed = json.loads(result.stdout)
        assert printed == json_schema()

    def test_schema_flag_requires_no_host_and_no_ssh(self):
        # --schema must short-circuit before any --host validation or SSH call.
        result = subprocess.run(
            [sys.executable, str(self.CLI), "--schema"],
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": "/nonexistent"},  # ssh would fail to even exec here
        )
        assert result.returncode == 0
        json.loads(result.stdout)

    def test_help_flag_includes_generated_epilog(self):
        result = subprocess.run(
            [sys.executable, str(self.CLI), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "--schema" in result.stdout
        # A couple of load-bearing phrases from the generated epilog.
        assert "request_id" in result.stdout
        assert "Dan" in result.stdout

    def test_no_args_and_no_schema_errors_without_ssh(self):
        result = subprocess.run(
            [sys.executable, str(self.CLI)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0
        assert "text" in result.stderr.lower()


class TestReliabilitySharesCanonicalConstants:
    """The server-side validator (src/mcp/reliability.py) must import the
    request_id rules and source name from this module rather than redefine
    them, so the enforced value and the documented value can't drift."""

    def test_reliability_request_id_pattern_matches_schema(self):
        mcp_dir = str(REPO_ROOT / "src" / "mcp")
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import reliability  # noqa: E402

        assert reliability._REQUEST_ID_PATTERN.pattern == REQUEST_ID_PATTERN
        assert reliability._REQUEST_ID_MAX_LEN == REQUEST_ID_MAX_LEN
        assert reliability.AGENT_CHANNEL_SOURCE == SOURCE
