# spec-document template

Use this template for long-lived architecture specs, system design documents, and
evolution plans that will be edited across many sessions with formal section coverage
tracking.

**What it produces:** A structured HTML document with a fixed header, section labels
(§1, §2, ...), a dark/light theme toggle, and a clipboard comment widget. The content
format is JSON, which enables precise machine-readable section tracking and addressed-
state coverage checks.

**When to use:**
- The document is long-lived and will be edited across many sessions
- The document has formal section coverage tracking (`addressed` flags per section)
- Section IDs must be stable across all renders (they are never renumbered)
- The document is an architecture spec or system design document
- Examples: html-doc-model-spec, implementation plans

**When not to use:**
- The document is primarily narrative and won't be tracked for section coverage
  → use `document-class`
- The document is a dashboard or data-driven operational view → use `dashboard-class`

**Content format:** JSON content manifest (root object contains the manifest schema
from §7 of html-doc-model-spec.md). See `content-scaffold.json` for the starting
structure.

**Section ID stability rule:** IDs in the manifest are permanent. A section removed
from the document retires its ID — that ID is never reassigned to a new section.
This ensures inbound links to specific sections (e.g., from Telegram messages) remain
valid across the document's lifetime.

**Components included automatically:**
- `theme-toggle` — dark/light mode toggle, top-right, localStorage persistence
- `clipboard-copy-widget` — per-section text inputs for leaving structured comments
