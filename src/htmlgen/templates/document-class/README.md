# document-class template

Use this template for narrative-primary documents: design docs, proposals, audits,
assessments, and synthesis documents. This is the default template — use it when no
other template clearly fits.

**What it produces:** A clean, readable HTML document with a fixed header, section
labels (§1, §2, ...), a dark/light theme toggle, and a clipboard comment widget at
the bottom for leaving section-scoped comments.

**When to use:**
- The document is primarily prose and will be read linearly
- The document may be reviewed by Dan with inline comments (clipboard widget)
- Examples: design doc, proposal, audit, retro, briefing, research summary

**When not to use:**
- The document is data-driven and regenerated frequently → use `dashboard-class`
- The document is a long-lived spec edited across many sessions with section coverage
  tracking → use `spec-document`

**Content format:** Markdown with section markers (`<!-- section: sN -->`).
See `content-scaffold.md` for the starting structure.

**Components included automatically:**
- `theme-toggle` — dark/light mode toggle, top-right, localStorage persistence
- `clipboard-copy-widget` — per-section text inputs + copy assembles `[§N] text | ...`
