"""
Tests for WOS error capture and classification.
"""

import subprocess
import time
import pytest
from orchestration.error_capture import (
    ErrorType,
    SubprocessError,
    ErrorClassification,
    classify_error,
    has_repeated_error,
    capture_subprocess_error,
    run_subprocess_with_error_capture,
    _classify_error_type,
)


class TestErrorClassification:
    """Test error classification logic."""

    def test_classify_uv_error(self):
        """Test detection of uv/dependency errors."""
        err = SubprocessError(
            component="executor",
            uow_id="test-001",
            error_type=ErrorType.NONZERO_EXIT,
            exit_code=1,
            stderr="error: failed to resolve dependency 'requests==1.2.3'",
            stdout="",
            command=["uv", "run", "my_script.py"],
            timestamp=time.time(),
        )
        classified = classify_error(err)
        assert classified.is_fatal
        assert "uv" in classified.classification
        assert "dependency" in classified.recovery_hint

    def test_classify_build_error(self):
        """Test detection of build failures."""
        err = SubprocessError(
            component="executor",
            uow_id="test-002",
            error_type=ErrorType.NONZERO_EXIT,
            exit_code=1,
            stderr="error: build failed",
            stdout="",
            command=["make", "build"],
            timestamp=time.time(),
        )
        classified = classify_error(err)
        assert classified.is_fatal
        assert "build" in classified.classification

    def test_classify_missing_binary(self):
        """Test detection of missing binaries."""
        err = SubprocessError(
            component="executor",
            uow_id="test-003",
            error_type=ErrorType.MISSING_BINARY,
            exit_code=127,
            stderr="claude: command not found",
            stdout="",
            command=["claude", "-p", "test"],
            timestamp=time.time(),
        )
        classified = classify_error(err)
        assert classified.is_fatal
        assert "missing" in classified.classification

    def test_classify_timeout(self):
        """Test that timeouts are not immediately fatal."""
        err = SubprocessError(
            component="executor",
            uow_id="test-004",
            error_type=ErrorType.TIMEOUT,
            exit_code=None,
            stderr="subprocess timed out after 60s",
            stdout="",
            command=["slow_command"],
            timestamp=time.time(),
        )
        classified = classify_error(err)
        assert not classified.is_fatal  # Single timeout is not fatal
        assert "timeout" in classified.classification


class TestErrorTypeClassification:
    """Test error type detection."""

    def test_nonzero_exit(self):
        """Test detection of non-zero exit code."""
        assert _classify_error_type(1, "") == ErrorType.NONZERO_EXIT
        assert _classify_error_type(127, "command not found") == ErrorType.MISSING_BINARY

    def test_timeout(self):
        """Test detection of timeout."""
        assert _classify_error_type(None, "") == ErrorType.TIMEOUT

    def test_uv_error(self):
        """Test detection of uv errors."""
        assert (
            _classify_error_type(1, "error: failed to install dependencies", ["uv", "run", "script.py"])
            == ErrorType.UV_ERROR
        )

    def test_build_failure(self):
        """Test detection of build failures."""
        assert (
            _classify_error_type(1, "failed to build")
            == ErrorType.BUILD_FAILURE
        )


class TestRepeatedErrorDetection:
    """Test repeated error detection."""

    def test_no_repeated_error_initially(self):
        """Test that errors are counted as new."""
        # First occurrence
        has_first = has_repeated_error("component1", "uow1", "error_type1", threshold=3)
        assert not has_first  # 1 < 3

        # Second occurrence
        has_second = has_repeated_error("component1", "uow1", "error_type1", threshold=3)
        assert not has_second  # 2 < 3

        # Third occurrence should trigger
        has_third = has_repeated_error("component1", "uow1", "error_type1", threshold=3)
        assert has_third  # 3 >= 3

    def test_separate_error_types_not_counted_together(self):
        """Test that different error types are tracked separately."""
        # Use unique identifiers to avoid test pollution
        has_first = has_repeated_error("comp_unique2", "uow_unique2", "error_type1", threshold=2)
        assert not has_first

        # Different error type should not increment the same counter
        has_different = has_repeated_error("comp_unique2", "uow_unique2", "error_type2", threshold=2)
        assert not has_different

    def test_separate_uows_not_counted_together(self):
        """Test that different UoWs are tracked separately."""
        has_first = has_repeated_error("component1", "uow1", "error_type", threshold=2)
        assert not has_first

        # Different UoW should not increment the same counter
        has_different = has_repeated_error("component1", "uow2", "error_type", threshold=2)
        assert not has_different


