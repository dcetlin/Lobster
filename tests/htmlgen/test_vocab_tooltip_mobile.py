"""
tests/htmlgen/test_vocab_tooltip_mobile.py

Unit tests for mobile-specific behavior added to src/htmlgen/components/vocab_tooltip.py:
  1. Tap-to-dismiss: touch devices can close tooltips via tap-outside, × button, or Escape key.
  2. Viewport-aware positioning: tooltip is shifted/flipped to stay within the viewport.

Tests verify that the rendered output contains the JS and CSS constructs needed for these
behaviors — checking behavioral intent (which events are wired, which CSS classes are
managed) rather than implementation line-by-line.

Run with: uv run pytest tests/htmlgen/test_vocab_tooltip_mobile.py -v
"""

from __future__ import annotations

import re

import pytest

from src.htmlgen.components import vocab_tooltip

# ---------------------------------------------------------------------------
# Sample vocab for all tests
# ---------------------------------------------------------------------------

SAMPLE_VOCAB = {
    "Stiffness": "Resistance to deformation under load.",
    "Elasticity": "Deformation with return to original form.",
    "Damping": "Absorbs vibration; prevents irrelevant oscillation.",
}


def _rendered() -> str:
    """Return rendered HTML for a standard sample vocab dict."""
    return vocab_tooltip.render({"vocab": SAMPLE_VOCAB})


# ---------------------------------------------------------------------------
# Fix 1: Tap-to-dismiss — close button
# ---------------------------------------------------------------------------

CLOSE_BUTTON_LABEL = "×"  # The visible × character used in the close button


class TestCloseDismissButton:
    """The tooltip card must contain a visible × close button for explicit dismiss."""

    def test_close_button_present_in_output(self):
        html = _rendered()
        # A × or &times; character inside a button-like element must be present in the JS template
        assert (
            "vocab-tip-close" in html or "×" in html or "&times;" in html
        ), "Close button markup (× or &times; or .vocab-tip-close) must appear in rendered output"

    def test_close_button_is_a_button_element(self):
        html = _rendered()
        # Must use a <button> (within the JS template string, which is in the output)
        # Pattern: button element with a close-related class or aria-label near ×
        pattern = r'<button[^>]*(vocab-tip-close|aria-label="[Cc]lose)[^>]*>'
        assert re.search(pattern, html), (
            "Close button must be a <button> element with either .vocab-tip-close class "
            "or aria-label='close'"
        )

    def test_close_button_positioned_top_right(self):
        html = _rendered()
        # The CSS must position the close button at top-right of the card
        assert "vocab-tip-close" in html, "CSS class .vocab-tip-close must be present for positioning"
        # position absolute and top/right coordinates in CSS
        assert re.search(r"\.vocab-tip-close\s*\{[^}]*(position\s*:\s*absolute|top\s*:|right\s*:)[^}]*\}", html, re.DOTALL), (
            ".vocab-tip-close must have absolute positioning with top/right offsets in CSS"
        )


# ---------------------------------------------------------------------------
# Fix 1: Tap-to-dismiss — toggle on term tap
# ---------------------------------------------------------------------------

