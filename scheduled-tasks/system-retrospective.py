#!/usr/bin/env python3
"""
System Retrospective — automated feedback loop observation layer.
================================================================

Runs weekly (Sundays 06:00 UTC) and on-demand. For each invocation:

1. Collects data for the lookback period (default: 7 days):
   - Git log: merged PRs and WOS-UoW footer stamps
   - WOS registry: completed UoWs grouped by outcome_category
   - Session files: ~/lobster-user-config/memory/canonical/sessions/
2. Computes metabolic ratios (pearl / seed / heat / shit).
3. Detects smell patterns against oracle/smell-patterns.yaml.
4. Checks golden pattern drift: encoded but unenforced patterns.
5. Writes ~/lobster-workspace/assessments/{date}-auto-retrospective.md.
6. Files GitHub issues for high-severity smells without open issues.
7. Sends Telegram escalation for smells detected 2+ consecutive runs.
8. Writes task output via write_task_output for the dispatcher.

Severity thresholds:
  high   → file GitHub issue immediately + add to output doc
  medium → add to output doc only
  repeat (2+ consecutive detections) → escalate to admin Telegram chat

Type C dispatch: cron-direct. No LLM round-trip. The jobs.json enabled
gate is checked at startup. All side effects are isolated to the final step.

Run standalone:
    uv run ~/lobster/scheduled-tasks/system-retrospective.py [--period-days N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml  # requires pyyaml

# ---------------------------------------------------------------------------
# Path setup — allow running as a script or via importlib (tests)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.utils.inbox_write import _inbox_dir, _task_outputs_dir, write_inbox_message  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("system-retrospective")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JOB_NAME: str = "system-retrospective"
REPO: str = "dcetlin/Lobster"

# Default lookback window
DEFAULT_PERIOD_DAYS: int = 7

# Maximum new issues to file per run (guard against issue floods)
MAX_ISSUES_PER_RUN: int = 3

# Severity levels
SEVERITY_HIGH: str = "high"
SEVERITY_MEDIUM: str = "medium"
SEVERITY_LOW: str = "low"

# Escalation threshold: number of consecutive detections before Telegram escalation
ESCALATION_THRESHOLD: int = 2

# Outcome categories
OUTCOME_PEARL: str = "pearl"
OUTCOME_SEED: str = "seed"
OUTCOME_HEAT: str = "heat"
OUTCOME_SHIT: str = "shit"

# Terminal UoW statuses for metabolic accounting
TERMINAL_STATUSES: tuple[str, ...] = ("done", "failed", "expired", "cancelled")

ADMIN_CHAT_ID: int = int(os.environ.get("LOBSTER_ADMIN_CHAT_ID", "8075091586"))


# ---------------------------------------------------------------------------
# Workspace / path helpers — pure functions
# ---------------------------------------------------------------------------

def _workspace() -> Path:
    return Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))


def _repo_root() -> Path:
    return Path(os.environ.get("LOBSTER_REPO", Path.home() / "lobster"))


def _registry_db_path() -> Path:
    env_override = os.environ.get("REGISTRY_DB_PATH")
    if env_override:
        return Path(env_override)
    return _workspace() / "orchestration" / "registry.db"


def _smell_patterns_path() -> Path:
    return _repo_root() / "oracle" / "smell-patterns.yaml"


def _golden_patterns_path() -> Path:
    return _repo_root() / "oracle" / "golden-patterns.md"


def _sessions_dir() -> Path:
    return Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config")) / "memory" / "canonical" / "sessions"


def _assessments_dir() -> Path:
    d = _workspace() / "assessments"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jobs_file() -> Path:
    return _workspace() / "scheduled-jobs" / "jobs.json"


def _task_output_record_path(timestamp: str) -> Path:
    date_prefix = timestamp[:19].replace(":", "").replace("-", "").replace("T", "-")
    return _task_outputs_dir() / f"{date_prefix}-{JOB_NAME}.json"


# ---------------------------------------------------------------------------
# jobs.json enabled gate — Type C dispatch pattern
# ---------------------------------------------------------------------------

def _is_job_enabled() -> bool:
    """Return True if this job is enabled in jobs.json. Defaults True when absent."""
    try:
        data = json.loads(_jobs_file().read_text())
        return bool(data.get("jobs", {}).get(JOB_NAME, {}).get("enabled", True))
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Shell helpers — pure (modulo subprocess)
# ---------------------------------------------------------------------------

def run_cmd(args: list[str], cwd: str | None = None, timeout: int = 30) -> str:
    """Run a subprocess and return stdout. Returns empty string on failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
        return ""