class TestSubprocessErrorCapture:
    """Test subprocess error capture."""

    def test_capture_subprocess_error_nonzero_exit(self):
        """Test capturing a subprocess error with non-zero exit."""
        err = capture_subprocess_error(
            component="test_comp",
            uow_id="test_uow",
            command=["false"],
            returncode=1,
            stderr="error message",
            stdout="some output",
        )

        assert err.component == "test_comp"
        assert err.uow_id == "test_uow"
        assert err.exit_code == 1
        assert err.stderr == "error message"
        assert err.stdout == "some output"

    def test_error_summary(self):
        """Test error summary formatting."""
        err = SubprocessError(
            component="executor",
            uow_id="test-001",
            error_type=ErrorType.NONZERO_EXIT,
            exit_code=1,
            stderr="error",
            stdout="",
            command=["test"],
            timestamp=time.time(),
        )
        summary = err.summary()
        assert "executor" in summary
        assert "test-001" in summary
        assert "exit=1" in summary

    def test_error_detail(self):
        """Test error detail formatting."""
        err = SubprocessError(
            component="executor",
            uow_id="test-001",
            error_type=ErrorType.NONZERO_EXIT,
            exit_code=1,
            stderr="error: something failed",
            stdout="",
            command=["test"],
            timestamp=time.time(),
        )
        detail = err.detail()
        assert "executor" in detail
        assert "error: something failed" in detail

    def test_error_to_dict(self):
        """Test conversion to JSON-serializable dict."""
        err = SubprocessError(
            component="executor",
            uow_id="test-001",
            error_type=ErrorType.NONZERO_EXIT,
            exit_code=1,
            stderr="error",
            stdout="",
            command=["test"],
            timestamp=time.time(),
        )
        d = err.to_dict()
        assert d["component"] == "executor"
        assert d["uow_id"] == "test-001"
        assert d["error_type"] == "nonzero_exit"  # Should be string
        assert d["exit_code"] == 1


class TestRunSubprocessWithErrorCapture:
    """Test the subprocess wrapper."""

    def test_successful_subprocess(self):
        """Test successful subprocess execution."""
        proc, error = run_subprocess_with_error_capture(
            component="test",
            uow_id="test_001",
            command=["echo", "hello"],
            timeout_seconds=10,
            check=False,
        )
        assert proc is not None
        assert error is None
        assert "hello" in proc.stdout

    def test_failing_subprocess(self):
        """Test failing subprocess is captured."""
        proc, error = run_subprocess_with_error_capture(
            component="test",
            uow_id="test_002",
            command=["sh", "-c", "exit 42"],
            timeout_seconds=10,
            check=False,
        )
        assert proc is not None
        assert error is not None
        assert error.exit_code == 42
        assert error.component == "test"
        assert error.uow_id == "test_002"

    def test_missing_binary_is_captured(self):
        """Test missing binary is captured as error."""
        proc, error = run_subprocess_with_error_capture(
            component="test",
            uow_id="test_003",
            command=["nonexistent_binary_xyz_123"],
            timeout_seconds=10,
            check=False,
        )
        assert error is not None
        assert error.error_type == ErrorType.MISSING_BINARY

    def test_timeout_is_captured(self):
        """Test timeout is captured as error."""
        proc, error = run_subprocess_with_error_capture(
            component="test",
            uow_id="test_004",
            command=["sleep", "100"],
            timeout_seconds=0.1,
            check=False,
        )
        assert error is not None
        assert error.error_type == ErrorType.TIMEOUT


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
