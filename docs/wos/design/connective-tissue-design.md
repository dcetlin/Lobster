# Connective Tissue Design

*Status: Design — 2026-05-22*
*WOS-UoW: uow_20260522_831912*

---

## Problem statement

Workers (subagents) regularly create and modify design documents, memory files, and frontier docs. When a worker encounters a concept that is semantically related to work it is doing in another document, there is currently no way to record that relationship. The result is a system where documents are individually coherent but structurally isolated: a reader arriving at one document has no way to discover that a directly relevant document exists two directories away. The goal is passive, low-maintenance connective tissue — lateral links that accumulate through normal work without requiring dedicated infrastructure or adding mandatory steps to every session.

---

## Options considered

**Convention** — Workers add a `## See Also` section (or append to an existing one) to any document they touch when they notice a semantic relationship to another document. The link is inline markdown, the section is standard, and the protocol fires only on notice — not on every touch. Worker burden is near-zero (adding two lines to a document already open). Staleness risk is non-trivial: if document B moves, the link in A rots silently. Discoverability is one-directional — a reader at A sees the link to B, but a reader arriving at B does not know A links to it. Bloat profile is benign: links accumulate as section content, no shared resource grows.

**Structural** — A dedicated flat index file (e.g. `docs/wos/links.md`) where workers append a one-liner whenever they notice a lateral relationship. Discoverability improves: the index can be queried without visiting every document. But worker burden increases — every encounter requires a write to a shared file, creating a merge-conflict surface if multiple workers run concurrently. Staleness risk persists; the index becomes a maintenance object that needs periodic pruning as documents move. Bloat profile is worse: the index grows monotonically and has no natural cleanup path.

**Extension** — Extend the frontier router (`src/harvest/frontier_router.py`) to support lateral links alongside its existing forward-routing behavior. The frontier router is purpose-built for philosophy session output → domain frontier documents; extending it to handle lateral links between WOS design docs, memory files, and other registers is a category mismatch. Worker burden increases (workers must explicitly invoke the router for non-frontier material). Staleness and bloat profiles are inherited from the frontier doc pattern. Discoverability is limited to whatever frontier files the lateral links land in. The extension creates coupling between distinct routing concerns.

---

## Recommendation

Use convention: workers add a `## See Also` section to documents they are already modifying when they notice a lateral relationship.

**Minimal worker protocol:**

When a worker is creating or substantially editing a document and notices that an existing document is semantically related to the content it is writing, it appends the following to the target document:

```markdown
## See Also

- [Short description of the relationship](../relative/path/to/related-doc.md)
```

If a `## See Also` section already exists, append to it rather than creating a second one.

**When this fires:** Only when the worker explicitly recognizes a semantic relationship — a shared concept, a direct dependency, a design decision that another document extends or constrains. It does not fire on every document touch. It does not fire for coincidental proximity (two docs in the same directory). The worker's judgment is the gate; no automated trigger.

**What file gets modified:** The document the worker is currently creating or editing. The link points from the current document to the related one using a relative path. The worker does not need to modify the related document unless it is already open for other reasons.

**Syntax:** Standard markdown relative link. Prefer relative paths over absolute paths so links survive directory structure moves that preserve relative positions. One line per related document; no nesting.

---

## What is out of scope

- Reverse-traversal indexing (finding all documents that link to a given document). This requires a structural index, which is out of scope for the convention approach.
- Automated link validation or staleness detection. Links may rot if documents move; that is an accepted tradeoff for the convention approach's zero infrastructure cost.
- Mandatory linking on every document touch. The protocol is passive — it fires only on explicit worker notice, not as a required step.
- Linking between registers that do not share semantic content (e.g., linking a WOS design doc to an unrelated frontier doc because they happen to be contemporaneous).
- A graph database, a link registry, or any queryable index of lateral relationships.
- Retroactive linking. Existing documents are not swept for unlabeled relationships; links accumulate forward from the point this protocol is in use.

---

## Open questions

1. **Relative path fragility:** If documents are reorganized (e.g., design docs move from `docs/wos/design/` to `docs/design/`), relative links break silently. Is there a lightweight path-aliasing convention (e.g., a slug in the document header) that would make links more rename-stable without adding infrastructure?

2. **Bidirectional linking:** The convention as specified is one-directional — a worker at document A records the link to B, but B does not know A links to it. For the most important relationships (e.g., a spec and its implementation design), should the protocol call for adding links in both documents when the worker has both open?