# ---------------------------------------------------------------------------
# Data collection — pure functions returning dicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeriodData:
    """All data collected for the retrospective period."""
    period_days: int
    since_iso: str
    merged_prs: list[dict] = field(default_factory=list)
    uow_footer_commits: list[str] = field(default_factory=list)
    completed_uows: list[dict] = field(default_factory=list)
    session_files_new: list[str] = field(default_factory=list)
    # Metabolic counts derived from completed_uows
    pearl_count: int = 0
    seed_count: int = 0
    heat_count: int = 0
    shit_count: int = 0


def _since_iso(days: int) -> str:
    """Return ISO 8601 UTC timestamp for N days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_git_data(repo_path: Path, since: str) -> tuple[list[dict], list[str]]:
    """
    Collect merged PRs and WOS-UoW footer stamps from git log.

    Returns:
        merged_prs: list of {sha, subject} for merge commits in period
        uow_footer_commits: list of UoW IDs found in commit footers
    """
    # Merge commits in the period (merged PRs appear as merge commits)
    log_raw = run_cmd(
        ["git", "log", f"--since={since}", "--oneline", "--merges"],
        cwd=str(repo_path),
    )
    merged_prs = []
    for line in log_raw.splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            merged_prs.append({"sha": parts[0], "subject": parts[1]})

    # Full commit messages in the period — grep for WOS-UoW footer stamps
    log_full = run_cmd(
        ["git", "log", f"--since={since}", "--format=%B"],
        cwd=str(repo_path),
    )
    uow_ids = re.findall(r"WOS-UoW:\s*(uow_[a-z0-9_]+)", log_full, re.IGNORECASE)

    return merged_prs, list(set(uow_ids))


def collect_completed_uows(db_path: Path, period_days: int) -> list[dict]:
    """
    Return UoWs that entered a terminal state in the last period_days.

    Returns [] if DB absent or query fails.
    """
    if not db_path.exists():
        log.warning("Registry DB not found at %s", db_path)
        return []

    cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
    terminal_ph = ",".join("?" * len(TERMINAL_STATUSES))

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            transition_rows = conn.execute(
                f"""
                SELECT DISTINCT uow_id FROM audit_log
                WHERE to_status IN ({terminal_ph})
                  AND ts >= ?
                """,
                (*TERMINAL_STATUSES, cutoff),
            ).fetchall()

            if not transition_rows:
                return []

            uow_ids = [r["uow_id"] for r in transition_rows]
            id_ph = ",".join("?" * len(uow_ids))

            rows = conn.execute(
                f"""
                SELECT id, status, summary, register, close_reason, output_ref,
                       started_at, closed_at, updated_at, created_at, steward_cycles
                FROM uow_registry
                WHERE id IN ({id_ph})
                  AND status IN ({terminal_ph})
                """,
                (*uow_ids, *TERMINAL_STATUSES),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Completed UoW query failed: %s", exc)
        return []


def collect_session_files(sessions_dir: Path, since: str) -> list[str]:
    """
    Return filenames of session files created or modified after `since`.

    Args:
        sessions_dir: Path to session files directory.
        since: ISO 8601 cutoff timestamp.

    Returns:
        List of session file names (not full paths).
    """
    if not sessions_dir.exists():
        return []
    cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
    new_files = []
    for f in sorted(sessions_dir.iterdir()):
        if f.is_file() and f.suffix in (".md", ".txt", ".json"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                new_files.append(f.name)
    return new_files


# ---------------------------------------------------------------------------
# Metabolic classification — pure functions
# ---------------------------------------------------------------------------

def classify_uow(uow: dict) -> str:
    """
    Classify a completed UoW as pearl / seed / heat / shit.

    Same heuristics as wos-metabolic-digest.py for consistency.
    """
    status = (uow.get("status") or "").lower()
    close_reason = (uow.get("close_reason") or "").lower()
    output_ref = uow.get("output_ref") or ""

    if status in ("failed", "expired", "cancelled"):
        return OUTCOME_SHIT
    if "ttl" in close_reason or "hard_cap" in close_reason or "user_closed" in close_reason:
        return OUTCOME_SHIT
    if status != "done":
        return OUTCOME_SHIT

    _SEED_ISSUE = re.compile(r"(opened|created|filed|new|spawned)\s+issue\s*#?\d*", re.IGNORECASE)
    _SEED_UOW = re.compile(r"(spawned|created|spawn)\s+(uow|new\s+uow)", re.IGNORECASE)
    if (
        _SEED_ISSUE.search(close_reason)
        or _SEED_UOW.search(close_reason)
        or "seeded" in close_reason
        or "follow-up" in close_reason
        or "follow_up" in close_reason
    ):
        return OUTCOME_SEED

    pearl_keywords = {"pr", "pull request", "implementation", "commit", "merge", "patch", "fix"}
    if any(kw in close_reason for kw in pearl_keywords):
        return OUTCOME_PEARL

    if output_ref:
        try:
            output_path = Path(output_ref)
            if output_path.exists() and output_path.stat().st_size > 500:
                heat_keywords = {"review", "analysis", "design", "synthesis", "assessment", "report"}
                if any(kw in close_reason for kw in heat_keywords):
                    return OUTCOME_HEAT
                return OUTCOME_PEARL
        except Exception:
            pass

    heat_keywords = {"review", "analysis", "design", "synthesis", "assessment", "report"}
    if any(kw in close_reason for kw in heat_keywords):
        return OUTCOME_HEAT

    return OUTCOME_SHIT


def compute_metabolic_counts(uows: list[dict]) -> dict[str, int]:
    """
    Classify all UoWs and return counts by outcome category.

    Returns a dict with keys: pearl, seed, heat, shit, total.
    """
    counts: dict[str, int] = {OUTCOME_PEARL: 0, OUTCOME_SEED: 0, OUTCOME_HEAT: 0, OUTCOME_SHIT: 0}
    for uow in uows:
        label = classify_uow(uow)
        counts[label] += 1
    counts["total"] = len(uows)
    return counts


def compute_metabolic_ratios(counts: dict[str, int]) -> dict[str, float]:
    """
    Compute fraction of total for each outcome category.

    Returns ratios with keys: pearl_ratio, seed_ratio, heat_ratio, shit_ratio.
    All ratios are 0.0 when total == 0.
    """
    total = counts.get("total", 0)
    if total == 0:
        return {f"{k}_ratio": 0.0 for k in [OUTCOME_PEARL, OUTCOME_SEED, OUTCOME_HEAT, OUTCOME_SHIT]}
    return {
        f"{k}_ratio": round(counts[k] / total, 3)
        for k in [OUTCOME_PEARL, OUTCOME_SEED, OUTCOME_HEAT, OUTCOME_SHIT]
    }


# ---------------------------------------------------------------------------
# Smell detection — pure functions
# ---------------------------------------------------------------------------

@dataclass
class SmellDetection:
    """Result of evaluating a single smell pattern against collected data."""
    pattern_id: str
    name: str
    severity: str
    detected: bool
    evidence: str
    recurrence_count: int
    open_issue_ref: str | None


def load_smell_patterns(patterns_path: Path) -> list[dict]:
    """
    Load smell patterns from YAML. Returns empty list on error.

    Each pattern dict has at least: id, name, severity, recurrence_count, status.
    """
    if not patterns_path.exists():
        log.warning("Smell patterns file not found at %s", patterns_path)
        return []
    try:
        data = yaml.safe_load(patterns_path.read_text())
        return data.get("patterns", [])
    except Exception as exc:
        log.warning("Failed to load smell patterns: %s", exc)
        return []


def write_back_recurrence_counts(
    patterns_path: Path,
    detections: list["SmellDetection"],
    dry_run: bool = False,
) -> None:
    """
    Write updated recurrence_count back to smell-patterns.yaml after each run.

    For each pattern:
    - If detected: increment recurrence_count by 1 and update last_detected to today
    - If not detected: reset recurrence_count to 0

    Uses atomic write (write to temp file, then rename) to prevent partial writes.
    Skips patterns not present in the YAML (e.g. no heuristic implemented).
    """
    if not patterns_path.exists():
        log.warning("Cannot write back recurrence counts — %s not found", patterns_path)
        return

    try:
        raw = patterns_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except Exception as exc:
        log.warning("Cannot write back recurrence counts — failed to load YAML: %s", exc)
        return

    patterns = data.get("patterns", [])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build lookup by pattern_id for O(1) access
    detection_by_id: dict[str, bool] = {d.pattern_id: d.detected for d in detections}

    updated = False
    for pattern in patterns:
        pid = pattern.get("id", "")
        if pid not in detection_by_id:
            continue  # pattern has no heuristic — leave counts untouched

        if detection_by_id[pid]:
            pattern["recurrence_count"] = int(pattern.get("recurrence_count", 0)) + 1
            pattern["last_detected"] = today
        else:
            pattern["recurrence_count"] = 0
        updated = True

    if not updated:
        log.debug("No patterns matched detections — smell-patterns.yaml unchanged")
        return

    if dry_run:
        log.info("DRY RUN: would write updated recurrence counts to %s", patterns_path)
        return

    try:
        tmp = Path(str(patterns_path) + ".tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        tmp.replace(patterns_path)
        log.info("Recurrence counts written back to %s", patterns_path)
    except Exception as exc:
        log.warning("Failed to write back recurrence counts to %s: %s", patterns_path, exc)


def _detect_write_result_not_back_propagated(
    uow_counts: dict[str, int],
    threshold: int,
) -> tuple[bool, str]:
    """
    Detect Smell: write_result not back-propagated.

    Heuristic: count UoWs classified as 'shit' (outcome_unverifiable proxy).
    Threshold is the minimum shit count to trigger detection.
    """
    shit_count = uow_counts.get(OUTCOME_SHIT, 0)
    total = uow_counts.get("total", 0)
    detected = shit_count > threshold
    evidence = (
        f"{shit_count}/{total} UoWs classified as 'shit' (unverifiable output); "
        f"threshold={threshold}"
    )
    return detected, evidence


def _detect_bare_python3_in_migrations(repo_path: Path, threshold: int) -> tuple[bool, str]:
    """
    Detect Smell: bare python3 in upgrade.sh migrations.

    Heuristic: grep for python3 or bare python lines in scripts/upgrade.sh
    that do not contain 'uv'.
    """
    upgrade_sh = repo_path / "scripts" / "upgrade.sh"
    if not upgrade_sh.exists():
        return False, "scripts/upgrade.sh not found"

    content = upgrade_sh.read_text()
    # Find lines matching python3 or 'python ' not preceded/followed by uv
    matches = []
    for lineno, line in enumerate(content.splitlines(), 1):
        stripped = line.strip()
        if re.search(r"\bpython3?\b", stripped) and "uv" not in stripped:
            # Exclude comments
            if not stripped.startswith("#"):
                matches.append(f"line {lineno}: {stripped[:80]}")

    count = len(matches)
    detected = count >= threshold
    if detected:
        evidence = f"{count} bare python3/python call(s) found without uv in upgrade.sh"
        if matches:
            evidence += ": " + "; ".join(matches[:3])
    else:
        evidence = f"No bare python3/python calls found in upgrade.sh (threshold={threshold})"
    return detected, evidence


def _detect_oracle_dual_write(repo_path: Path, threshold: int) -> tuple[bool, str]:
    """
    Detect Smell: oracle dual-write (decisions.md + verdicts/).

    Heuristic: check if oracle agent definition (lobster-oracle.md) still
    contains write instructions for decisions.md.
    """
    oracle_md_candidates = [
        repo_path / ".claude" / "agents" / "lobster-oracle.md",
        repo_path / "agents" / "lobster-oracle.md",
    ]
    oracle_md = next((p for p in oracle_md_candidates if p.exists()), None)

    if oracle_md is None:
        return False, "lobster-oracle.md not found — cannot check for dual-write"

    content = oracle_md.read_text()
    # Look specifically for write/open instructions targeting decisions.md —
    # not bare filename presence, which fires on historical/instructional references
    # in the agent's own instruction file.
    decisions_write_pattern = re.compile(
        r"(?:write|open|create|append|update)\s+[^\n]*decisions\.md",
        re.IGNORECASE,
    )
    matches = decisions_write_pattern.findall(content)
    count = len(matches)
    detected = count >= threshold

    if detected:
        evidence = (
            f"Found {count} write/open instruction(s) targeting 'decisions.md' in "
            f"lobster-oracle.md — dual-write path may still be active"
        )
    else:
        evidence = f"No write/open instructions targeting 'decisions.md' in lobster-oracle.md (threshold={threshold})"
    return detected, evidence


def _detect_rolling_summary_bloat(threshold: int) -> tuple[bool, str]:
    """
    Detect Smell: rolling-summary.md exceeds line threshold.

    Heuristic: count lines in rolling-summary.md.
    """
    rolling_summary = (
        Path(os.environ.get("LOBSTER_USER_CONFIG", Path.home() / "lobster-user-config"))
        / "memory" / "canonical" / "rolling-summary.md"
    )
    if not rolling_summary.exists():
        return False, "rolling-summary.md not found"

    lines = rolling_summary.read_text().count("\n")
    detected = lines > threshold
    evidence = f"rolling-summary.md has {lines} lines (threshold={threshold})"
    return detected, evidence


def _detect_health_probe_memory_saturation(
    workspace: Path, threshold: int,
) -> tuple[bool, str]:
    """
    Detect Smell: health probes saturating vector memory.

    Heuristic: count memory DB entries with event_type containing 'health_probe'
    or 'cron_heartbeat'.
    """
    db_path = workspace / "data" / "memory.db"
    if not db_path.exists():
        return False, "memory.db not found"

    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            # Try common schema variations
            row = conn.execute(
                """
                SELECT COUNT(*) as n FROM memory_events
                WHERE event_type IN ('health_probe', 'cron_heartbeat')
                   OR event_type LIKE '%health%'
                """
            ).fetchone()
            count = row[0] if row else 0
        except Exception:
            # Table may not exist or have different schema
            count = 0
        finally:
            conn.close()
    except Exception as exc:
        log.debug("Health probe memory check failed: %s", exc)
        count = 0

    detected = count > threshold
    evidence = f"{count} health-probe/cron-heartbeat entries in memory DB (threshold={threshold})"
    return detected, evidence


# Map pattern IDs to their detection functions
# Each function takes (pattern: dict, collected_data: dict) and returns (detected, evidence)
def _dispatch_detection(
    pattern: dict,
    uow_counts: dict[str, int],
    repo_path: Path,
    workspace: Path,
) -> tuple[bool, str]:
    """
    Route a pattern to its detection function by pattern ID.

    Returns (detected: bool, evidence: str).
    Any unknown pattern ID returns (False, "no detection heuristic implemented").
    """
    pid = pattern.get("id", "")
    threshold = int(pattern.get("threshold", 1))

    if pid == "write_result_not_back_propagated":
        return _detect_write_result_not_back_propagated(uow_counts, threshold)
    elif pid == "bare_python3_in_migrations":
        return _detect_bare_python3_in_migrations(repo_path, threshold)
    elif pid == "oracle_dual_write_no_enforcement":
        return _detect_oracle_dual_write(repo_path, threshold)
    elif pid == "rolling_summary_bloat":
        return _detect_rolling_summary_bloat(threshold)
    elif pid == "health_probe_memory_saturation":
        return _detect_health_probe_memory_saturation(workspace, threshold)
    else:
        return False, f"no detection heuristic implemented for pattern '{pid}'"


def detect_smells(
    patterns: list[dict],
    uow_counts: dict[str, int],
    repo_path: Path,
    workspace: Path,
) -> list[SmellDetection]:
    """
    Run all smell detection heuristics.

    Returns a list of SmellDetection results — one per pattern, regardless
    of whether detection fired. Callers filter by `detected` field.
    """
    results = []
    for pattern in patterns:
        detected, evidence = _dispatch_detection(pattern, uow_counts, repo_path, workspace)
        results.append(SmellDetection(
            pattern_id=pattern.get("id", ""),
            name=pattern.get("name", ""),
            severity=pattern.get("severity", SEVERITY_LOW),
            detected=detected,
            evidence=evidence,
            recurrence_count=int(pattern.get("recurrence_count", 0)),
            open_issue_ref=pattern.get("issue_ref"),
        ))
    return results


# ---------------------------------------------------------------------------
# Golden pattern drift detection — pure function
# ---------------------------------------------------------------------------

@dataclass
class GoldenPatternDrift:
    """A golden pattern that appears documented but potentially unenforced."""
    title: str
    evidence: str


def detect_golden_pattern_drift(
    golden_patterns_path: Path,
    repo_path: Path,
) -> list[GoldenPatternDrift]:
    """
    Check golden patterns for documented-but-unenforced state.

    Current heuristic: look for patterns where "enforcement: none" or
    "Reuse guidance" section exists but no corresponding hook/test file is
    referenced in the pattern entry. This is a lightweight signal, not
    exhaustive enforcement auditing.

    Returns list of GoldenPatternDrift for patterns flagged as potentially drifted.
    """
    if not golden_patterns_path.exists():
        return []

    content = golden_patterns_path.read_text()

    # Split into pattern entries by header
    sections = re.split(r"\n### \[", content)
    drifted = []

    for section in sections[1:]:  # skip preamble
        # Extract title from first line
        first_line = section.split("\n", 1)[0]
        title = first_line.strip()

        # Look for explicit "enforcement: none" signal
        if re.search(r"enforcement:\s*none", section, re.IGNORECASE):
            drifted.append(GoldenPatternDrift(
                title=title,
                evidence="Pattern marked 'enforcement: none'",
            ))

    return drifted


# ---------------------------------------------------------------------------
# GitHub issue filing — side effect, isolated
# ---------------------------------------------------------------------------

def _issue_already_open(label: str, repo: str) -> bool:
    """
    Check if any open issue with this label already exists.

    Returns True if at least one open issue with the label exists.
    """
    raw = run_cmd([
        "gh", "issue", "list",
        "--repo", repo,
        "--label", label,
        "--state", "open",
        "--json", "number",
        "--limit", "10",
    ])
    if not raw:
        return False
    try:
        items = json.loads(raw)
        return len(items) > 0
    except json.JSONDecodeError:
        return False


def file_github_issue(smell: SmellDetection, repo: str, dry_run: bool = False) -> str | None:
    """
    File a GitHub issue for a detected smell. Returns the issue URL or None.

    Skips filing if:
    - An open issue already references this pattern (smell.open_issue_ref set)
    - An open issue with label 'smell-pattern' already exists (guards against duplicates)
    - dry_run is True (logs intent, does not create)
    """
    # If the smell already has an open issue ref, don't re-file
    if smell.open_issue_ref:
        log.info("Smell '%s' already has open issue %s — skipping", smell.pattern_id, smell.open_issue_ref)
        return None

    label = "smell-pattern"
    if _issue_already_open(f"smell:{smell.pattern_id}", repo):
        log.info("Open issue with label smell:%s exists — skipping", smell.pattern_id)
        return None

    title = f"Smell detected: {smell.name} (auto-retrospective)"
    body = (
        f"**Pattern ID:** `{smell.pattern_id}`\n"
        f"**Severity:** {smell.severity}\n"
        f"**Recurrence count:** {smell.recurrence_count}\n\n"
        f"**Evidence:**\n{smell.evidence}\n\n"
        f"---\n"
        f"Auto-filed by `system-retrospective` job. "
        f"See `oracle/smell-patterns.yaml` for pattern definition."
    )

    if dry_run:
        log.info("DRY RUN: would file issue — %s", title)
        return "(dry-run)"

    result = run_cmd([
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--label", label,
    ], timeout=30)

    if result:
        log.info("Filed issue: %s", result)
        return result
    else:
        log.warning("Failed to file issue for smell '%s'", smell.pattern_id)
        return None


# ---------------------------------------------------------------------------
# Telegram escalation — side effect, isolated
# ---------------------------------------------------------------------------

def escalate_to_telegram(
    smell: SmellDetection,
    period_days: int,
    dry_run: bool = False,
) -> None:
    """
    Write an inbox message escalating a recurring smell to the admin.

    Fires when a smell has been detected in 2+ consecutive runs
    (recurrence_count >= ESCALATION_THRESHOLD).
    """
    text = (
        f"Smell escalation: '{smell.name}'\n"
        f"Detected for {smell.recurrence_count} consecutive retrospective(s). "
        f"Severity: {smell.severity}.\n"
        f"Evidence: {smell.evidence}\n"
        f"Pattern ID: {smell.pattern_id} — see oracle/smell-patterns.yaml"
    )

    if dry_run:
        log.info("DRY RUN: would send Telegram escalation: %s", text)
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        write_inbox_message(JOB_NAME, ADMIN_CHAT_ID, text, timestamp)
        log.info("Escalation message written to inbox for smell '%s'", smell.pattern_id)
    except Exception as exc:
        log.warning("Failed to write Telegram escalation: %s", exc)


# ---------------------------------------------------------------------------
# Assessment document writer — pure formatting + file write
# ---------------------------------------------------------------------------

def _format_metabolic_table(counts: dict[str, int], ratios: dict[str, float]) -> str:
    total = counts.get("total", 0)
    if total == 0:
        return "_No UoWs completed in this period._"
    return (
        f"| Category | Count | Ratio |\n"
        f"|----------|-------|-------|\n"
        f"| Pearl    | {counts[OUTCOME_PEARL]}     | {ratios['pearl_ratio']:.1%} |\n"
        f"| Seed     | {counts[OUTCOME_SEED]}     | {ratios['seed_ratio']:.1%} |\n"
        f"| Heat     | {counts[OUTCOME_HEAT]}     | {ratios['heat_ratio']:.1%} |\n"
        f"| Shit     | {counts[OUTCOME_SHIT]}     | {ratios['shit_ratio']:.1%} |\n"
        f"| **Total**| **{total}** | — |"
    )


def build_assessment_doc(
    date: str,
    period_days: int,
    since_iso: str,
    merged_prs: list[dict],
    uow_footer_commits: list[str],
    session_files_new: list[str],
    counts: dict[str, int],
    ratios: dict[str, float],
    detections: list[SmellDetection],
    drifted_patterns: list[GoldenPatternDrift],
    filed_issues: list[tuple[SmellDetection, str]],
    issue_867_open: bool,
) -> str:
    """
    Compose the assessment markdown document. Pure function — no side effects.
    """
    lines = [
        f"# Auto-Retrospective: {date}",
        f"**Period:** {since_iso} — {date} ({period_days} days)",
        f"**Generated by:** `system-retrospective` scheduled job",
        "",
    ]

    if issue_867_open:
        lines += [
            "> **Warning:** Issue #867 (write_result back-propagation) is still open.",
            "> Metabolic ratios are unreliable — UoW history is largely unverifiable.",
            "> Pearl/shit counts are lower/upper bounds only.",
            "",
        ]

    # Metabolic summary
    lines += [
        "## Metabolic Summary",
        "",
        _format_metabolic_table(counts, ratios),
        "",
        f"**Merged PRs in period:** {len(merged_prs)}",
        f"**WOS-UoW footer stamps found:** {len(uow_footer_commits)}",
        f"**New session files:** {len(session_files_new)}",
        "",
    ]

    if merged_prs:
        lines.append("### Merged PRs")
        for pr in merged_prs[:20]:
            lines.append(f"- `{pr['sha']}` {pr['subject']}")
        if len(merged_prs) > 20:
            lines.append(f"- … and {len(merged_prs) - 20} more")
        lines.append("")

    # Smell detection
    lines += [
        "## Smell Detection",
        "",
    ]

    detected_smells = [d for d in detections if d.detected]
    if not detected_smells:
        lines.append("No smell patterns detected above threshold this period.")
    else:
        for smell in detected_smells:
            lines += [
                f"### {smell.name}",
                f"- **Severity:** {smell.severity}",
                f"- **Recurrence count:** {smell.recurrence_count}",
                f"- **Evidence:** {smell.evidence}",
            ]
            if smell.open_issue_ref:
                lines.append(f"- **Existing issue:** {smell.open_issue_ref}")
            lines.append("")

    lines.append("")

    # Golden pattern drift
    lines += [
        "## Golden Pattern Drift",
        "",
    ]
    if not drifted_patterns:
        lines.append("No golden pattern drift detected.")
    else:
        for drift in drifted_patterns:
            lines += [
                f"- **{drift.title}**: {drift.evidence}",
            ]
    lines.append("")

    # Issues filed
    if filed_issues:
        lines += [
            "## Issues Filed This Run",
            "",
        ]
        for smell, url in filed_issues:
            lines.append(f"- [{smell.name}]({url})")
        lines.append("")

    # Not-detected smells (brief)
    not_detected = [d for d in detections if not d.detected]
    if not_detected:
        lines += [
            "## Patterns Below Threshold",
            "",
        ]
        for smell in not_detected:
            lines.append(f"- **{smell.pattern_id}**: {smell.evidence}")
        lines.append("")

    lines += [
        "---",
        f"*Generated by `system-retrospective` | {datetime.now(timezone.utc).isoformat()}*",
    ]

    return "\n".join(lines)


def write_assessment(content: str, date: str, dry_run: bool = False) -> Path | None:
    """Write the assessment document. Returns the output path or None in dry_run."""
    output_path = _assessments_dir() / f"{date}-auto-retrospective.md"
    if dry_run:
        log.info("DRY RUN: would write assessment to %s", output_path)
        log.info("--- Assessment preview ---\n%s\n---", content[:500])
        return None

    tmp = Path(str(output_path) + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(output_path)
    log.info("Assessment written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Task output writer — side effect, isolated
# ---------------------------------------------------------------------------

def write_task_output_record(
    summary: str,
    status: str,
    timestamp: str,
    dry_run: bool = False,
) -> None:
    """Write job completion record to task-outputs directory."""
    record = {
        "job_name": JOB_NAME,
        "timestamp": timestamp,
        "status": status,
        "output": summary,
    }
    if dry_run:
        log.info("DRY RUN: task output record: %s", json.dumps(record)[:200])
        return

    out_path = _task_output_record_path(timestamp)
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)
    log.info("Task output written to %s", out_path)


# ---------------------------------------------------------------------------
# Issue #867 open check
# ---------------------------------------------------------------------------

def _is_issue_867_open(repo: str) -> bool:
    """Check if issue #867 is still open on GitHub."""
    raw = run_cmd(["gh", "issue", "view", "867", "--repo", repo, "--json", "state"], timeout=15)
    if not raw:
        return True  # Assume open if we can't check
    try:
        data = json.loads(raw)
        return data.get("state", "OPEN").upper() == "OPEN"
    except json.JSONDecodeError:
        return True


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(period_days: int, dry_run: bool = False) -> int:
    """
    Execute the system retrospective pipeline.

    Returns 0 on success, 1 on failure.
    """
    now = datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    since = _since_iso(period_days)
    timestamp = now.isoformat()

    log.info("Starting system-retrospective — period=%d days since=%s dry_run=%s",
             period_days, since, dry_run)

    repo_path = _repo_root()
    workspace = _workspace()
    db_path = _registry_db_path()

    # --- Data collection ---
    log.info("Collecting git data...")
    merged_prs, uow_footer_commits = collect_git_data(repo_path, since)
    log.info("  Merged PRs: %d, UoW footer stamps: %d", len(merged_prs), len(uow_footer_commits))

    log.info("Collecting completed UoWs from registry...")
    completed_uows = collect_completed_uows(db_path, period_days)
    log.info("  Completed UoWs: %d", len(completed_uows))

    log.info("Scanning session files...")
    session_files_new = collect_session_files(_sessions_dir(), since)
    log.info("  New session files: %d", len(session_files_new))

    # --- Metabolic accounting ---
    counts = compute_metabolic_counts(completed_uows)
    ratios = compute_metabolic_ratios(counts)
    log.info(
        "Metabolic counts — pearl=%d seed=%d heat=%d shit=%d total=%d",
        counts[OUTCOME_PEARL], counts[OUTCOME_SEED],
        counts[OUTCOME_HEAT], counts[OUTCOME_SHIT], counts["total"],
    )

    # --- Smell detection ---
    log.info("Loading smell patterns...")
    patterns = load_smell_patterns(_smell_patterns_path())
    log.info("  Patterns loaded: %d", len(patterns))

    detections = detect_smells(patterns, counts, repo_path, workspace)
    detected_count = sum(1 for d in detections if d.detected)
    log.info("  Smells detected: %d / %d", detected_count, len(detections))

    # --- Write back recurrence counts ---
    # Must happen before any escalation or issue-filing reads recurrence_count from
    # SmellDetection, so the counts used for gating reflect the pre-run state from
    # smell-patterns.yaml. The write-back records what happened this run for next run.
    log.info("Writing recurrence counts back to smell-patterns.yaml...")
    write_back_recurrence_counts(_smell_patterns_path(), detections, dry_run=dry_run)

    # --- Golden pattern drift ---
    log.info("Checking golden pattern drift...")
    drifted = detect_golden_pattern_drift(_golden_patterns_path(), repo_path)
    log.info("  Drifted patterns: %d", len(drifted))

    # --- Issue #867 status ---
    issue_867_open = _is_issue_867_open(REPO)
    if issue_867_open:
        log.warning("Issue #867 is still open — metabolic ratios are unreliable")

    # --- File issues for high-severity detected smells ---
    filed_issues: list[tuple[SmellDetection, str]] = []
    issues_filed_count = 0

    for smell in detections:
        if not smell.detected:
            continue
        if smell.severity != SEVERITY_HIGH:
            continue
        if issues_filed_count >= MAX_ISSUES_PER_RUN:
            log.info("Reached max issues per run (%d) — skipping remaining", MAX_ISSUES_PER_RUN)
            break

        url = file_github_issue(smell, REPO, dry_run=dry_run)
        if url:
            filed_issues.append((smell, url))
            issues_filed_count += 1

    # --- Escalate recurring smells via Telegram ---
    # smell.recurrence_count holds the pre-run value (read from YAML before write-back).
    # write_back_recurrence_counts() already ran above, so after this run the YAML value
    # will be recurrence_count + 1.  We escalate only on the exact first crossing:
    # pre-run count == ESCALATION_THRESHOLD - 1 means this run pushes it to threshold.
    # Using == instead of >= prevents re-escalation on every subsequent run.
    for smell in detections:
        if not smell.detected:
            continue
        if smell.recurrence_count == ESCALATION_THRESHOLD - 1:
            log.info(
                "Escalating smell '%s' (recurrence_count=%d, crosses threshold=%d this run)",
                smell.pattern_id, smell.recurrence_count, ESCALATION_THRESHOLD,
            )
            escalate_to_telegram(smell, period_days, dry_run=dry_run)

    # --- Write assessment document ---
    assessment = build_assessment_doc(
        date=date,
        period_days=period_days,
        since_iso=since,
        merged_prs=merged_prs,
        uow_footer_commits=uow_footer_commits,
        session_files_new=session_files_new,
        counts=counts,
        ratios=ratios,
        detections=detections,
        drifted_patterns=drifted,
        filed_issues=filed_issues,
        issue_867_open=issue_867_open,
    )
    write_assessment(assessment, date, dry_run=dry_run)

    # --- Write task output ---
    detected_smells_summary = (
        ", ".join(d.pattern_id for d in detections if d.detected)
        or "none"
    )
    summary = (
        f"System retrospective complete for {date} (period={period_days}d). "
        f"Metabolic: {counts[OUTCOME_PEARL]}P / {counts[OUTCOME_SEED]}Se / "
        f"{counts[OUTCOME_HEAT]}H / {counts[OUTCOME_SHIT]}Sh "
        f"({counts['total']} UoWs total). "
        f"Smells detected: {detected_smells_summary}. "
        f"Issues filed: {issues_filed_count}. "
        f"Issue #867 open: {issue_867_open}."
    )
    log.info(summary)

    write_task_output_record(summary, "success", timestamp, dry_run=dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="System retrospective — automated smell detection and metabolic accounting"
    )
    parser.add_argument(
        "--period-days",
        type=int,
        default=DEFAULT_PERIOD_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_PERIOD_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and detect but do not file issues, send Telegram, or write files",
    )
    args = parser.parse_args()

    if not _is_job_enabled():
        log.info("Job '%s' is disabled in jobs.json — skipping", JOB_NAME)
        return 0

    return run(period_days=args.period_days, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
