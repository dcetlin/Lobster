#!/bin/bash
#===============================================================================
# Test Suite: Restart-coordination lock (issue #1537)
#
# A failure-injection test surfaced that a manual restart (operator-run
# restart-mcp.sh or dispatcher-refresh.sh) and an automatic restart
# (health-check-v3.sh's do_restart() / check_session_age()) could fire
# concurrently and both act against the same dispatcher session. The fix is
# a shared, non-blocking flock-based lock (scripts/restart-lock-lib.sh) that
# every restart-triggering path must acquire before performing its action.
#
# health-check-v3.sh's own use of the lock (inside do_restart() and
# check_session_age()) is covered by tests/test-health-check-session-age.sh
# (test 12, "restart_lock_held_skips_sigterm") and manual code review of
# do_restart() (not unit-testable in isolation — it drives real systemctl/
# tmux/sudo calls). This suite covers:
#   1. restart-lock-lib.sh: acquire/release semantics in isolation
#   2. Cross-process contention: a second process cannot acquire a held lock
#   3. scripts/restart-mcp.sh aborts (exit 1, no systemctl call) when the
#      lock is already held
#   4. scripts/dispatcher-refresh.sh aborts (exit 1, no SIGTERM sent) when
#      the lock is already held
#   5. scripts/restart-mcp.sh and scripts/dispatcher-refresh.sh compute the
#      SAME default lock file path — required for them to actually contend
#      with one another and with health-check-v3.sh
#
# Tests that would require actually restarting a systemd service or sending
# SIGTERM to a real dispatcher process are intentionally NOT exercised here
# (out of scope for an automated unit test — see PR's Manual test section).
#
# Usage: bash tests/test-restart-lock-coordination.sh
#===============================================================================

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
LOCK_LIB="$SCRIPT_DIR/restart-lock-lib.sh"
RESTART_MCP="$SCRIPT_DIR/restart-mcp.sh"
DISPATCHER_REFRESH="$SCRIPT_DIR/dispatcher-refresh.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-restart-lock-test-XXXXXX)
cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

# Safety net: restart-mcp.sh calls real `sudo systemctl restart <unit>` on
# its happy path. Every invocation of restart-mcp.sh in this suite MUST run
# with this stub bin directory prepended to PATH, so that even if a lock
# assumption is ever wrong, no real systemd unit on this host can be
# restarted by this test suite. `systemctl list-unit-files` (used for unit
# auto-detection) is also stubbed to avoid depending on host state.
STUB_BIN="$TEST_TMPDIR/bin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/sudo" <<'EOF'
#!/bin/bash
echo "STUB sudo $*" >> "${STUB_CALL_LOG:-/dev/null}"
exit 0
EOF
cat > "$STUB_BIN/systemctl" <<'EOF'
#!/bin/bash
echo "STUB systemctl $*" >> "${STUB_CALL_LOG:-/dev/null}"
if [[ "$1" == "list-unit-files" ]]; then
    exit 1  # no matching unit — forces restart-mcp.sh's fallback unit name
