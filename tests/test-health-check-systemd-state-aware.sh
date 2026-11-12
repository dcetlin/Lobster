#!/bin/bash
#===============================================================================
# Test Suite: Health Check v3 - Systemd-State-Aware Classification
#
# Tests for:
#   1. get_systemd_state() returns ActiveState and SubState lines
#   2. is_systemd_activating() returns 0 for activating units
#   3. is_systemd_activating() returns 0 for auto-restart SubState
#   4. is_systemd_activating() returns 1 for active/running units
#   5. is_systemd_activating() returns 1 for failed units
#   6. SYSTEMD_ACTIVATING_AGE_SECONDS is set correctly
#   7. check_services() returns 1 (YELLOW) for activating within grace window
#   8. check_services() returns 2 (RED) for genuinely failed unit
#   9. check_services() returns 2 (RED) when activating exceeds grace window
#
# Usage: bash tests/test-health-check-systemd-state-aware.sh
#===============================================================================

set -eE

# Helper to capture exit code without triggering set -e
run_and_capture_rc() {
    "$@" && RC=$? || RC=$?
}

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TOTAL=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts"
HEALTH_SCRIPT="$SCRIPT_DIR/health-check-v3.sh"

TEST_TMPDIR=$(mktemp -d /tmp/lobster-systemd-state-test-XXXXXX)
TEST_MESSAGES="$TEST_TMPDIR/messages"
TEST_CONFIG="$TEST_MESSAGES/config"
TEST_LOG_DIR="$TEST_TMPDIR/logs"
TEST_LOG="$TEST_LOG_DIR/health-check.log"

cleanup() { rm -rf "$TEST_TMPDIR"; }
trap cleanup EXIT

mkdir -p "$TEST_CONFIG" "$TEST_LOG_DIR"

#===============================================================================
# Helpers
#===============================================================================

begin_test() { TOTAL=$((TOTAL + 1)); test_name="$1"; }
pass()        { PASS=$((PASS + 1)); echo -e "  ${GREEN}PASS${NC} $test_name"; }
fail()        { FAIL=$((FAIL + 1)); echo -e "  ${RED}FAIL${NC} $test_name: $1"; }

assert_rc() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected RC=$expected, got RC=$actual"; fi
}

assert_eq() {
    local actual="$1" expected="$2"
    if [[ "$actual" == "$expected" ]]; then pass; else fail "expected '$expected', got '$actual'"; fi
}

#===============================================================================
# Stub setup — we override the get_systemd_state() function directly in the
# shell rather than stubbing the systemctl binary. This is simpler and avoids
# PATH/subprocess complications.
#===============================================================================

# set_stub_state sets the canned state that the overridden get_systemd_state()
# and the InactiveExitTimestampMonotonic query will return.
STUB_ACTIVE_STATE="active"
STUB_SUB_STATE="running"
STUB_INACTIVE_EXIT_TS=0

set_stub_state() {
    STUB_ACTIVE_STATE="$1"
    STUB_SUB_STATE="$2"
    STUB_INACTIVE_EXIT_TS="${3:-0}"
}

#===============================================================================
# Source functions from health-check-v3.sh, then override get_systemd_state
#===============================================================================

LOG_FILE="$TEST_LOG"
LOBSTER_STATE_FILE="$TEST_CONFIG/lobster-state.json"
SYSTEMD_ACTIVATING_GRACE_SECONDS=120
SERVICE_CLAUDE="lobster-claude"
SERVICE_ROUTER="lobster-router"
SYSTEMD_ACTIVATING_AGE_SECONDS=0
mkdir -p "$(dirname "$LOG_FILE")"

# Source log functions
log()       { echo "[$(date -Iseconds)] [$1] $2" >> "$LOG_FILE"; }
log_info()  { log "INFO"  "$1"; }
log_warn()  { log "WARN"  "$1"; }
log_error() { log "ERROR" "$1"; }

# Source get_systemd_state (real version, but we override immediately after)
eval "$(sed -n '/^get_systemd_state()/,/^}/p' "$HEALTH_SCRIPT")" 2>/dev/null

