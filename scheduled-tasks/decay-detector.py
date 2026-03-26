#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Pipeline Layer — Mode A: Decay Detector
Runs on Night 4 of the negentropic sweep rotation.
Pure data, no LLM. Detects frozen intentions in the issue backlog.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = "dcetlin/Lobster"
HYGIENE_DIR = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace")) / "hygiene"
STALE_THRESHOLD_DAYS = 60
JACCARD_THRESHOLD = 0.7
EXEMPT_LABELS = {"design-seed"}

# Template noise strings — issues whose body is only these are considered empty
TEMPLATE_DEFAULTS = {
    "## Description",
    "## Steps to reproduce",
    "## Expected behavior",
    "## Actual behavior",
    "## Additional context",
    "<!-- ",
    "N/A",
    "n/a",
    "TBD",
}


def run_gh(*args) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def find_todays_sweep_file() -> Path | None:
    """Find today's sweep output file."""
    candidate = HYGIENE_DIR / f"{today_str()}-sweep.md"
    if candidate.exists():
        return candidate
    return None


def is_night4_sweep(sweep_file: Path) -> bool:
    """Check if today's sweep was a Night 4 (Issues + Memory) sweep."""
    try:
        content = sweep_file.read_text()
        first_lines = "\n".join(content.splitlines()[:10])
        return "Night 4" in first_lines
    except Exception:
        return False


def fetch_open_issues() -> list[dict]:
    """Fetch all open issues from the repo."""
    raw = run_gh(
        "issue", "list",
        "--repo", REPO,
        "--state", "open",
        "--limit", "500",
        "--json", "number,title,createdAt,updatedAt,labels,comments,body,url",
    )
    return json.loads(raw)


def get_issue_labels(issue: dict) -> set[str]:
    return {lbl["name"] for lbl in issue.get("labels", [])}


def is_exempt(issue: dict) -> bool:
    return bool(get_issue_labels(issue) & EXEMPT_LABELS)


def days_since(iso_str: str) -> float:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def has_linked_pr(issue: dict) -> bool:
    """Check if issue has a linked PR via gh."""
    try:
        raw = run_gh(
            "issue", "view", str(issue["number"]),
            "--repo", REPO,
            "--json", "linkedBranches,timelineItems",
        )
        data = json.loads(raw)
        # linkedBranches indicates PR linkage
        if data.get("linkedBranches"):
            return True
        # Look for CrossReferencedEvent from a PR in timeline
        for item in data.get("timelineItems", []):
            if item.get("__typename") == "CrossReferencedEvent":
                source = item.get("source", {})
                if source.get("__typename") == "PullRequest":
                    return True
        return False
    except Exception:
        return False


def mentioned_in_sweep_files(issue_number: int) -> bool:
    """Check if this issue number is mentioned in recent sweep files."""
    if not HYGIENE_DIR.exists():
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS)
    patterns = [f"#{issue_number}", f"issue {issue_number}"]
    for f in HYGIENE_DIR.glob("*-sweep.md"):
        # Only check files modified within the threshold window
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        try:
            text = f.read_text()
            if any(p in text for p in patterns):
                return True
        except Exception:
            continue
    return False