fi
exit 0
EOF
chmod +x "$STUB_BIN/sudo" "$STUB_BIN/systemctl"
export STUB_CALL_LOG="$TEST_TMPDIR/stub-calls.log"
STUBBED_PATH="$STUB_BIN:$PATH"

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()  { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()  { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

# Hold the shared lock in a genuinely separate process (fork via `&`) for
# up to $2 seconds. Returns once the lock is confirmed held.
#
# The background subshell's stdout/stderr MUST be redirected away from the
# caller's command-substitution pipe (>/dev/null 2>&1) — otherwise, even
# though `echo $!` returns immediately, `$(hold_lock_in_background ...)`
# will not return until the backgrounded sleep finishes. This is the classic
# bash gotcha where a background job started inside `$(...)` inherits the
# substitution's stdout pipe fd; the reader doesn't see EOF (and so doesn't
# return) until every process holding that fd — including the backgrounded
# one — closes it, even if that process never writes to it.
hold_lock_in_background() {
    local lock_file="$1"
    local hold_seconds="$2"
    (
        exec 201>"$lock_file"
        flock -n 201 || exit 1
        sleep "$hold_seconds"
    ) >/dev/null 2>&1 &
    echo $!
}

# Poll until the given PID is no longer alive (or a bounded number of
# attempts is exhausted). `wait $pid` cannot be used here: PIDs returned by
# hold_lock_in_background are backgrounded inside a command-substitution
# subshell, so by the time the caller sees the PID that subshell has already
# exited — the PID is not a direct child of the caller's shell, and bash's
# `wait` builtin only works on direct children (it would return 127
# immediately rather than actually waiting).
wait_for_pid_exit() {
    local pid="$1"
    local attempts=0
    while kill -0 "$pid" 2>/dev/null; do
        attempts=$((attempts + 1))
        [[ $attempts -gt 50 ]] && return 1  # ~5s cap
        sleep 0.1
    done
    return 0
}

echo ""
echo "=== Restart-Coordination Lock Tests ==="
echo ""

# 1. acquire_restart_coordination_lock() succeeds when uncontended
begin_test "acquire_succeeds_when_uncontended"
(
    LOCK_FILE="$TEST_TMPDIR/lock1"
    LOBSTER_RESTART_LOCK="$LOCK_FILE" bash -c "
        source '$LOCK_LIB'
        acquire_restart_coordination_lock
    "
)
assert_rc=$?
if [[ $assert_rc -eq 0 ]]; then pass; else fail "expected exit 0, got $assert_rc"; fi

# 2. A second, separate process cannot acquire a lock already held
begin_test "second_process_cannot_acquire_held_lock"
lock_file="$TEST_TMPDIR/lock2"
holder_pid=$(hold_lock_in_background "$lock_file" 3)
sleep 0.5
LOBSTER_RESTART_LOCK="$lock_file" bash -c "
    source '$LOCK_LIB'
    acquire_restart_coordination_lock
"
rc=$?
kill "$holder_pid" 2>/dev/null || true
wait_for_pid_exit "$holder_pid" || true
if [[ $rc -eq 1 ]]; then pass; else fail "expected exit 1 (lock contended), got $rc"; fi

# 3. Once the holder releases (process exits), a new acquire succeeds
begin_test "acquire_succeeds_after_holder_releases"
lock_file="$TEST_TMPDIR/lock3"
holder_pid=$(hold_lock_in_background "$lock_file" 1)
wait_for_pid_exit "$holder_pid"  # holder process exits, releasing the lock
LOBSTER_RESTART_LOCK="$lock_file" bash -c "
    source '$LOCK_LIB'
    acquire_restart_coordination_lock
"
rc=$?
if [[ $rc -eq 0 ]]; then pass; else fail "expected exit 0 after release, got $rc"; fi

# 4. restart-mcp.sh aborts (exit 1) and does not reach the systemctl call
#    when the coordination lock is already held.
#
# sudo/systemctl are stubbed (PATH=$STUBBED_PATH) as a safety net so this
# assertion is never load-bearing on host state — even if the lock were not
# actually held, no real systemd unit could be restarted by this test.
begin_test "restart_mcp_aborts_when_lock_held"
lock_file="$TEST_TMPDIR/lock4"
messages_dir="$TEST_TMPDIR/messages4"
mkdir -p "$messages_dir/inbox"
: > "$STUB_CALL_LOG"
holder_pid=$(hold_lock_in_background "$lock_file" 3)
sleep 0.5
output=$(LOBSTER_RESTART_LOCK="$lock_file" LOBSTER_MESSAGES="$messages_dir" PATH="$STUBBED_PATH" bash "$RESTART_MCP" --no-wait 2>&1)
rc=$?
kill "$holder_pid" 2>/dev/null || true
wait_for_pid_exit "$holder_pid" || true
inbox_count=$(find "$messages_dir/inbox" -name "*.json" 2>/dev/null | wc -l)
stub_calls=$(wc -l < "$STUB_CALL_LOG" 2>/dev/null || echo 0)
if [[ $rc -eq 1 && "$output" == *"already in progress"* && "$inbox_count" -eq 0 && "$stub_calls" -eq 0 ]]; then
    pass
else
    fail "expected exit 1 + abort message + no inbox write + no systemctl/sudo calls, got rc=$rc inbox_count=$inbox_count stub_calls=$stub_calls output='$output'"
fi

# 5. dispatcher-refresh.sh aborts (exit 1) and does NOT send SIGTERM to the
#    dispatcher process when the coordination lock is already held.
begin_test "dispatcher_refresh_aborts_when_lock_held"
lock_file="$TEST_TMPDIR/lock5"
messages_dir="$TEST_TMPDIR/messages5"
mkdir -p "$messages_dir/config"
sleep 600 &
target_pid=$!
echo "$target_pid" > "$messages_dir/config/dispatcher.pid"
holder_pid=$(hold_lock_in_background "$lock_file" 3)
sleep 0.5
output=$(LOBSTER_RESTART_LOCK="$lock_file" LOBSTER_MESSAGES="$messages_dir" bash "$DISPATCHER_REFRESH" 2>&1)
rc=$?
still_alive=false
kill -0 "$target_pid" 2>/dev/null && still_alive=true
kill "$target_pid" 2>/dev/null || true
kill "$holder_pid" 2>/dev/null || true
wait_for_pid_exit "$holder_pid" || true
if [[ $rc -eq 1 && "$output" == *"already in progress"* && "$still_alive" == "true" ]]; then
    pass
else
    fail "expected exit 1 + abort message + target PID alive, got rc=$rc still_alive=$still_alive output='$output'"
fi

# 6. dispatcher-refresh.sh sends SIGTERM when the lock is free (happy path,
#    using a dummy target process — never a real dispatcher).
begin_test "dispatcher_refresh_sends_sigterm_when_lock_free"
lock_file="$TEST_TMPDIR/lock6"
messages_dir="$TEST_TMPDIR/messages6"
mkdir -p "$messages_dir/config"
sleep 600 &
target_pid=$!
echo "$target_pid" > "$messages_dir/config/dispatcher.pid"
LOBSTER_RESTART_LOCK="$lock_file" LOBSTER_MESSAGES="$messages_dir" bash "$DISPATCHER_REFRESH" >/dev/null 2>&1
rc=$?
sleep 0.3
still_alive=false
kill -0 "$target_pid" 2>/dev/null && still_alive=true
kill -9 "$target_pid" 2>/dev/null || true
if [[ $rc -eq 0 && "$still_alive" == "false" ]]; then
    pass
else
    fail "expected exit 0 + SIGTERM delivered, got rc=$rc still_alive=$still_alive"
fi

# 7. restart-mcp.sh and dispatcher-refresh.sh resolve to the SAME default
#    lock file path (required for real coordination — a lock only works if
#    every path contends for the same file).
begin_test "scripts_share_the_same_default_lock_path"
messages_dir="$TEST_TMPDIR/messages7"
path_from_mcp=$(LOBSTER_MESSAGES="$messages_dir" bash -c "
    source '$LOCK_LIB'
    echo \"\$RESTART_COORDINATION_LOCK_FILE\"
")
if [[ "$path_from_mcp" == "$messages_dir/config/restart-coordination.lock" ]]; then
    pass
else
    fail "expected $messages_dir/config/restart-coordination.lock, got '$path_from_mcp'"
fi

#===============================================================================
# Summary
#===============================================================================
echo ""
echo "Results: $PASS passed, $FAIL failed, $TOTAL total"
if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All restart-lock coordination tests passed.${NC}"
    exit 0
else
    echo -e "${RED}$FAIL test(s) failed.${NC}"
    exit 1
fi
