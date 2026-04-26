# Oracle Process Reference

Canonical named processes for the oracle review pipeline.
Read-only for agents; updated via oracle decisions or human edit.

---

## Resolution-by-Absence

### Definition

Resolution-by-absence occurs when a NEEDS_CHANGES gap appears to close in a subsequent PR not because the underlying problem was fixed, but because the artifact that triggered the gap (a file, a symbol, a behavioral implication) was removed from the PR scope.

The gap is deferred, not closed.

### Why It Matters

A gap resolved by absence can re-fire on any follow-on PR that reintroduces the same artifact. Oracle learnings.md has two entries on this pattern firing in the same session (PR #882/#884 and PR #896). When the artifact is reintroduced in a later PR, the oracle agent reviewing that PR may not connect it to the prior gap unless the verdict history is consulted explicitly.

The risk is not the re-firing itself — that is expected — but that the oracle agent treats the new PR as a fresh evaluation, missing the accumulated context about why the artifact was problematic.

### Handling Protocol

When a fix agent resolves a gap by removing an artifact from a PR rather than fixing the underlying issue:

1. **Verify the removal is intentional.** Confirm with the engineer's commit message or PR description that the artifact was explicitly scoped out, not accidentally deleted.

2. **Mark the verdict explicitly.** In `oracle/verdicts/pr-{number}.md`, note the gap as deferred:

   ```
   GAP DEFERRED: <gap name>
   Reason: artifact <path/symbol> was removed from PR scope rather than fixed.
   Follow-on: any PR reintroducing <path/symbol> must re-check this gap before review.
   ```

3. **Do not mark it APPROVED on that gap.** A deferred gap is not an approved gap. The verdict can still be APPROVED if the remaining (non-deferred) gaps are resolved and the scope reduction is acceptable, but the deferral must be explicit in the verdict file.

4. **Log the deferred gap in oracle/learnings.md** under the PR number, citing the artifact and the follow-on condition.

### Resolution-by-Verification (Named Variant)

Resolution-by-absence should not be confused with resolution-by-verification.

Resolution-by-verification occurs when an oracle fix agent discovers that the flagged symbol or constant actually does exist in the codebase — i.e., the oracle's "absence" assumption was wrong. The gap resolves not by code change but by verification that the artifact is present.

Protocol for resolution-by-verification:

1. **Verify presence directly.** Check the actual file, not the diff. The oracle may have flagged absence based on diff context that excluded the surrounding file content.

2. **Update the verdict to reflect the finding.** The verdict entry for that gap should note:

   ```
   GAP RESOLVED-BY-VERIFICATION: <gap name>
   Finding: <symbol/path> exists at <location>. Absence was not confirmed in diff context.
   No code change required.
   ```

3. **Do not make spurious changes.** A fix agent that cannot find the gap to fix should not make a substitution change to satisfy the oracle round. If the symbol exists and the gap was based on incorrect absence detection, the correct action is to update the verdict and close the gap — not to introduce a new symbol or modify working code.

4. **Flag the oracle detection miss.** If resolution-by-verification occurs, the original oracle detection was a false positive. Note this in oracle/learnings.md so the detection pattern can be refined.

---

## See Also

- `oracle/learnings.md` — PR #882, #884, #896 entries on resolution-by-absence
- `oracle/patterns.md` — infrastructure-vs-execution discriminator (related: what counts as resolution vs. deferral)
