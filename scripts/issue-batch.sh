#!/usr/bin/env bash
# issue-batch.sh — Sweep-based lifecycle batch readout for dcetlin/Lobster
#
# Produces a structured view of the current issue queue across lifecycle states.
# Use this to answer: what can be acted on right now, and what requires a human
# decision before anything moves?
#
# Usage:
#   ./scripts/issue-batch.sh [--repo OWNER/REPO] [--json]
#
# Output modes:
#   (default)  Human-readable batch readout
#   --json     Machine-readable JSON (useful for downstream tooling or weekly synthesis)
#
# Requires: gh CLI authenticated, jq

set -euo pipefail

REPO="${LOBSTER_ISSUE_REPO:-dcetlin/Lobster}"
OUTPUT_JSON=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --json) OUTPUT_JSON=true; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Fetch all open issues with full label data in one call
ALL_ISSUES=$(gh issue list \
  --repo "$REPO" \
  --state open \
  --limit 200 \
  --json number,title,labels,body,createdAt \
)

# Pure label-predicate functions (return 0 if issue has label, 1 otherwise)
has_label() {
  local issue="$1" label="$2"
  echo "$issue" | jq -e --arg l "$label" \
    '.labels | map(.name) | contains([$l])' > /dev/null 2>&1
}

# Extract the 'enables' list from issue body metadata field
# Parses: "enables: [#X, #Y]" or "enables: <!-- ... -->" (treats comment as empty)
extract_enables_count() {
  local body="$1"
  echo "$body" \
    | grep -Eo '^enables:[[:space:]]*\[.*\]' 2>/dev/null \
    | grep -Eo '#[0-9]+' \
    | wc -l \
    | tr -d ' '
}

# Classify each issue into one of the batch buckets.
# Returns a JSON object with bucket membership and metadata.
classify_issue() {
  local issue="$1"
  local number title body enables_count
  number=$(echo "$issue" | jq -r '.number')
  title=$(echo "$issue" | jq -r '.title')
  body=$(echo "$issue" | jq -r '.body // ""')
  enables_count=$(extract_enables_count "$body")

  # Dependency check: extract depends-on references and check if any are still open
  local depends_on_open=false
  local depends_raw
  depends_raw=$(echo "$body" | grep -Eo '^depends-on:[[:space:]]*\[.*\]' 2>/dev/null | grep -Eo '#[0-9]+' || true)
  if [[ -n "$depends_raw" ]]; then
    for dep_ref in $depends_raw; do
      dep_num="${dep_ref#\#}"
      dep_state=$(echo "$ALL_ISSUES" | jq -r --argjson n "$dep_num" \
        '.[] | select(.number == $n) | "open"' 2>/dev/null || echo "")
      # If the dep appears in open issues list, it's unresolved
      dep_in_open=$(echo "$ALL_ISSUES" | jq -r --argjson n "$dep_num" \
        'map(select(.number == $n)) | length' 2>/dev/null || echo "0")
      if [[ "$dep_in_open" -gt 0 ]]; then
        depends_on_open=true
        break
      fi
    done
  fi

  # Determine primary bucket
  local bucket=""

  if has_label "$issue" "blocked-on-dan"; then
    bucket="blocked-on-dan"
  elif has_label "$issue" "ready-to-execute" && [[ "$depends_on_open" == "false" ]]; then
    bucket="executable-now"
  elif has_label "$issue" "ready-to-execute" && [[ "$depends_on_open" == "true" ]]; then
    bucket="blocked-by-dep"
  elif has_label "$issue" "needs-further-design" || has_label "$issue" "needs-design"; then
    if has_label "$issue" "action:iterate-design" || \
       has_label "$issue" "action:challenge-design" || \
       ! has_label "$issue" "action:design-conversation"; then
      bucket="in-design"
    else
      bucket="design-conversation"
    fi
  elif has_label "$issue" "action:design-conversation"; then
    bucket="design-conversation"
  elif has_label "$issue" "needs-agent-posture"; then
    bucket="needs-posture"
  elif has_label "$issue" "auditing"; then
    bucket="auditing"
  elif has_label "$issue" "design-seed"; then
    bucket="design-seed"
  else
    bucket="unclassified"
  fi

  # Collect action type
  local action_type=""
  for at in "action:iterate-design" "action:challenge-design" "action:implement" \
            "action:experiment" "action:design-conversation"; do
    if has_label "$issue" "$at"; then
      action_type="${at#action:}"
      break
    fi
  done

  jq -n \
    --argjson num "$number" \
    --arg title "$title" \
    --arg bucket "$bucket" \
    --arg action_type "$action_type" \
    --argjson enables "$enables_count" \
    --argjson depends_blocked "$([ "$depends_on_open" = true ] && echo 'true' || echo 'false')" \
    '{number: $num, title: $title, bucket: $bucket, action_type: $action_type,
      enables: $enables, depends_blocked: $depends_blocked}'
}

# Build classified list
CLASSIFIED=$(echo "$ALL_ISSUES" | jq -c '.[]' | while IFS= read -r issue; do
  classify_issue "$issue"
done | jq -s '.')

if [[ "$OUTPUT_JSON" == "true" ]]; then
  echo "$CLASSIFIED"
  exit 0
fi

# ── Human-readable readout ────────────────────────────────────────────────────

print_bucket() {
  local label="$1" bucket="$2"
  local items
  items=$(echo "$CLASSIFIED" | jq -r \
    --arg b "$bucket" \
    '.[] | select(.bucket == $b) | "  #\(.number)  \(.title)"')
  local count
  count=$(echo "$CLASSIFIED" | jq --arg b "$bucket" '[.[] | select(.bucket == $b)] | length')
  echo ""
  echo "$label ($count)"
  if [[ -n "$items" ]]; then
    echo "$items"
  else
    echo "  (none)"
  fi
}

HIGH_LEVERAGE=$(echo "$CLASSIFIED" | jq -r \
  '[.[] | select(.enables >= 3)] | sort_by(-.enables)[] |
   "  #\(.number) [enables \(.enables)]  \(.title)"')
HIGH_LEVERAGE_COUNT=$(echo "$CLASSIFIED" | jq '[.[] | select(.enables >= 3)] | length')

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Lobster Issue Batch Readout  —  $(date '+%Y-%m-%d')"
echo " Repo: $REPO"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

print_bucket "Executable now (ready-to-execute, unblocked)" "executable-now"
print_bucket "In design iteration (can run now, no blocker)" "in-design"
print_bucket "Needs design conversation with Dan" "design-conversation"
print_bucket "Needs agent posture (specified in issue)" "needs-posture"
print_bucket "Blocked by dependency" "blocked-by-dep"
print_bucket "Blocked on Dan" "blocked-on-dan"
print_bucket "Under audit" "auditing"
print_bucket "Design seed (not yet shaped)" "design-seed"
print_bucket "Unclassified (no lifecycle labels)" "unclassified"

echo ""
echo "High upstream leverage (enables >= 3 downstream issues): $HIGH_LEVERAGE_COUNT"
if [[ -n "$HIGH_LEVERAGE" ]]; then
  echo "$HIGH_LEVERAGE"
else
  echo "  (none tagged yet)"
fi

echo ""
TOTAL=$(echo "$CLASSIFIED" | jq 'length')
echo "Total open issues scanned: $TOTAL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
