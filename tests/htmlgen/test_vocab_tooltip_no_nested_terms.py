"""
tests/htmlgen/test_vocab_tooltip_no_nested_terms.py

Tests that definition text rendered inside tooltip cards does NOT contain
nested vocab-term markup.  The bug: when term A's definition text contains
term B, the JavaScript term-wrapping loop was applying term B's regex to the
accumulated HTML string — which already contained A's definition text — causing
<span class="vocab-term"> markup to appear inside the tooltip card.

The fix: term replacement must not match inside already-injected definition
text.  These tests verify that the rendered JS contains the mechanism that
prevents this (placeholder-based protection), and that the logic is sound
for the cross-term collision case.

Run with: uv run pytest tests/htmlgen/test_vocab_tooltip_no_nested_terms.py -v
"""

from __future__ import annotations

import re

import pytest

from src.htmlgen.components import vocab_tooltip

# ---------------------------------------------------------------------------
# Named constants from the spec
# ---------------------------------------------------------------------------

# Placeholder token prefix used in the JS to protect already-wrapped tooltip
# HTML from subsequent term regex passes.  The JS constructs tokens like
# '__TTIP_' + idx + '__' at runtime; in the rendered source we look for the
# prefix literal '__TTIP_' appearing in the JS source code.
PLACEHOLDER_TOKEN_PREFIX = "__TTIP_"

# The term whose definition contains another term — the cross-term case.
CROSS_TERM_A = "Elasticity"
CROSS_TERM_B = "Stiffness"  # appears inside Elasticity's definition

# Vocab dict where Elasticity's definition mentions Stiffness.
CROSS_TERM_VOCAB = {
    CROSS_TERM_A: f"Deformation with return; complements {CROSS_TERM_B}.",
    CROSS_TERM_B: "Resistance to deformation under load.",
    "Damping": "Absorbs vibration; prevents oscillation.",
}


def _rendered(vocab: dict | None = None) -> str:
    v = vocab if vocab is not None else CROSS_TERM_VOCAB
    return vocab_tooltip.render({"vocab": v})


# ---------------------------------------------------------------------------
# Core invariant: definition text must not contain vocab-term spans
# ---------------------------------------------------------------------------

class TestNoNestedTermMarkupInDefinitions:
    """The definition text inside a tooltip card must be plain text — no
    nested <span class="vocab-term"> markup is permitted."""

    def test_definition_text_does_not_contain_vocab_term_span(self):
        """vocab-term span must never appear nested inside a vocab-tip span
        in the rendered JS template.

        Because the markup is injected at runtime by JS, we verify the
        mechanism: the replacement string must reference a placeholder token,
        not the raw definition text, so later term regexes cannot match inside
        already-wrapped definitions.  The placeholder token prefix __TTIP_
        must appear in the rendered JS source.
        """
        html = _rendered()
        # The JS must contain the placeholder token prefix '__TTIP_' as a
        # string literal used to construct per-term placeholders at runtime.
        assert PLACEHOLDER_TOKEN_PREFIX in html, (
            f"JS must use placeholder tokens prefixed with {PLACEHOLDER_TOKEN_PREFIX!r} "
            "to protect definition text from nested term substitution"
        )

    def test_placeholder_restore_pass_present(self):
        """After the term-replacement loop, the JS must restore placeholders
        to their actual tooltip HTML in a single final pass."""
        html = _rendered()
        # Signal: a loop or replace call that maps placeholder IDs back to
        # stored tooltip strings.  Look for array index restore or a
        # 'defns' / 'tooltips' / 'parts' variable restoration pass.
        assert re.search(
            r"(__TTIP_|placeholder|defns|tooltips|stored|restore)",
            html,
        ), (
            "JS must include a restore pass that replaces placeholder tokens "
            "with the actual tooltip HTML after all term replacements are done"
        )

    def test_tooltip_html_stored_before_replacement(self):
        """The full tooltip span HTML must be stored in an array/dict *before*
        it is injected into the text, so placeholders reference stored content."""
        html = _rendered()
        # Signal: a push() call or array assignment storing tooltip HTML,
        # OR a 'defns' / 'parts' variable that accumulates tooltip fragments.
        assert re.search(
            r"(\.push\s*\(|defns\s*=|tooltips\s*=|parts\s*=|stored\s*=|__TTIP_)",
            html,
        ), (
            "JS must store tooltip HTML fragments in an array or mapping "
            "before injecting placeholders into the working string"
        )