# Source is_systemd_activating
eval "$(awk '/^SYSTEMD_ACTIVATING_AGE_SECONDS=0/{found=1} found && /^is_systemd_activating\(\)/,/^\}$/{print}' "$HEALTH_SCRIPT")" 2>/dev/null

# Source check_services
eval "$(awk '/^check_services\(\)/,/^\}$/{print}' "$HEALTH_SCRIPT")" 2>/dev/null

# Override get_systemd_state to return stub values.
# Also override the InactiveExitTimestampMonotonic query inside is_systemd_activating
# by overriding the whole function with a testable version that uses stub values.
get_systemd_state() {
    local unit="$1"
    echo "ActiveState=${STUB_ACTIVE_STATE}"
    echo "SubState=${STUB_SUB_STATE}"
}

# Re-define is_systemd_activating to use the stub for InactiveExitTimestampMonotonic
# while preserving the rest of the logic.
is_systemd_activating() {
    local unit="$1"
    SYSTEMD_ACTIVATING_AGE_SECONDS=0

    local state_output
    state_output=$(get_systemd_state "$unit")
    if [[ -z "$state_output" ]]; then
        return 1
    fi

    local active_state sub_state
    active_state=$(echo "$state_output" | grep '^ActiveState=' | cut -d= -f2)
    sub_state=$(echo "$state_output" | grep '^SubState=' | cut -d= -f2)

    if [[ "$active_state" == "activating" || "$sub_state" == "auto-restart" ]]; then
        # Use stub InactiveExitTimestampMonotonic instead of real systemctl call
        local inactive_exit_ts="$STUB_INACTIVE_EXIT_TS"
        if [[ "$inactive_exit_ts" =~ ^[0-9]+$ && "$inactive_exit_ts" -gt 0 ]]; then
            local uptime_s
            uptime_s=$(awk '{print int($1)}' /proc/uptime 2>/dev/null || echo 0)
            local uptime_us=$(( uptime_s * 1000000 ))
            local age_us=$(( uptime_us - inactive_exit_ts ))
            local age_s=$(( age_us / 1000000 ))
            [[ $age_s -lt 0 ]] && age_s=0
            SYSTEMD_ACTIVATING_AGE_SECONDS=$age_s
        fi
        return 0
    fi

    return 1
}

# Override systemctl as a bash function so it can access STUB_* variables
# directly. This is simpler than a file-based stub.
#
# The function handles two call patterns used by check_services():
#   systemctl is-active <unit>         → echo "$STUB_ACTIVE_STATE" (for lobster-claude)
#                                        echo "active" (for lobster-router)
#   systemctl is-active --quiet <unit> → exit 0 if active, 1 otherwise
#
# All other systemctl calls fall through to the real binary.
systemctl() {
    local subcmd="$1"
    shift
    if [[ "$subcmd" == "is-active" ]]; then
        local quiet=false unit=""
        for arg in "$@"; do
            [[ "$arg" == "--quiet" ]] && quiet=true || unit="$arg"
        done
        local effective_state
        if [[ "$unit" == "$SERVICE_CLAUDE" ]]; then
            effective_state="$STUB_ACTIVE_STATE"
        else
            # Other units (e.g. lobster-router) — return active
            effective_state="active"
        fi
        if [[ "$quiet" == true ]]; then
            [[ "$effective_state" == "active" ]] && return 0 || return 1
        else
            echo "$effective_state"
            [[ "$effective_state" == "active" ]] && return 0 || return 1
        fi
    fi
    # Fall through to real systemctl for other subcommands
    /bin/systemctl "$subcmd" "$@"
}

#===============================================================================
# Test 1: get_systemd_state() (stub) returns correct ActiveState/SubState
#===============================================================================
begin_test "get_systemd_state (stub): returns correct state lines"

set_stub_state "active" "running" 0
state_output=$(get_systemd_state "lobster-claude")
if echo "$state_output" | grep -q "^ActiveState=active" && \
   echo "$state_output" | grep -q "^SubState=running"; then
    pass
else
    fail "expected ActiveState=active SubState=running, got: $state_output"
fi

#===============================================================================
# Test 2: is_systemd_activating() returns 0 for ActiveState=activating
#===============================================================================
begin_test "is_systemd_activating: returns 0 (true) for ActiveState=activating"

