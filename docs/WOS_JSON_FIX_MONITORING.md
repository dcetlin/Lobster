# WOS JSON Format Resilience - Deployment Monitoring Guide

## Overview

This document describes the monitoring and observability requirements for the WOS JSON format resilience fix (steward.py hardening + prescription_parser.py multi-level fallback).

## What Changed

**Front-end (steward.py):**
- Hardened prescription generation prompt with explicit "JSON ONLY" directive
- Removed markdown code fence suggestions  
- Added JSON structure example and field documentation
- Changed `--output-format text` in claude CLI invocation (no changes to actual flag usage)

**Back-end (prescription_parser.py):**
- New module with 5-level fallback JSON parsing strategy
- Level 0: Strict json.loads() - clean JSON input
- Level 1: Strip markdown code fences - ```json...``` wrapped output
- Level 2: Extract JSON block from prose - regex-based extraction
- Level 3: Extract individual fields from prose - regex field matching  
- Level 4: Deterministic template - sensible default for all failures

**steward.py integration:**
- `_llm_prescribe()` now uses `parse_prescription_json()` instead of inline parsing
- Comprehensive logging at each fallback level
- Schema validation via `validate_prescription_schema()`
- Fallback level tracked in logs for observability

## Expected Behavior

### Success Path (No Fallback)
When the LLM produces clean JSON:
```
INFO _llm_prescribe: LLM prescription generated for <uow_id> 
(model=<model>, estimated_cycles=<cycles>, fallback_level=0)
```

### Fallback Paths (Expected with Claude's output patterns)
When LLM wraps JSON in markdown:
```
INFO parse_prescription_json: Level 1 (strip markdown) succeeded for <uow_id>
INFO _llm_prescribe: LLM prescription generated for <uow_id> 
(model=<model>, estimated_cycles=<cycles>, fallback_level=1)
```

When JSON is embedded in prose:
```
INFO parse_prescription_json: Level 2 (extract JSON block) succeeded for <uow_id>
INFO _llm_prescribe: LLM prescription generated for <uow_id> 
(model=<model>, estimated_cycles=<cycles>, fallback_level=2)
```

When only field fragments are available:
```
INFO parse_prescription_json: Level 3 (extract fields) succeeded for <uow_id>
INFO _llm_prescribe: LLM prescription generated for <uow_id> 
(model=<model>, estimated_cycles=<cycles>, fallback_level=3)
```

### Deterministic Fallback (Level 4)
When parsing completely fails:
```
WARNING parse_prescription_json: All fallback levels failed for <uow_id> — 
returning deterministic template
WARNING _llm_prescribe: prescription parsing failed for <uow_id> — 
all fallback levels exhausted (fallback_level=4), using deterministic template
```

The UoW will receive a generic "perform diagnostic pass" prescription and
advance to the next steward cycle for manual review.

## Monitoring Checklist

### Immediate Post-Deployment (Hour 1)

- [ ] Check steward.log for any prescription parsing failures
- [ ] Look for Level 4 fallback messages - count them
- [ ] Verify no errors in UoW status transitions after _llm_prescribe
- [ ] Check that UoWs with fallback_level=0 (no fallback) are common

### Short-term Monitoring (First 3 Cycles)

Track the following metrics:

1. **Fallback Level Distribution**
   - Count prescriptions at each fallback level (0, 1, 2, 3, 4)
   - Expected: majority at level 0, some at level 1, minimal at level 4
   - WARNING: If >50% at level 3 or 4, prompt improvement needed

2. **UoW Success Rate by Fallback Level**
   - Track which UoWs reached completion with each fallback level
   - Monitor if level 4 fallbacks cause executor confusion
   - Check if level 1/2 fallbacks correlate with executor performance

3. **Specific UoWs to Monitor**
   - Document any UoW that hits level 4 (complete parsing failure)
   - Review the raw LLM output for these cases
   - Compare against expected patterns in test suite

4. **Executor Feedback**
   - Monitor executor logs for "instructions unclear" or similar issues
   - Track if fallback level correlates with executor re-entry
   - Check if deterministic template prescriptions are being re-prescribed correctly

### Example Query (steward.log)

```bash
# Count prescriptions by fallback level
grep "fallback_level=" steward.log | \
  sed 's/.*fallback_level=//' | \
  sed 's/[),].*//' | \
  sort | uniq -c

# Expected output (ideally):
#      50 0
#      15 1
#       3 2
#       1 3
#       0 4
```

### Example Query (executor.log)

```bash
# Find prescriptions that mention "No specific prescription"
# (indicates Level 4 fallback was used)
grep -l "No specific prescription" executor-*.log | wc -l

# Review what the executor did with deterministic prescriptions
grep -A 5 "No specific prescription" executor-*.log | head -20
```

## Key Logging Locations

1. **steward.log** - Main location for prescription generation logs
   - Search: `_llm_prescribe:` for decision points
   - Search: `fallback_level=` to filter by level

2. **prescription_parser.log** (implicit in steward.log)
   - Search: `parse_prescription_json:` for parsing details
   - Shows level-by-level progression

3. **executor logs** - Monitor executor behavior with prescriptions
   - Check if prescriptions are clear and actionable
   - Look for "instructions unclear" or similar feedback

## Success Criteria

**Deployment is successful if:**
1. All 53 unit/integration tests pass
2. First 3 cycles show fallback distribution expected (see above)
3. No executor failures due to malformed instructions
4. Most UoWs (>80%) processed at fallback_level=0 or 1
5. Level 4 occurrences are rare (<2 per cycle) and documented

**Rollback trigger:**
- If >30% of prescriptions hit Level 4
- If executor success rate drops >10% vs baseline
- If any UoW cycles indefinitely due to bad prescriptions

## Operational Notes

### If High Level 4 Fallback Rate Observed

1. Check if LLM model changed or parameters shifted
2. Review raw LLM output samples for new patterns
3. Add pattern-specific fallback level if needed (e.g., Level 2.5 for common prose pattern)
4. Consider re-running prompt hardening with stronger directives

### If Executor Performance Degrades

1. Compare prescriptions from level 0 vs level 4 for quality differences
2. Check if deterministic template is too generic
3. Review executor logs for "instructions unclear" patterns
4. Consider tighter constraints in deterministic template

### Tuning Opportunities

- **Prompt tuning:** If Level 1 common, strengthen "no markdown" directive
- **Parser tuning:** If specific prose pattern appears frequently, add Level X for it
- **Fallback tuning:** If level 4 deterministic template causes issues, customize per UoW type

## Maintenance

After monitoring period (3+ cycles):

1. Document actual fallback distribution vs expected
2. Note any UoW types that consistently hit specific fallback levels
3. Consider pattern-specific handlers for top 2-3 unexpected patterns
4. Update this document with real-world findings