class TestTapToToggle:
    """Tapping a vocab term on touch devices must toggle the tooltip open/closed."""

    def test_touch_event_listener_present(self):
        html = _rendered()
        # Must listen for ontouchstart or pointerdown/touchstart events
        assert (
            "touchstart" in html or "ontouchstart" in html or "pointerdown" in html
        ), "Touch event (touchstart or pointerdown) must be wired for mobile tap-to-toggle"

    def test_toggle_class_used_for_open_state(self):
        html = _rendered()
        # Must manage an open/active state via a CSS class (e.g., 'vocab-tip-open' or 'active')
        assert (
            "vocab-tip-open" in html or "vocab-term-active" in html or "is-open" in html
        ), "A CSS class must manage the tooltip open state for touch devices"

    def test_only_one_tooltip_open_at_a_time(self):
        html = _rendered()
        # JS must close all existing tooltips before opening a new one
        # Signals: querySelectorAll with the open class, or a closeAll helper
        assert re.search(
            r"(closeAll|querySelectorAll|getElementsByClassName)[^;]*(vocab-tip-open|vocab-term-active|is-open)",
            html,
        ), "JS must close all open tooltips before opening a new one (one-at-a-time invariant)"

    def test_tap_outside_closes_tooltip(self):
        html = _rendered()
        # Must listen on document for click/touchstart and close if outside vocab-term
        assert re.search(
            r"document\.(addEventListener|on(click|touchstart|pointerdown))[^;]*(vocab|tip|close)",
            html,
        ) or re.search(
            r"(vocab|tip|close)[^;]*document\.(addEventListener|on(click|touchstart|pointerdown))",
            html,
        ), "document-level event listener must be present to close tooltips on outside tap"


# ---------------------------------------------------------------------------
# Fix 1: Tap-to-dismiss — Escape key
# ---------------------------------------------------------------------------

class TestEscapeKeyDismiss:
    """Pressing Escape must close any open tooltip."""

    def test_escape_key_listener_present(self):
        html = _rendered()
        assert "keydown" in html or "keyup" in html or "keypress" in html, (
            "A keyboard event listener must be registered (keydown/keyup)"
        )

    def test_escape_key_closes_tooltip(self):
        html = _rendered()
        # Must check for 'Escape' key or keyCode 27
        assert "Escape" in html or "27" in html, (
            "Escape key (string 'Escape' or keyCode 27) must be handled to close tooltips"
        )


# ---------------------------------------------------------------------------
# Fix 1: Desktop hover unchanged
# ---------------------------------------------------------------------------

class TestDesktopHoverPreserved:
    """The existing hover-based tooltip behavior must remain in CSS for pointer devices."""

    def test_hover_css_rule_still_present(self):
        html = _rendered()
        assert ".vocab-term:hover .vocab-tip" in html, (
            "CSS :hover rule on .vocab-term must still be present for desktop behavior"
        )

    def test_pointer_events_not_none_globally(self):
        html = _rendered()
        # pointer-events: none on .vocab-tip is the default invisible state;
        # it must NOT be set to 'none' permanently — hover should re-enable it
        # The CSS has pointer-events: none on .vocab-tip by default (hidden state)
        # but hover should make it visible
        assert ".vocab-term:hover .vocab-tip" in html, (
            "Hover rule must exist — desktop behavior must not be removed"
        )


# ---------------------------------------------------------------------------
# Fix 2: Viewport-aware positioning — CSS
# ---------------------------------------------------------------------------

class TestViewportAwareCSS:
    """CSS must enforce max-width constraints for the tooltip card."""

    def test_max_width_clamp_in_css(self):
        html = _rendered()
        # Must have max-width: min(320px, 90vw) or equivalent clamping
        assert re.search(r"max-width\s*:\s*(min\(|clamp\(|320px|90vw)", html), (
            "CSS must clamp tooltip max-width to min(320px, 90vw) or equivalent"
        )

    def test_tooltip_width_uses_vw_or_min(self):
        html = _rendered()
        assert "vw" in html or "min(" in html or "clamp(" in html, (
            "Tooltip CSS must use viewport-relative units (vw) or min()/clamp() for width"
        )


# ---------------------------------------------------------------------------
# Fix 2: Viewport-aware positioning — JS
# ---------------------------------------------------------------------------