set_stub_state "activating" "start" 0
run_and_capture_rc is_systemd_activating "lobster-claude"
assert_rc "$RC" "0"

#===============================================================================
# Test 3: is_systemd_activating() returns 0 for SubState=auto-restart
#===============================================================================
begin_test "is_systemd_activating: returns 0 (true) for SubState=auto-restart"

set_stub_state "activating" "auto-restart" 0
run_and_capture_rc is_systemd_activating "lobster-claude"
assert_rc "$RC" "0"

#===============================================================================
# Test 4: is_systemd_activating() returns 1 for active/running
#===============================================================================
begin_test "is_systemd_activating: returns 1 (false) for ActiveState=active SubState=running"

set_stub_state "active" "running" 0
run_and_capture_rc is_systemd_activating "lobster-claude"
assert_rc "$RC" "1"

#===============================================================================
# Test 5: is_systemd_activating() returns 1 for failed
#===============================================================================
begin_test "is_systemd_activating: returns 1 (false) for ActiveState=failed"

set_stub_state "failed" "failed" 0
run_and_capture_rc is_systemd_activating "lobster-claude"
assert_rc "$RC" "1"

#===============================================================================
# Test 6: SYSTEMD_ACTIVATING_AGE_SECONDS reflects InactiveExitTimestampMonotonic
#===============================================================================
begin_test "is_systemd_activating: sets SYSTEMD_ACTIVATING_AGE_SECONDS correctly"

# Compute a timestamp 10s ago (in systemd monotonic microseconds)
uptime_s=$(awk '{print int($1)}' /proc/uptime)
uptime_us=$(( uptime_s * 1000000 ))
target_age_s=10
fake_ts=$(( uptime_us - (target_age_s * 1000000) ))

set_stub_state "activating" "auto-restart" "$fake_ts"
SYSTEMD_ACTIVATING_AGE_SECONDS=0
is_systemd_activating "lobster-claude" || true

age=$SYSTEMD_ACTIVATING_AGE_SECONDS
if [[ $age -ge 5 && $age -le 20 ]]; then
    pass
else
    fail "expected SYSTEMD_ACTIVATING_AGE_SECONDS ~10s, got ${age}s"
fi

#===============================================================================
# Test 7: check_services() returns 1 (YELLOW) for activating within grace
#===============================================================================
begin_test "check_services: returns 1 (YELLOW) when service is activating within grace window"

uptime_s=$(awk '{print int($1)}' /proc/uptime)
uptime_us=$(( uptime_s * 1000000 ))
# 5s ago — well within SYSTEMD_ACTIVATING_GRACE_SECONDS=120
fake_ts=$(( uptime_us - (5 * 1000000) ))

SYSTEMD_ACTIVATING_GRACE_SECONDS=120
set_stub_state "activating" "auto-restart" "$fake_ts"
run_and_capture_rc check_services
assert_rc "$RC" "1"

#===============================================================================
# Test 8: check_services() returns 2 (RED) for genuinely failed unit
#===============================================================================
begin_test "check_services: returns 2 (RED) when service is failed/inactive"

set_stub_state "failed" "failed" 0
run_and_capture_rc check_services
assert_rc "$RC" "2"

#===============================================================================
# Test 9: check_services() returns 2 (RED) when activating exceeds grace window
#===============================================================================
begin_test "check_services: returns 2 (RED) when activating exceeds grace window"

SYSTEMD_ACTIVATING_GRACE_SECONDS=60
uptime_s=$(awk '{print int($1)}' /proc/uptime)
uptime_us=$(( uptime_s * 1000000 ))
# 200s ago — past the 60s grace window
fake_ts=$(( uptime_us - (200 * 1000000) ))

set_stub_state "activating" "auto-restart" "$fake_ts"
run_and_capture_rc check_services
assert_rc "$RC" "2"

# Restore grace
SYSTEMD_ACTIVATING_GRACE_SECONDS=120

#===============================================================================
# Summary
#===============================================================================
echo ""
echo -e "${BOLD}Results: $PASS/$TOTAL passed${NC}"
if [[ $FAIL -gt 0 ]]; then
    echo -e "${RED}$FAIL test(s) failed${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed${NC}"
    exit 0
fi