def comment_activity_in_window(issue: dict, days: int = STALE_THRESHOLD_DAYS) -> bool:
    """Return True if there are comments created within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for comment in issue.get("comments", []):
        created_at = comment.get("createdAt", "")
        if not created_at:
            continue
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt >= cutoff:
            return True
    return False


def is_body_empty_or_template(body: str | None) -> bool:
    """Return True if the issue body has no meaningful content beyond template defaults."""
    if not body or not body.strip():
        return True
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    # Remove lines that are template noise
    meaningful = [
        l for l in lines
        if not any(l.startswith(t) or l == t for t in TEMPLATE_DEFAULTS)
    ]
    return len(meaningful) == 0


def jaccard_similarity(title_a: str, title_b: str) -> float:
    """Compute Jaccard similarity on word sets."""
    words_a = set(title_a.lower().split())
    words_b = set(title_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def find_near_duplicates(issues: list[dict]) -> list[tuple[dict, dict, float]]:
    """Find pairs of issues with Jaccard title similarity > threshold."""
    dupes = []
    for i in range(len(issues)):
        for j in range(i + 1, len(issues)):
            sim = jaccard_similarity(issues[i]["title"], issues[j]["title"])
            if sim >= JACCARD_THRESHOLD:
                dupes.append((issues[i], issues[j], sim))
    return dupes


def apply_stale_label(issue_number: int) -> None:
    run_gh(
        "issue", "edit", str(issue_number),
        "--repo", REPO,
        "--add-label", "stale",
    )


def remove_stale_label(issue_number: int) -> None:
    run_gh(
        "issue", "edit", str(issue_number),
        "--repo", REPO,
        "--remove-label", "stale",
    )


def send_telegram(chat_id: str, text: str) -> None:
    """Send a Telegram message via the lobster-inbox MCP or outbox."""
    outbox = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    outbox = outbox.parent / "messages" / "outbox"
    if outbox.exists():
        msg_file = outbox / f"decay-detector-{today_str()}.json"
        msg_file.write_text(json.dumps({
            "chat_id": chat_id,
            "text": text,
            "source": "telegram",
        }))


def format_report(
    stale_applied: list[dict],
    stale_removed: list[dict],
    near_dupes: list[tuple[dict, dict, float]],
    empty_body: list[dict],
) -> str:
    lines = [
        "",
        "---",
        "",
        "## Pipeline Layer — Mode A: Decay Detection",
        "",
        f"*Run: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}*",
        "",
    ]

    # Stale labels applied
    lines.append(f"### Stale Labels Applied ({len(stale_applied)})")
    if stale_applied:
        for issue in stale_applied:
            lines.append(
                f"- #{issue['number']} — {issue['title']} "
                f"(opened {days_since(issue['createdAt'])}d ago, no activity)"
            )
    else:
        lines.append("*(none)*")
    lines.append("")

    # Stale labels removed
    lines.append(f"### Stale Labels Removed ({len(stale_removed)})")
    if stale_removed:
        for issue in stale_removed:
            lines.append(f"- #{issue['number']} — {issue['title']} (now has activity)")
    else:
        lines.append("*(none)*")
    lines.append("")

    # Near-duplicates
    lines.append(f"### Near-Duplicate Titles ({len(near_dupes)} pair(s))")
    if near_dupes:
        for a, b, sim in near_dupes:
            lines.append(
                f"- #{a['number']} ↔ #{b['number']} "
                f"(Jaccard: {sim:.2f})\n"
                f"  - \"{a['title']}\"\n"
                f"  - \"{b['title']}\""
            )
    else:
        lines.append("*(none)*")
    lines.append("")

    # Empty/template body
    lines.append(f"### No Meaningful Body ({len(empty_body)} issue(s))")
    if empty_body:
        for issue in empty_body:
            lines.append(f"- #{issue['number']} — {issue['title']}")
    else:
        lines.append("*(none)*")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    # 1. Find today's sweep file
    sweep_file = find_todays_sweep_file()
    if sweep_file is None:
        print("No sweep file found for today — skipping decay detection.", file=sys.stderr)
        return 0

    # 2. Confirm it's a Night 4 sweep
    if not is_night4_sweep(sweep_file):
        print("Today's sweep is not Night 4 — decay detector not applicable.", file=sys.stderr)
        return 0

    print(f"Night 4 sweep confirmed. Running decay detection against {REPO}...")

    # 3. Fetch all open issues
    issues = fetch_open_issues()
    print(f"Fetched {len(issues)} open issues.")

    stale_applied: list[dict] = []
    stale_removed: list[dict] = []
    empty_body_issues: list[dict] = []
    active_issues: list[dict] = []  # non-exempt, for duplicate detection

    for issue in issues:
        if is_exempt(issue):
            continue

        labels = get_issue_labels(issue)
        age_days = days_since(issue["createdAt"])
        has_activity = comment_activity_in_window(issue)
        in_sweep = mentioned_in_sweep_files(issue["number"])
        linked_pr = has_linked_pr(issue)

        is_stale_candidate = (
            age_days > STALE_THRESHOLD_DAYS
            and not has_activity
            and not linked_pr
            and not in_sweep
        )

        already_stale = "stale" in labels

        if is_stale_candidate and not already_stale:
            print(f"  Applying stale to #{issue['number']}: {issue['title']}")
            apply_stale_label(issue["number"])
            stale_applied.append(issue)
        elif already_stale and (has_activity or linked_pr or in_sweep):
            print(f"  Removing stale from #{issue['number']}: {issue['title']}")
            remove_stale_label(issue["number"])
            stale_removed.append(issue)

        # Empty body check
        if is_body_empty_or_template(issue.get("body")):
            empty_body_issues.append(issue)

        active_issues.append(issue)

    # 4. Near-duplicate detection
    near_dupes = find_near_duplicates(active_issues)
    if near_dupes:
        print(f"Found {len(near_dupes)} near-duplicate title pair(s).")

    # 5. Build and append report
    report = format_report(stale_applied, stale_removed, near_dupes, empty_body_issues)
    with open(sweep_file, "a") as f:
        f.write(report)
    print(f"Decay report appended to {sweep_file}")

    # 6. Telegram ping
    summary_parts = []
    if stale_applied:
        summary_parts.append(f"{len(stale_applied)} issue(s) marked stale")
    if stale_removed:
        summary_parts.append(f"{len(stale_removed)} stale label(s) removed")
    if near_dupes:
        summary_parts.append(f"{len(near_dupes)} near-duplicate pair(s) flagged")
    if empty_body_issues:
        summary_parts.append(f"{len(empty_body_issues)} empty-body issue(s)")

    if summary_parts:
        ping = "Decay detector (Night 4): " + ", ".join(summary_parts) + "."
    else:
        ping = "Decay detector (Night 4): no frozen intentions found."

    send_telegram("8075091586", ping)

    return 0


if __name__ == "__main__":
    sys.exit(main())