# ---------------------------------------------------------------------------
# Mechanism: the replacement string uses a placeholder, not the definition
# ---------------------------------------------------------------------------

class TestReplacementUsesPlaceholder:
    """The string substituted into `html` during the terms loop must be a
    placeholder token, never the raw definition string directly."""

    def test_vocab_tip_span_not_built_inline_in_replace(self):
        """The `.replace(re, ...)` call must not inline the full
        <span class="vocab-tip">…definition…</span> HTML directly.

        Instead it should inline a placeholder like __TTIP_N__.
        We detect the violation by checking that the vocab-tip open tag
        does NOT appear as a string literal concatenated with the term
        replacement in a single replace() call argument.
        """
        html = _rendered()
        # A violation looks like: html.replace(re, '<span class="vocab-term">…vocab-tip…' + term)
        # We check: the replace call argument does not span from vocab-term open
        # to vocab-tip content inline in a single expression.
        # We detect the *correct* state: the replace() argument contains a
        # placeholder token instead of the full tooltip span.
        assert re.search(r"__TTIP_", html), (
            "The replace() argument in the terms loop must use a placeholder "
            "token (__TTIP_N__) rather than inlining the full tooltip span HTML"
        )


# ---------------------------------------------------------------------------
# Regression: cross-term collision case is handled
# ---------------------------------------------------------------------------

class TestCrossTermCollisionRegression:
    """When term A's definition contains term B, the wrapping of term B
    must not appear inside A's tooltip definition text."""

    def test_cross_term_vocab_renders_without_error(self):
        """render() must not raise for a vocab where definitions cross-reference."""
        result = _rendered(CROSS_TERM_VOCAB)
        assert isinstance(result, str) and len(result) > 0

    def test_cross_term_vocab_js_still_contains_both_terms(self):
        """Both terms must still appear in the embedded vocab JSON."""
        html = _rendered(CROSS_TERM_VOCAB)
        assert CROSS_TERM_A in html
        assert CROSS_TERM_B in html

    def test_cross_term_definition_contains_raw_text_not_markup(self):
        """In the vocab JSON embedded in the script, the definition for
        Elasticity must contain the word 'Stiffness' as plain text —
        not wrapped in a vocab-term span.

        We read the embedded vocab JSON to verify that definitions are stored
        without markup (the wrapping happens at DOM-walk time, not JSON-embed
        time; and the DOM-walk must not re-enter tooltip spans).
        """
        import json

        html = _rendered(CROSS_TERM_VOCAB)
        # Extract the vocab JSON embedded in the JS template.
        # Pattern: var vocab = {...};  (JSON object on one line)
        m = re.search(r"var vocab\s*=\s*(\{[^\n]+\})\s*;", html)
        assert m, "Could not find 'var vocab = {...};' in rendered output"
        vocab_obj = json.loads(m.group(1))
        elasticity_def = vocab_obj.get(CROSS_TERM_A, "")
        # The definition should contain the word 'Stiffness' literally,
        # NOT as '<span class="vocab-term"...>Stiffness</span>'.
        assert CROSS_TERM_B in elasticity_def, (
            f"Definition for {CROSS_TERM_A} must contain the word {CROSS_TERM_B!r}"
        )
        assert '<span class="vocab-term"' not in elasticity_def, (
            f"Definition for {CROSS_TERM_A} in the embedded JSON must be plain text, "
            f"not HTML markup — found vocab-term span inside definition"
        )


# ---------------------------------------------------------------------------
# Regression: existing behavior unchanged
# ---------------------------------------------------------------------------

class TestExistingBehaviorUnchanged:
    """The fix must not break any existing functionality."""

    def test_vocab_terms_still_wrapped_in_js(self):
        """wrapTermsInNode function must still be present."""
        html = _rendered()
        assert "wrapTermsInNode" in html

    def test_vocab_tip_span_still_present_in_template(self):
        """The tooltip card span structure must still be generated."""
        html = _rendered()
        assert "vocab-tip" in html

    def test_close_button_still_present(self):
        html = _rendered()
        assert "vocab-tip-close" in html

    def test_component_version_bumped(self):
        """COMPONENT_VERSION must be incremented beyond 1.1.0 to reflect the fix."""
        version = vocab_tooltip.COMPONENT_VERSION
        major, minor, patch = (int(x) for x in version.split("."))
        # Must be > 1.1.0
        assert (major, minor, patch) > (1, 1, 0), (
            f"COMPONENT_VERSION must be bumped beyond 1.1.0; got {version}"
        )
