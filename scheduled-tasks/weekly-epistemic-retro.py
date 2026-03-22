#!/usr/bin/env python3
"""
Weekly Epistemic Retro — Lobster Scheduled Job
===============================================

Pulls the past 7 days of conversation history, evaluates against Dan's
epistemic principles, writes a structured artifact to
~/lobster-user-config/memory/retros/, and delivers a distilled Telegram
summary.

Design constraints (from Issue #2):
- Err toward understatement. 3 real observations beats 12 generic ones.
- No automated writes to orientation documents.
- Use Dan's register, not AI-normalized language.
- Distinguish: response pattern observations / interaction dynamic observations /
  candidates for lessons or memory updates.

Run standalone:
    uv run ~/lobster/scheduled-tasks/weekly-epistemic-retro.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure data helpers
# ---------------------------------------------------------------------------

def resolve_paths() -> dict:
    """Return all relevant filesystem paths as an immutable dict."""
    home = Path.home()
    return {
        "epistemic_md": home / "lobster-user-config" / "agents" / "user.epistemic.md",
        "bootup_md": home / "lobster-user-config" / "agents" / "user.base.bootup.md",
        "retros_dir": home / "lobster-user-config" / "memory" / "retros",
    }


def load_file(path: Path) -> str:
    """Read a file and return its contents, or an empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def week_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")


def artifact_path(retros_dir: Path) -> Path:
    return retros_dir / f"retro-{today_iso()}.md"


# ---------------------------------------------------------------------------
# MCP call via claude -p
# ---------------------------------------------------------------------------

