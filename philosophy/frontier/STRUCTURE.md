# Frontier Directory Structure

`philosophy/frontier/` holds three kinds of files. The naming convention encodes the kind.

## Kinds

**Living documents** — evolving frameworks and active conceptual work. No date prefix. Named for the concept, not the session. These accumulate revisions over time.

Examples: `system-metabolism.md`, `orient.md`, `registers.md`, `wos-v3-convergence.md`

**Point-in-time logs / retros** — session records, assessments, snapshots. Date-prefixed (`YYYY-MM-DD-`). Never edited after writing.

Examples: `ooda-retro-2026-04-16.md`

**Canonical tools / templates** — stable, reusable operational artifacts. Prefixed with their role or named as `ALLCAPS.md` for index/reference cards. No date prefix.

Examples: `horizonal-kickoff-template.md`, `STRUCTURE.md`

## Rules

- Living doc → no date prefix, noun phrase name
- Log or retro → `YYYY-MM-DD-` prefix, imperative or event description
- Template or tool → descriptive noun, no date, stable name
- No subdirectories unless count exceeds ~20 files per kind
- If subdirs become necessary: `logs/`, `living/`, `tools/` — nothing else
- One file per concept; extend in place rather than versioning by filename

## Current anomalies (as of 2026-04-16)

The holographic-epistemology cluster (`holographic-epistemology.md`, `-challenge.md`, `-synthesis.md`, `-systems-alignment.md`) is a living-doc series that predates this convention. Treat as living docs; no rename needed.
