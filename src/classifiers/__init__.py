"""
src/classifiers/ — Background classification layer for the multi-timescale architecture.

Two processes:
  - quick_classifier.py  (Layer 3, medium-quick): first-pass tagging near message receipt
  - slow_reclassifier.py (Layer 4, medium-slow):  continuous re-categorization with accumulated context

See design/cycle-spec-design.md for the full architectural rationale.
"""