def call_mcp(mcp_call_description: str) -> str:
    """
    Invoke a one-shot Claude subagent that makes a single MCP call and returns
    the result as plain text.  This lets the Python script remain pure — all
    Lobster I/O goes through Claude's tool layer.
    """
    result = subprocess.run(
        [
            "claude", "-p", mcp_call_description,
            "--dangerously-skip-permissions",
            "--max-turns", "5",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def fetch_conversation_history() -> str:
    """Fetch the last 7 days of conversation history via MCP."""
    prompt = (
        "Call get_conversation_history with limit=100 and return the raw JSON result "
        "exactly as the tool returns it. No commentary, no formatting — just the JSON."
    )
    return call_mcp(prompt)


# ---------------------------------------------------------------------------
# Retro generation via Claude subagent
# ---------------------------------------------------------------------------

RETRO_PROMPT_TEMPLATE = """
You are conducting a weekly epistemic retro for Lobster's interactions with Dan.

## Your task

Review the conversation history below (past 7 days) against Dan's epistemic
principles. Produce a structured retro artifact. Follow the design constraints
exactly.

---

## Design constraints

- Err toward understatement. 3 real observations beats 12 generic ones.
  This is Filter 3: observable behavioral signatures only.
- No proposals to change CLAUDE.md or any orientation document.
  Observe and surface. Dan decides.
- Use Dan's register, not AI-normalized language. His vocabulary:
  basin-capture, attractor, minority-basin structure, entrainment,
  genuine vs. performed attunement, trajectory correction, semantic mirroring,
  fundamental frequency, phase alignment, narrow lightcone / wide contemplative.
- The Telegram summary is 3–5 points maximum. A distillation, not a report dump.

---

## Evaluation criteria (from user.epistemic.md and user.base.bootup.md)

**Sycophancy / basin-capture signals (look for these as problems):**
- Responses that accommodated Dan's framing without probing what it was pointing toward
- Pushback treated as error or preference to manage, not as possible minority-basin signal
- Responses that formed too easily — smoothness without resistance (fluency ≠ understanding)
- Generic openers or hand-holding requests when Dan's directive was clear
- Pattern surfacing that conflated observe / meaning / scope into one undifferentiated statement
- Outputs that would have been essentially the same without Dan's specific context

**Semantic mirroring / genuine attunement (look for these as positives):**
- Moments where Lobster genuinely worked from within Dan's frame
- Responses that probed pushback rather than accommodating or overriding it
- Trajectory corrections treated as navigation signals, not just information updates
- Outputs that would have failed to be generated without Dan's specific context
- Appropriate tentativeness in the right places (genuine minority-attractor work)

**Interaction dynamic observations:**
- Patterns in what Dan brought — recurring domains, modes (narrow lightcone vs. wide contemplative), types of requests
- Moments where the interaction dynamic itself is notable (not just the response quality)

**Candidates for lessons or memory updates:**
- Observations that appear more than once across different exchanges
- (Single-session signals should NOT be promoted — note only if you see 2+ instances)

---

## Required output format

Produce a structured markdown artifact with these sections:

```
# Weekly Epistemic Retro — {date}

## Response Pattern Observations
[Graded by confidence: High / Medium / Low. Each observation: quote the exchange
(from → to), state what you observe, state what it might mean — as separate sentences.]

## Interaction Dynamic Observations
[What you observe about the interaction dynamic this week — not individual responses
but the shape of the exchanges overall.]

## Candidates for Lessons or Memory Updates
[Only if 2+ instances confirm the same pattern. Each entry: pattern / evidence /
confidence. If nothing qualifies, write "Nothing confirmed this week."]

## Telegram Summary
[3–5 bullet points. Plain language. Dan's register. What is actually worth his attention.]
```

---

## Dan's epistemic principles (reference)

{epistemic_md}

---

## Dan's behavioral principles (reference)

{bootup_md}

---

## Conversation history (past 7 days)

{conversation_history}

---

Now write the retro artifact. Remember: 3 real observations beats 12 generic ones.
"""


def generate_retro(
    epistemic_md: str,
    bootup_md: str,
    conversation_history: str,
    date: str,
) -> str:
    """
    Run a Claude subagent to produce the retro artifact.
    Returns the artifact text.
    """
    prompt = RETRO_PROMPT_TEMPLATE.format(
        date=date,
        epistemic_md=epistemic_md,
        bootup_md=bootup_md,
        conversation_history=conversation_history,
    )

    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", "3",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Extract Telegram summary from artifact
# ---------------------------------------------------------------------------

def extract_telegram_summary(artifact: str, date: str) -> str:
    """
    Pull the ## Telegram Summary section from the artifact.
    Falls back to a minimal message if extraction fails.
    """
    marker = "## Telegram Summary"
    if marker in artifact:
        after = artifact.split(marker, 1)[1].strip()
        # Take everything up to the next ## section (if any)
        lines = []
        for line in after.splitlines():
            if line.startswith("## ") and lines:
                break
            lines.append(line)
        summary_body = "\n".join(lines).strip()
        return f"Weekly epistemic retro — {date}\n\n{summary_body}"
    return (
        f"Weekly epistemic retro — {date}\n\n"
        "Retro completed. Full artifact at "
        f"~/lobster-user-config/memory/retros/retro-{date}.md"
    )


# ---------------------------------------------------------------------------
# Write artifact to disk
# ---------------------------------------------------------------------------

def write_artifact(retros_dir: Path, path: Path, content: str) -> None:
    """Ensure the retros directory exists and write the artifact."""
    retros_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Deliver Telegram summary and write task output
# ---------------------------------------------------------------------------

def deliver_and_log(summary: str, artifact_path_str: str) -> None:
    """
    Send the Telegram summary and write task output via a Claude subagent.
    Kept as a single MCP call to avoid partial delivery.
    """
    chat_id = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))
    prompt = f"""
You are delivering a weekly epistemic retro summary. Make exactly these two calls:

1. Call send_reply with:
   - chat_id: {chat_id}
   - source: "telegram"
   - text: {json.dumps(summary)}

2. Call write_task_output with:
   - job_name: "weekly-epistemic-retro"
   - output: "Retro completed. Artifact written to {artifact_path_str}. Summary delivered to Telegram."
   - status: "success"

Make both calls, then stop. No commentary.
"""
    subprocess.run(
        [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--max-turns", "5",
        ],
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Main pipeline — pure composition of the steps above
# ---------------------------------------------------------------------------

def run() -> int:
    """
    Execute the weekly epistemic retro pipeline.
    Returns exit code: 0 for success, 1 for failure.
    """
    paths = resolve_paths()
    date = today_iso()

    print(f"[{date}] Starting weekly epistemic retro")

    # Load reference documents
    epistemic_md = load_file(paths["epistemic_md"])
    bootup_md = load_file(paths["bootup_md"])

    if not epistemic_md:
        print("WARNING: user.epistemic.md not found — retro will proceed with empty principles")
    if not bootup_md:
        print("WARNING: user.base.bootup.md not found — retro will proceed with empty behavioral context")

    # Fetch conversation history
    print("Fetching conversation history (past 7 days)...")
    conversation_history = fetch_conversation_history()
    if not conversation_history:
        print("ERROR: Could not fetch conversation history")
        return 1

    # Generate retro artifact
    print("Generating retro artifact...")
    artifact = generate_retro(epistemic_md, bootup_md, conversation_history, date)
    if not artifact:
        print("ERROR: Retro generation returned empty output")
        return 1

    # Write artifact to disk
    art_path = artifact_path(paths["retros_dir"])
    write_artifact(paths["retros_dir"], art_path, artifact)
    print(f"Artifact written to: {art_path}")

    # Extract summary and deliver
    summary = extract_telegram_summary(artifact, date)
    print("Delivering Telegram summary...")
    deliver_and_log(summary, str(art_path))

    print(f"[{date}] Weekly epistemic retro complete")
    return 0


if __name__ == "__main__":
    sys.exit(run())
