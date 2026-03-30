# Connective Tissue Linker — Design

*Status: DRAFT — Phase 2 WOS Sprint, Item 20*
*Written: 2026-03-30*

---

## 1. Purpose

Workers processing documents — philosophy sessions, frontier docs, sweep reports, UoW outputs — routinely encounter concepts that bear on other documents in the corpus. Today, those relationships are lost. A worker may observe that a philosophy session directly elaborates a frontier doc, or that a sweep report contradicts an earlier design decision, but has no channel to record that observation. The connective tissue linker gives workers a structured interface to record cross-document relationships, and gives Dan a way to surface them.

This is an append-only, low-ceremony system. Workers call one function. Links accumulate in a flat log. A periodic report surfaces clusters of related documents.

---

## 2. Worker Interface

```python
def record_link(
    source_path: str,
    target_path: str,
    link_type: str,
    rationale: str,
    worker_id: str | None = None,
) -> str:
    """
    Record a directional cross-document link.

    Args:
        source_path: Relative path to the source document (e.g. "sessions/2026-03-29.md")
        target_path: Relative path to the target document (e.g. "frontier-docs/agency-architecture.md")
        link_type:   One of: references, elaborates, contradicts, supersedes, synthesizes
        rationale:   1–3 sentences. Why does this link exist? What is the relationship?
        worker_id:   Optional. Identifies the calling worker (e.g. "cultivator", "steward").

    Returns:
        link_id: A unique string ID for the link (UUID or timestamp-prefixed slug).
    """
```

### Design constraints on the interface

- **No blocking I/O requirement.** The worker appends to a file and returns. No database connection, no network call.
- **Paths are corpus-relative.** Both `source_path` and `target_path` are relative to the lobster-user-config corpus root (`~/lobster-user-config/`). Workers do not pass absolute paths. If a document does not yet exist at `target_path`, the link is still recorded — the system tolerates forward references.
- **Rationale is mandatory and human-readable.** A link with no rationale is not recorded. Workers must articulate the relationship in prose, not just label it.
- **Link direction is intentional.** `source_path` is the document the worker was processing when the link was discovered. `target_path` is the document being pointed at. The graph is directed.

---

## 3. Link Types

| Type | Meaning | Example |
|------|---------|---------|
| `references` | Source cites or quotes target as evidence or background | A sweep report references an engineering-principles doc |
| `elaborates` | Source expands on a concept introduced in target | A frontier doc elaborates a pearl from a philosophy session |
| `contradicts` | Source and target make incompatible claims | Two design docs prescribe conflicting routing behaviors |
| `supersedes` | Source replaces or invalidates target | A v2 design supersedes the v1 design |
| `synthesizes` | Source integrates or reconciles multiple targets | A canonical-templates update synthesizes two competing proposals |

`synthesizes` is a multi-target link type. When a worker synthesizes more than one source, they record one link per source-to-target pair, all with type `synthesizes`. The rationale on each link should reference the synthesis context.

---

## 4. Storage

Links are stored in a single append-only JSONL file:

```
~/lobster-user-config/links/cross-doc-links.jsonl
```

One JSON object per line. Schema:

```json
{
  "link_id": "20260330T142301-7f3a",
  "recorded_at": "2026-03-30T14:23:01Z",
  "source_path": "sessions/2026-03-29-philosophy.md",
  "target_path": "frontier-docs/agency-architecture.md",
  "link_type": "elaborates",
  "rationale": "The session's discussion of distributed cognition directly elaborates the agency-architecture doc's open question about multi-agent epistemics.",
  "worker_id": "cultivator"
}
```

### Why JSONL, not SQLite

- Workers do not need to read the link store — only append to it.
- The surfacing agent (see Section 6) is the only reader; it runs periodically, not on the hot path.
- JSONL is crash-safe for appends. A write failure leaves the file in a valid state.
- No migration tooling required. Schema additions are backward-compatible by default.
- The file can be `cat`-inspected, `grep`-searched, and version-controlled without tooling.

SQLite is the right answer if link queries become complex (e.g., multi-hop traversal, full-text search on rationale). The JSONL format is designed to be migrated to SQLite without loss — every field maps directly to a column.

### File management

- The `links/` directory is created on first write if it does not exist.
- Rotation: when the file exceeds 10,000 lines, the writer renames it to `cross-doc-links.YYYYMM.jsonl` and starts a new `cross-doc-links.jsonl`. The surfacing agent reads all files matching `cross-doc-links*.jsonl`.

---

## 5. Implementation Module

```
~/lobster/agents/connective_tissue.py
```

Public API:
- `record_link(source_path, target_path, link_type, rationale, worker_id=None) -> str`
- `load_links(since: datetime | None = None) -> list[dict]`

`load_links` is for the surfacing agent only. Workers call only `record_link`.

---

## 6. Surfacing Discovered Connections Back to Dan

The linker accumulates silently. Surfacing is a scheduled job that runs on the digest cycle (or on demand).

### Surfacing agent behavior

1. Read all links recorded since the last surfacing run (or last 30 days if no prior run).
2. Group by `target_path` — documents with multiple inbound links are likely conceptually central.
3. Identify `contradicts` links — these always surface regardless of count.
4. Identify documents appearing in both `source_path` and `target_path` fields — these are network hubs.
5. Generate a prose summary: "3 documents link to `frontier-docs/agency-architecture.md`. One link is a `contradicts` from a sweep report (see rationale). Two are `elaborates` from recent philosophy sessions."
6. Send to Dan via `send_reply` with the summary and a list of high-signal links (contradicts first, then hubs sorted by inbound count).

### Trigger conditions

- **Scheduled:** Runs as part of the weekly digest job.
- **On demand:** Dan can request a link report at any time via `lobster links` or similar.
- **Threshold trigger:** If 5 or more new `contradicts` links accumulate since the last surfacing run, the surfacing agent fires immediately without waiting for the scheduled cycle.

### Format

```
Link report — 2026-03-30

Hub documents (3+ inbound links):
  frontier-docs/agency-architecture.md — 4 inbound (2 elaborates, 1 contradicts, 1 references)

Contradictions requiring attention:
  sweep-reports/2026-03-28.md → contradicts → design/wos-v2-design.md
  Rationale: "The sweep report's routing prescription conflicts with the v2 design's configuration-rules-routing principle."

Recent synthesis:
  sessions/2026-03-29-philosophy.md → synthesizes → frontier-docs/agency-architecture.md
  Rationale: "Session reconciled the distributed cognition framing with the executor contract."
```

---

## 7. Integration Points

Workers that should call `record_link`:

| Worker | When to call |
|--------|-------------|
| Cultivator | After classifying a philosophy session: link session to any frontier doc it elaborates |
| Steward | When diagnosing a UoW: link sweep report to any design doc it references |
| Sweep agents | When a sweep output explicitly contradicts or supersedes an existing doc |
| Oracle reviewer | When a PR review identifies a contradiction between PR content and an existing design doc |

Workers are not required to call `record_link`. It is an optional enrichment, not a gate. Missing links degrade surfacing quality; they do not break the pipeline.

---

## 8. Open Questions (not blocking Phase 2)

- **Bi-directional query:** Should `load_links` support reverse lookup (what does document X link to, and what links to X)? Deferred — the surfacing agent's current grouping handles this implicitly.
- **Link confidence:** Should workers be able to express confidence in a link (definite vs. tentative)? Deferred — rationale text handles this adequately for now.
- **UI:** Should link clusters be visualizable as a graph? Deferred — the prose summary is sufficient for Phase 2.
