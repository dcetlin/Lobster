#!/usr/bin/env python3
"""
Regenerate the external-agent-facing discoverability artifacts for the agent
channel protocol (source="local-claude") from the single canonical schema
module, src/protocol/agent_channel_schema.py.

This exists so `lobster-chat --schema`/`--help` and docs/reference/agent-channel-schema.md
can never drift from each other or from the protocol module they describe —
they are text baked from the same data, not three hand-maintained copies.

Generates:
  1. docs/reference/agent-channel-schema.md — fully generated Markdown doc.
  2. scripts/lobster-chat.py — replaces the content between the
     "BEGIN GENERATED SCHEMA" / "END GENERATED SCHEMA" markers with the
     current schema (as a JSON string constant) and CLI help epilog.

Usage:
    uv run scripts/generate_agent_channel_docs.py            # write the artifacts
    uv run scripts/generate_agent_channel_docs.py --check     # verify no drift; exit 1 if stale

Run from anywhere within the repo checkout — paths are resolved relative to
this script's location, not the current working directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.protocol.agent_channel_schema import (  # noqa: E402
    json_schema,
    render_cli_help_epilog,
    render_markdown,
)

DOCS_SCHEMA_PATH = REPO_ROOT / "docs" / "reference" / "agent-channel-schema.md"
LOBSTER_CHAT_PATH = REPO_ROOT / "scripts" / "lobster-chat.py"

_BEGIN_MARKER = "# === BEGIN GENERATED SCHEMA (see scripts/generate_agent_channel_docs.py) ==="
_END_MARKER = "# === END GENERATED SCHEMA ==="
_BLOCK_PATTERN = re.compile(
    re.escape(_BEGIN_MARKER) + r".*?" + re.escape(_END_MARKER),
    re.DOTALL,
)


def _generated_block() -> str:
    schema_json = json.dumps(json_schema(), indent=2, sort_keys=False)
    epilog = render_cli_help_epilog()
    lines = [
        _BEGIN_MARKER,
        "# Do not hand-edit this block. It is generated from the single canonical",
        "# schema module, src/protocol/agent_channel_schema.py, so that --schema and",
        "# --help can never drift from the server-side protocol they describe.",
        "# Regenerate: uv run scripts/generate_agent_channel_docs.py",
        "# Verify in sync: uv run scripts/generate_agent_channel_docs.py --check",
        f"_SCHEMA_JSON = {schema_json!r}",
        "",
        f"_HELP_EPILOG = {epilog!r}",
        _END_MARKER,
    ]
    return "\n".join(lines)


def _render_lobster_chat(current: str) -> str:
    if not _BLOCK_PATTERN.search(current):
        raise SystemExit(
            f"generate_agent_channel_docs: could not find the generated-schema markers "
            f"in {LOBSTER_CHAT_PATH} — has the file been restructured? Expected to find "
            f"'{_BEGIN_MARKER}' ... '{_END_MARKER}'."
        )
    # Use a callable replacement, not a plain string: re.sub processes
    # backslash escapes (\n, \1, ...) in a *string* replacement, which would
    # mangle the embedded repr()'d JSON (full of literal "\n" and "\'" text).
    # A callable's return value is substituted verbatim.
    return _BLOCK_PATTERN.sub(lambda _m: _generated_block(), current, count=1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--check",
        action="store_true",
        help="Don't write anything — exit 1 if the generated artifacts are stale relative to the schema module.",
    )
    args = p.parse_args()

    docs_wanted = render_markdown()
    lobster_chat_current = LOBSTER_CHAT_PATH.read_text()
    lobster_chat_wanted = _render_lobster_chat(lobster_chat_current)

    docs_current = DOCS_SCHEMA_PATH.read_text() if DOCS_SCHEMA_PATH.exists() else None
    stale = []
    if docs_current != docs_wanted:
        stale.append(str(DOCS_SCHEMA_PATH))
    if lobster_chat_current != lobster_chat_wanted:
        stale.append(str(LOBSTER_CHAT_PATH))

    if args.check:
        if stale:
            print(
                "generate_agent_channel_docs --check: STALE — out of sync with "
                "src/protocol/agent_channel_schema.py:\n  " + "\n  ".join(stale) +
                "\nRun `uv run scripts/generate_agent_channel_docs.py` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print("generate_agent_channel_docs --check: OK — all generated artifacts in sync.")
        return 0

    DOCS_SCHEMA_PATH.write_text(docs_wanted)
    LOBSTER_CHAT_PATH.write_text(lobster_chat_wanted)
    if stale:
        print("Regenerated (were stale):\n  " + "\n  ".join(stale))
    else:
        print("Regenerated (already in sync, wrote anyway).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
