"""
Clipboard copy widget component — per-section comment inputs with Copy Comments button.

Extracted from html-interface/STANDARDS.md §5.
Phase 1: replicates existing behavior exactly.

Includes isSecureContext guard + execCommand fallback per STANDARDS.md §13.
"""

COMPONENT_ID = "clipboard-copy-widget"
COMPONENT_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["id", "label"],
            },
        }
    },
    "required": ["sections"],
}

_CSS = """
.comment-widget {
  margin-top: 48px;
  padding: 20px 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.comment-widget-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .09em;
  color: var(--text3);
  margin-bottom: 14px;
}
.comment-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.comment-label {
  font-size: 11px;
  font-weight: 700;
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--text3);
  min-width: 36px;
}
.comment-input {
  flex: 1;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text);
  outline: none;
}
.comment-input:focus { border-color: var(--accent); }
.copy-comments-btn {
  margin-top: 12px;
  padding: 8px 18px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: opacity .15s;
}
.copy-comments-btn:hover { opacity: .85; }
.copy-feedback { margin-top: 8px; font-size: 11px; color: var(--accent3); min-height: 16px; }
"""

_HTML_TEMPLATE = """<!-- COMMENT WIDGET -->
<div class="comment-widget" id="comment-widget">
  <div class="comment-widget-title">Section Comments</div>
  <div class="comment-inputs" id="comment-inputs"></div>
  <button class="copy-comments-btn" onclick="copyComments()">Copy comments</button>
  <div class="copy-feedback" id="copy-feedback"></div>
</div>"""

_JS_TEMPLATE = """
const sections = {sections_json};

(function buildCommentWidget() {{
  const container = document.getElementById('comment-inputs');
  if (!container) return;
  sections.forEach(function(s) {{
    const row = document.createElement('div');
    row.className = 'comment-row';
    row.innerHTML =
      '<span class="comment-label">' + s.label + '</span>' +
      '<input class="comment-input" type="text" id="ci-' + s.id +
      '" placeholder="Comment on ' + s.label + '\\u2026">';
    container.appendChild(row);
  }});
}})();

function copyComments() {{
  const parts = sections
    .map(function(s) {{
      const val = (document.getElementById('ci-' + s.id) || {{}}).value || '';
      return val.trim() ? '[' + s.label + '] ' + val.trim() : null;
    }})
    .filter(Boolean);
  if (!parts.length) {{
    document.getElementById('copy-feedback').textContent = 'Nothing to copy.';
    return;
  }}
  const text = parts.join(' | ');
  if (navigator.clipboard && window.isSecureContext) {{
    navigator.clipboard.writeText(text).then(function() {{
      document.getElementById('copy-feedback').textContent = 'Copied to clipboard.';
      setTimeout(function() {{
        document.getElementById('copy-feedback').textContent = '';
      }}, 2500);
    }});
  }} else {{
    // execCommand fallback for non-secure contexts
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {{
      document.execCommand('copy');
      document.getElementById('copy-feedback').textContent = 'Copied to clipboard.';
      setTimeout(function() {{
        document.getElementById('copy-feedback').textContent = '';
      }}, 2500);
    }} catch (e) {{
      document.getElementById('copy-feedback').textContent = 'Copy failed — please copy manually.';
    }}
    document.body.removeChild(ta);
  }}
}}
"""


def render(config: dict) -> str:
    """
    Returns the clipboard comment widget HTML+CSS+JS fragment.

    Full visual spec: html-interface/STANDARDS.md §5.
    Position: bottom of document body, above footer.

    Includes isSecureContext guard + execCommand fallback for non-HTTPS contexts.
    """
    sections = config["sections"]

    import json

    sections_json = json.dumps(sections)
    js = _JS_TEMPLATE.format(sections_json=sections_json)

    return (
        f"<style>{_CSS}</style>\n"
        f"{_HTML_TEMPLATE}\n"
        f"<script>{js}</script>"
    )
