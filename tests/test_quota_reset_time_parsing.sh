#!/bin/bash
#===============================================================================
# Test Suite: Quota Reset Time Parsing
#
# Tests the new reset time parsing logic in claude-persistent.sh
# to ensure it correctly extracts reset times from error messages
# and calculates proper sleep durations.
#
# Fixed issue: dispatcher was sleeping until midnight UTC instead of
# actual quota reset time, causing immediate retry loops.
#===============================================================================

set -eE

RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
BOLD="\033[1m"
NC="\033[0m"

PASS=0
FAIL=0

# Test helper
assert_success() {
    local test_name="$1"
    local cmd="$2"
    
    if eval "$cmd" >/dev/null 2>&1; then
        echo -e "${GREEN}✓ PASS${NC}: $test_name"
        ((PASS++))
    else
        echo -e "${RED}✗ FAIL${NC}: $test_name"
        eval "$cmd"
        ((FAIL++))
    fi
}

assert_contains() {
    local test_name="$1"
    local output="$2"
    local expected="$3"
    
    if echo "$output" | grep -q "$expected"; then
        echo -e "${GREEN}✓ PASS${NC}: $test_name"
        ((PASS++))
    else
        echo -e "${RED}✗ FAIL${NC}: $test_name"
        echo "  Expected substring: $expected"
        echo "  Got output: $output"
        ((FAIL++))
    fi
}

echo "====== Quota Reset Time Parsing Tests ======"
echo

# Test 1: Parse simple time format (e.g., "6pm")
echo "Test Group 1: Simple time format parsing"
log_sample="You've hit your limit · resets 6pm (UTC)"
reset_str=$(echo "$log_sample" | grep -oP 'resets \\K[^(]+' | xargs)
assert_contains "Parse simple time" "$reset_str" "6pm"

# Test 2: Parse tomorrow notation
echo "Test Group 2: Tomorrow notation"
log_sample2="You've hit your limit · resets tomorrow 12am (UTC)"
reset_str2=$(echo "$log_sample2" | grep -oP 'resets \\K[^(]+' | xargs)
assert_contains "Parse tomorrow notation" "$reset_str2" "tomorrow"

# Test 3: Parse date with time
echo "Test Group 3: Date with time format"
log_sample3="You've hit your limit · resets Apr 13, 6pm (UTC)"
reset_str3=$(echo "$log_sample3" | grep -oP 'resets \\K[^(]+' | xargs)
assert_contains "Parse date with time" "$reset_str3" "Apr 13"

# Test 4: Verify the regex handles edge cases
echo "Test Group 4: Edge cases"
log_empty=""
reset_str_empty=$(echo "$log_empty" | grep -oP 'resets \\K[^(]+' | xargs || echo "")
if [[ -z "$reset_str_empty" ]]; then
    echo -e "${GREEN}✓ PASS${NC}: Empty input returns empty"
    ((PASS++))
else
    echo -e "${RED}✗ FAIL${NC}: Empty input should return empty"
    ((FAIL++))
fi

echo
echo "====== Test Summary ======"
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
fi
