# dashboard-class template

Use this template for data-driven documents that are regenerated frequently and
include interactive elements: status pages, operational views.

**What it produces:** A compact HTML dashboard with automatic dark/light theme (via
`@media prefers-color-scheme` — no toggle button), optional filter bar, and a
structured layout optimized for dense data presentation.

**When to use:**
- The document is regenerated frequently (every few minutes to hours)
- The document has interactive elements: filter dropdowns, status toggles, action buttons
- Examples: agent session view, health report, operational status page

**When not to use:**
- The document is primarily prose read linearly → use `document-class`
- The document is a long-lived spec with section tracking → use `spec-document`

**Content format:** JSON content manifest. See `content-scaffold.json` for the
starting structure.

**Components included automatically:**
- `theme-toggle` — `mode: media-query` (respects OS preference, no button shown)
- `dashboard-filter-bar` — status filter dropdown + stalled-only checkbox (optional)

**Note on theme toggle:** Dashboard-class uses `@media prefers-color-scheme` rather
than the JS toggle button. This is intentional: dashboards are viewed in operational
contexts where the user's OS theme is the appropriate control. The JS toggle is for
reviewed documents that Dan reads in a specific environment.
