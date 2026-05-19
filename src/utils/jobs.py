import json
import os
from pathlib import Path


def is_job_enabled(job_name: str, default: bool = True) -> bool:
    """
    Return True if the job is enabled in jobs.json, False if explicitly disabled.

    Defaults to ``default`` (True) when:
    - jobs.json is absent
    - the job entry is missing
    - the file is unreadable or malformed

    This mirrors the gate logic in dispatch-job.sh so Type B (cron-direct) jobs
    respect the same runtime enable/disable toggle as Type A jobs.
    """
    workspace = Path(os.environ.get("LOBSTER_WORKSPACE", Path.home() / "lobster-workspace"))
    jobs_file = workspace / "scheduled-jobs" / "jobs.json"
    try:
        data = json.loads(jobs_file.read_text())
        return bool(data.get("jobs", {}).get(job_name, {}).get("enabled", default))
    except Exception:
        return default
