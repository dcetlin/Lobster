#!/usr/bin/env uv run
"""
Lobster Tier 2: Daily AIDE Integrity Check
Runs AIDE against the baseline and logs/alerts on changes.

Type B job - cron-direct, no MCP inbox message
"""
import subprocess
import sys
import json
import os
from datetime import datetime
from pathlib import Path

# Add src to path for utils imports
repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root))

from src.utils.jobs import is_job_enabled

def run_aide_check():
    """Run AIDE check and return result."""
    try:
        result = subprocess.run(
            ["/usr/sbin/aide", "--check"],
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "AIDE check timed out after 600 seconds"
    except Exception as e:
        return 1, "", f"Failed to run AIDE: {e}"

def should_alert(returncode):
    """Determine if changes were detected (aid returns 7 if changes found)."""
    # AIDE return codes:
    # 0 = ok
    # 7 = changes detected
    return returncode == 7

def main():
    # Gate check - is this job enabled?
    if not is_job_enabled("aide-daily-check"):
        print("aide-daily-check is disabled in jobs.json - skipping", file=sys.stderr)
        sys.exit(0)

    returncode, stdout, stderr = run_aide_check()

    # Prepare output
    timestamp = datetime.utcnow().isoformat()
    output = {
        "timestamp": timestamp,
        "returncode": returncode,
        "changes_detected": should_alert(returncode),
        "stdout_lines": len(stdout.splitlines()),
        "stderr_lines": len(stderr.splitlines())
    }

    if should_alert(returncode):
        # Changes detected - log and potentially alert
        output["alert"] = True
        output["summary"] = "AIDE detected changes"

        # Extract a summary from aide output (first few changed files)
        lines = stdout.splitlines()
        changed_files = [l for l in lines if l.startswith("changed") or l.startswith("added") or l.startswith("removed")]
        if changed_files:
            output["examples"] = changed_files[:5]

        # Log detailed output
        log_output = f"{timestamp} - AIDE changes detected (return={returncode})\n"
        log_output += "=== AIDE Output ===\n"
        log_output += stdout
        if stderr:
            log_output += "\n=== AIDE Errors ===\n"
            log_output += stderr

        print(log_output, file=sys.stderr)
    else:
        output["summary"] = "AIDE check clean"

    # Print JSON for structured logging
    print(json.dumps(output, indent=2))

    return 0 if not should_alert(returncode) else 7

if __name__ == "__main__":
    sys.exit(main())