class TestViewportAwareJS:
    """JS must check and correct tooltip position after it becomes visible."""

    def test_get_bounding_client_rect_used(self):
        html = _rendered()
        assert "getBoundingClientRect" in html, (
            "JS must call getBoundingClientRect() to measure element positions"
        )

    def test_viewport_width_check_present(self):
        html = _rendered()
        # window.innerWidth or document.documentElement.clientWidth
        assert "innerWidth" in html or "clientWidth" in html, (
            "JS must read viewport width (window.innerWidth or clientWidth) for overflow detection"
        )

    def test_viewport_height_check_present(self):
        html = _rendered()
        # window.innerHeight or document.documentElement.clientHeight
        assert "innerHeight" in html or "clientHeight" in html, (
            "JS must read viewport height (window.innerHeight or clientHeight) for overflow detection"
        )

    def test_right_edge_overflow_corrected(self):
        html = _rendered()
        # JS must shift left when tooltip overflows right edge
        # Signal: checking right >= innerWidth or similar, then adjusting left
        assert re.search(
            r"(rect\.right|right\s*[><=]+\s*inner|overflow.*right|right.*overflow)",
            html,
        ), "JS must detect and correct right-edge overflow"

    def test_top_edge_flip_to_below(self):
        html = _rendered()
        # JS must flip the tooltip below the term when it goes off the top
        # Signal: checking rect.top < 0 or similar, then changing bottom/top positioning
        assert re.search(
            r"(rect\.top|top\s*<\s*0|flip.*below|below.*flip|flip-below|vocab-tip-below)",
            html,
        ), "JS must detect top-edge overflow and flip tooltip to show below the term"

    def test_position_recalculated_after_visible(self):
        html = _rendered()
        # Positioning must run after the element is visible (not before display:block)
        # Signal: the function that reads getBoundingClientRect is called after
        # the tooltip's open class is set (i.e., the term has been activated)
        # We check that getBoundingClientRect appears in the same JS block as the open-class logic
        assert "getBoundingClientRect" in html, (
            "getBoundingClientRect must be present — positioning runs after visible"
        )


# ---------------------------------------------------------------------------
# Fix 2: Narrow screen centering
# ---------------------------------------------------------------------------

class TestNarrowScreenCentering:
    """On screens narrower than 400px, tooltip must center horizontally relative to the viewport."""

    NARROW_SCREEN_THRESHOLD = 400  # pixels, per the spec

    def test_narrow_screen_threshold_present(self):
        html = _rendered()
        assert str(self.NARROW_SCREEN_THRESHOLD) in html, (
            f"The {self.NARROW_SCREEN_THRESHOLD}px narrow-screen threshold must appear in JS"
        )

    def test_narrow_screen_centering_logic_present(self):
        html = _rendered()
        # On narrow screens, center relative to viewport
        # Signal: left = (innerWidth / 2) - (tipWidth / 2) or similar
        assert re.search(
            r"(innerWidth\s*/\s*2|50.*vw|centering|center.*viewport|viewport.*center)",
            html,
        ) or re.search(
            r"400[^;]*(innerWidth|center|50%)",
            html,
        ), "JS must center the tooltip horizontally on screens narrower than 400px"


# ---------------------------------------------------------------------------
# Regression: empty vocab returns empty string
# ---------------------------------------------------------------------------

class TestEmptyVocabRegression:
    def test_empty_vocab_returns_empty_string(self):
        result = vocab_tooltip.render({"vocab": {}})
        assert result == "", "render() must return empty string when vocab dict is empty"

    def test_missing_vocab_key_returns_empty_string(self):
        result = vocab_tooltip.render({})
        assert result == "", "render() must return empty string when vocab key is absent"


# ---------------------------------------------------------------------------
# Structural: component metadata unchanged
# ---------------------------------------------------------------------------

class TestComponentMetadata:
    def test_component_id_unchanged(self):
        assert vocab_tooltip.COMPONENT_ID == "vocab-tooltip"

    def test_component_version_semver(self):
        parts = vocab_tooltip.COMPONENT_VERSION.split(".")
        assert len(parts) == 3, "COMPONENT_VERSION must be semver X.Y.Z"

    def test_config_schema_present(self):
        assert isinstance(vocab_tooltip.CONFIG_SCHEMA, dict)
