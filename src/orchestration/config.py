"""
Centralized timeout configuration for orchestration layer.

Consolidates hardcoded timeout values across executor, steward, and other
components into a single source of truth. Each timeout has a documented purpose
and can be overridden via environment variable.
"""

import os


class TimeoutConfig:
    """Centralized timeout configuration."""

    @staticmethod
    def claude_dispatch_timeout_secs() -> int:
        """
        Timeout for the claude -p functional-engineer subprocess in seconds.
        Matched to the default UoW estimated_runtime ceiling (30 minutes)
        plus a generous buffer.

        Override via: WOS_EXECUTOR_TIMEOUT env var
        Default: 7200 seconds (2 hours)
        """
        return int(os.environ.get("WOS_EXECUTOR_TIMEOUT", "7200"))

    @staticmethod
    def llm_prescription_timeout_secs() -> int:
        """
        Timeout for LLM-based prescription diagnosis in seconds.
        Used by the Steward during diagnosis phase.

        Override via: LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS env var
        Default: 600 seconds (10 minutes)
        """
        return int(os.environ.get("LOBSTER_LLM_PRESCRIPTION_TIMEOUT_SECS", "600"))

    @staticmethod
    def github_api_timeout_secs() -> int:
        """
        Timeout for GitHub API calls (gh CLI) in seconds.
        Covers issue closure, comment posting, and other sync operations.

        Override via: LOBSTER_GITHUB_API_TIMEOUT_SECS env var
        Default: 30 seconds
        """
        return int(os.environ.get("LOBSTER_GITHUB_API_TIMEOUT_SECS", "30"))
