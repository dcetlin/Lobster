"""
Theme toggle component — light/dark mode toggle with localStorage persistence.

Extracted from html-interface/STANDARDS.md §3.
Phase 1: replicates existing behavior exactly.
"""

COMPONENT_ID = "theme-toggle"
COMPONENT_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["js-toggle", "media-query"],
            "default": "js-toggle",
        }
    },
}

_JS_TOGGLE_CSS = """
#theme-toggle {
  position: fixed;
  top: 14px;
  right: 18px;
  z-index: 1000;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text2);
  cursor: pointer;
  transition: background .15s, color .15s;
}
#theme-toggle:hover { background: var(--surface3); color: var(--text); }
"""

_JS_TOGGLE_HTML = '<button id="theme-toggle" onclick="toggleTheme()">&#9788; Light</button>'

_JS_TOGGLE_JS = """
function toggleTheme() {
  const isLight = document.body.classList.toggle('light-mode');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').textContent = isLight ? '\\u263e Dark' : '\\u2600 Light';
}
(function() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light-mode');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '\\u263e Dark';
  }
})();
"""

_MEDIA_QUERY_CSS = """
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f8f9fb;
    --surface: #ffffff;
    --surface2: #f0f2f7;
    --surface3: #e8ecf4;
    --border: #dde1ee;
    --border2: #c8cedf;
    --text: #1a1d2e;
    --text2: #4a5068;
    --text3: #8890a8;
    --accent: #2563eb;
    --accent2: #7c3aed;
    --accent3: #059669;
    --warn: #ea580c;
  }
}
"""


def render(config: dict) -> str:
    """
    Returns the dark/light mode toggle HTML+CSS+JS fragment.

    mode="js-toggle": fixed button, top-right, localStorage persistence.
    mode="media-query": @media prefers-color-scheme only, no button.

    Full spec: html-interface/STANDARDS.md §3.
    Position: injected as first child of <body>.
    """
    mode = config.get("mode", "js-toggle")

    if mode == "media-query":
        return f"<style>{_MEDIA_QUERY_CSS}</style>"

    # Default: js-toggle
    return (
        f"<style>{_JS_TOGGLE_CSS}</style>\n"
        f"{_JS_TOGGLE_HTML}\n"
        f"<script>{_JS_TOGGLE_JS}</script>"
    )
