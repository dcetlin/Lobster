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
COMPONENT_VERSION = "1.2.0"

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
  max-width: min(320px, 90vw);
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
/* flip-below variant: caret moves to bottom of card pointing up */
.vocab-term .vocab-tip.vocab-tip-below {
  bottom: auto;
  top: calc(100% + 6px);
}
.vocab-term .vocab-tip.vocab-tip-below::after {
  top: auto;
  bottom: 100%;
  border-top-color: transparent;
  border-bottom-color: var(--border2);
}
/* Desktop: hover/focus shows tooltip */
.vocab-term:hover .vocab-tip,
.vocab-term:focus .vocab-tip {
  visibility: visible;
  opacity: 1;
  pointer-events: auto;
}
/* Mobile: JS adds vocab-tip-open class to show tooltip */
.vocab-term.vocab-tip-open .vocab-tip {
  visibility: visible;
  opacity: 1;
  pointer-events: auto;
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
/* ── Close button (top-right of tooltip card) ── */
.vocab-tip-close {
  position: absolute;
  top: 6px;
  right: 8px;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  color: var(--text3);
  padding: 2px 4px;
  opacity: 0.7;
}
.vocab-tip-close:hover { opacity: 1; }

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

  // Narrow-screen threshold: below this width, center tooltip relative to viewport.
  var NARROW_SCREEN_THRESHOLD = 400;

  // Sort terms longest-first to avoid partial matches swallowing longer terms.
  var terms = Object.keys(vocab).sort(function(a, b) { return b.length - a.length; });

  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // ---------------------------------------------------------------------------
  // Close all open tooltips (enforces one-at-a-time invariant).
  // ---------------------------------------------------------------------------
  function closeAll() {
    var open = document.querySelectorAll('.vocab-tip-open');
    for (var i = 0; i < open.length; i++) {
      open[i].classList.remove('vocab-tip-open');
      // Reset any inline positioning applied by repositionTip()
      var tip = open[i].querySelector('.vocab-tip');
      if (tip) {
        tip.style.left = '';
        tip.style.right = '';
        tip.style.top = '';
        tip.style.bottom = '';
        tip.style.transform = '';
        tip.style.position = '';
        tip.classList.remove('vocab-tip-below');
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Viewport-aware positioning: shift/flip so the tooltip card stays on-screen.
  // Runs after the tooltip is made visible so getBoundingClientRect() is accurate.
  // ---------------------------------------------------------------------------
  function repositionTip(termEl) {
    var tip = termEl.querySelector('.vocab-tip');
    if (!tip) return;

    var termRect = termEl.getBoundingClientRect();
    var tipRect = tip.getBoundingClientRect();
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;

    // Reset to default (above, centred on term)
    tip.classList.remove('vocab-tip-below');
    tip.style.left = '';
    tip.style.right = '';
    tip.style.top = '';
    tip.style.bottom = '';
    tip.style.transform = '';
    tip.style.position = 'absolute';

    // Re-read rect after reset
    tipRect = tip.getBoundingClientRect();

    // ── Vertical: flip below if top goes off-screen ──
    if (tipRect.top < 0) {
      tip.classList.add('vocab-tip-below');
      tipRect = tip.getBoundingClientRect();
    }

    // ── Vertical: flip above if bottom goes off-screen (and above fits) ──
    if (tipRect.bottom > vh && tip.classList.contains('vocab-tip-below')) {
      // Both positions overflow — prefer above if it has more room
      if (termRect.top > vh - termRect.bottom) {
        tip.classList.remove('vocab-tip-below');
        tipRect = tip.getBoundingClientRect();
      }
    }

    // ── Horizontal: narrow-screen centering ──
    if (vw < NARROW_SCREEN_THRESHOLD) {
      // Centre relative to viewport rather than the term
      var centreLeft = (vw / 2) - (tipRect.width / 2);
      // Convert viewport-relative centreLeft to position relative to termEl's offset parent
      var offsetLeft = centreLeft - termRect.left;
      tip.style.left = offsetLeft + 'px';
      tip.style.transform = 'none';
    } else {
      // Normal screens: adjust for right-edge overflow
      tipRect = tip.getBoundingClientRect();
      if (tipRect.right > vw) {
        var shiftLeft = tipRect.right - vw + 8;
        var currentLeft = parseFloat(tip.style.left) || 0;
        tip.style.left = (currentLeft - shiftLeft) + 'px';
        tip.style.transform = 'none';
        tipRect = tip.getBoundingClientRect();
      }
      // Clamp so tip never goes past the left edge
      if (tipRect.left < 0) {
        var shiftRight = -tipRect.left + 8;
        var currentLeft2 = parseFloat(tip.style.left) || 0;
        tip.style.left = (currentLeft2 + shiftRight) + 'px';
        tip.style.transform = 'none';
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Term wrapping — inject tooltip markup including the × close button.
  //
  // To prevent nested tooltip markup inside definition text, this function
  // uses a two-pass approach:
  //   Pass 1 (terms loop): replace each term occurrence with a placeholder
  //           token __TTIP_N__ and store the full tooltip span HTML in defns[].
  //           Subsequent term regexes operate on a string that contains only
  //           placeholders for already-wrapped terms — never raw definition text.
  //   Pass 2 (restore): replace each __TTIP_N__ placeholder with its stored
  //           tooltip HTML to produce the final markup string.
  // ---------------------------------------------------------------------------
  function wrapTermsInNode(node) {
    if (!node) return;
    if (node.nodeType === Node.TEXT_NODE) {
      var text = node.textContent;
      var html = text;
      var matched = false;
      var defns = [];  // stores full tooltip span HTML indexed by placeholder id
      terms.forEach(function(term) {
        // Match case-sensitive (no word-boundary anchors — terms may appear mid-word).
        var re = new RegExp('(' + escapeRegex(term) + ')', 'g');
        var def = vocab[term]
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
        var newHtml = html.replace(re, function(match, captured) {
          // Build full tooltip span and store it; substitute a placeholder.
          var tipHtml =
            '<span class="vocab-term" tabindex="0">' +
            '<span class="vocab-tip">' +
            '<button class="vocab-tip-close" aria-label="Close" tabindex="0">×</button>' +
            '<span class="vocab-tip-term">' + term + '</span>' + def + '</span>' +
            captured + '</span>';
          var idx = defns.length;
          defns.push(tipHtml);
          return '__TTIP_' + idx + '__';
        });
        if (newHtml !== html) {
          matched = true;
          html = newHtml;
        }
      });
      if (matched) {
        // Pass 2: restore placeholders to their stored tooltip HTML.
        for (var i = 0; i < defns.length; i++) {
          html = html.split('__TTIP_' + i + '__').join(defns[i]);
        }
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

  // ---------------------------------------------------------------------------
  // Mobile tap-to-toggle: wire touch/pointer events after terms are wrapped.
  // ---------------------------------------------------------------------------
  // Delegate touch events from the document root to avoid stale-reference issues
  // after innerHTML replacement during term wrapping.
  document.addEventListener('touchstart', function(e) {
    var termEl = e.target.closest('.vocab-term');
    var closeBtn = e.target.closest('.vocab-tip-close');

    if (closeBtn) {
      // × button: close the containing term's tooltip
      e.preventDefault();
      var parentTerm = closeBtn.closest('.vocab-term');
      if (parentTerm) parentTerm.classList.remove('vocab-tip-open');
      return;
    }

    if (termEl) {
      // Tapping a term: toggle its tooltip; close all others first
      e.preventDefault();
      var wasOpen = termEl.classList.contains('vocab-tip-open');
      closeAll();
      if (!wasOpen) {
        termEl.classList.add('vocab-tip-open');
        repositionTip(termEl);
      }
      return;
    }

    // Tapping outside any term or tooltip: close all
    var tipEl = e.target.closest('.vocab-tip');
    if (!tipEl) {
      closeAll();
    }
  }, { passive: false });

  // Document-level click listener handles outside-tap on non-touch devices too
  document.addEventListener('click', function(e) {
    var termEl = e.target.closest('.vocab-term');
    var tipEl = e.target.closest('.vocab-tip');
    if (!termEl && !tipEl) {
      closeAll();
    }
  });

  // Escape key closes any open tooltip
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' || e.keyCode === 27) {
      closeAll();
    }
  });
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
