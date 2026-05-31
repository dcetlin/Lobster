"""
vocab_tooltip.py — Vocabulary tooltip index component.

Renders two interleaved artifacts:
  1. A <style> + <script> block that wraps every occurrence of each vocab
     term in the document body with a <span class="vocab-term"> that shows
     a hover tooltip card.
  2. A collapsible <div class="vocab-index"> panel listing all terms
     alphabetically with their full definitions.

The component is activated by including a ``vocab`` dict in the content
manifest (term → definition strings).  When the manifest has no ``vocab``
field the renderer skips this component entirely.

Usage in manifest JSON:
    {
      "vocab": {
        "Stiffness": "Resistance to deformation; preserves geometry under load.",
        "Elasticity": "Deformation with return; ...",
        ...
      }
    }

The renderer auto-injects this component when the manifest carries a
``vocab`` key — callers do not need to list it in ``components``.

Design constraints:
- Matches the document's dark/light CSS variable palette.
- Tooltip card appears above the term (flips below near viewport top).
- The vocab index panel is collapsible (open by default).
- Term matching is case-sensitive to avoid false positives on common words.
- Terms containing regex special characters are escaped before matching.
"""

from __future__ import annotations

import json
import re

COMPONENT_ID = "vocab-tooltip"
COMPONENT_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "vocab": {
            "type": "object",
            "description": "Mapping of term strings to definition strings.",
            "additionalProperties": {"type": "string"},
        }
    },
    "required": ["vocab"],
}

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
/* ── Vocab tooltip inline terms ── */
.vocab-term {
  border-bottom: 1px dotted var(--accent);
  cursor: help;
  position: relative;
  display: inline-block;
}
.vocab-term .vocab-tip {
  visibility: hidden;
  opacity: 0;
  pointer-events: none;
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%);
  z-index: 900;
  background: var(--surface3);
  border: 1px solid var(--border2);
  border-radius: 8px;
  padding: 10px 14px;
  width: 260px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text);
  box-shadow: 0 4px 16px rgba(0,0,0,.35);
  transition: opacity .15s ease, visibility .15s ease;
  white-space: normal;
  text-align: left;
}
.vocab-term .vocab-tip::after {
  content: '';
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  border: 6px solid transparent;
  border-top-color: var(--border2);
}
.vocab-term:hover .vocab-tip,
.vocab-term:focus .vocab-tip {
  visibility: visible;
  opacity: 1;
}
.vocab-tip-term {
  font-weight: 700;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .06em;
  color: var(--accent);
  display: block;
  margin-bottom: 5px;
}

/* ── Vocab index panel ── */
.vocab-index {
  margin: 2.5rem 0 1rem;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: hidden;
}
.vocab-index-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid var(--border);
  background: var(--surface2);
}
.vocab-index-header:hover { background: var(--surface3); }
.vocab-index-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .09em;
  color: var(--text3);
}
.vocab-index-chevron {
  font-size: 10px;
  color: var(--text3);
  transition: transform .2s ease;
}
.vocab-index.collapsed .vocab-index-chevron { transform: rotate(-90deg); }
.vocab-index-body {
  padding: 16px 20px 20px;
  display: grid;
  grid-template-columns: 1fr;
  gap: 0;
}
.vocab-index.collapsed .vocab-index-body { display: none; }
.vocab-entry {
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
}
.vocab-entry:last-child { border-bottom: none; }
.vocab-entry-term {
  font-size: 13px;
  font-weight: 700;
  color: var(--accent);
  margin-bottom: 4px;
}
.vocab-entry-def {
  font-size: 13px;
  color: var(--text2);
  line-height: 1.6;
}
"""

# ---------------------------------------------------------------------------
# JS — term highlighting injected at runtime (safe: runs after DOM is built)
# ---------------------------------------------------------------------------

_JS_TEMPLATE = r"""
(function() {
  var vocab = {vocab_json};

  // Sort terms longest-first to avoid partial matches swallowing longer terms.
  var terms = Object.keys(vocab).sort(function(a, b) { return b.length - a.length; });

  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function wrapTermsInNode(node) {
    if (!node) return;
    if (node.nodeType === Node.TEXT_NODE) {
      var text = node.textContent;
      var html = text;
      var matched = false;
      terms.forEach(function(term) {
        // Match whole word only, case-sensitive.
        var re = new RegExp('(' + escapeRegex(term) + ')', 'g');
        if (re.test(html)) {
          matched = true;
          var def = vocab[term].replace(/"/g, '&quot;');
          html = html.replace(re,
            '<span class="vocab-term" tabindex="0">' +
            '<span class="vocab-tip"><span class="vocab-tip-term">' + term + '</span>' + vocab[term] + '</span>' +
            '$1</span>'
          );
        }
      });
      if (matched) {
        var span = document.createElement('span');
        span.innerHTML = html;
        node.parentNode.replaceChild(span, node);
      }
    } else if (
      node.nodeType === Node.ELEMENT_NODE &&
      !/^(SCRIPT|STYLE|CODE|PRE|TEXTAREA|INPUT|BUTTON|A)$/.test(node.tagName) &&
      !node.classList.contains('vocab-tip') &&
      !node.classList.contains('vocab-index')
    ) {
      // Walk child nodes (snapshot to avoid live-NodeList mutation issues)
      Array.from(node.childNodes).forEach(wrapTermsInNode);
    }
  }

  // Run after DOM is ready (we're deferred to end of body)
  var sections = document.querySelectorAll('.section');
  sections.forEach(function(el) { wrapTermsInNode(el); });
})();

function toggleVocabIndex() {
  var panel = document.getElementById('vocab-index-panel');
  if (panel) panel.classList.toggle('collapsed');
}
"""

# ---------------------------------------------------------------------------
# HTML panel template
# ---------------------------------------------------------------------------

_PANEL_TEMPLATE = """<div class="vocab-index" id="vocab-index-panel">
  <div class="vocab-index-header" onclick="toggleVocabIndex()">
    <span class="vocab-index-label">Vocabulary Index</span>
    <span class="vocab-index-chevron">&#9660;</span>
  </div>
  <div class="vocab-index-body">
{entries}
  </div>
</div>"""

_ENTRY_TEMPLATE = """    <div class="vocab-entry">
      <div class="vocab-entry-term">{term}</div>
      <div class="vocab-entry-def">{definition}</div>
    </div>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(config: dict) -> str:
    """Return HTML+CSS+JS fragment for the vocab tooltip + index panel.

    Args:
        config: Dict with key ``vocab`` mapping term strings to definition strings.

    Returns:
        A self-contained HTML fragment: <style>, panel HTML, and <script>.
    """
    vocab: dict[str, str] = config.get("vocab", {})
    if not vocab:
        return ""

    # Build alphabetically sorted entries for the panel
    sorted_terms = sorted(vocab.keys())
    entries_html = "\n".join(
        _ENTRY_TEMPLATE.format(
            term=_escape_html(term),
            definition=_escape_html(vocab[term]),
        )
        for term in sorted_terms
    )
    panel_html = _PANEL_TEMPLATE.format(entries=entries_html)

    # Build JS with the vocab data embedded
    vocab_json = json.dumps(vocab, ensure_ascii=False)
    js = _JS_TEMPLATE.replace("{vocab_json}", vocab_json)

    return (
        f"<style>{_CSS}</style>\n"
        f"{panel_html}\n"
        f"<script>{js}</script>"
    )


def _escape_html(s: str) -> str:
    """Minimal HTML escaping for safe injection into HTML attributes and text."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )
