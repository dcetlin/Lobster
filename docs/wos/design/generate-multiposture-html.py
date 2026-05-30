"""
generate-multiposture-html.py — Phase 3 migration of multiposture-spec.html

Renders multiposture-spec.html via the Lobster renderer (spec-document template),
then post-injects CSS for the custom HTML components (posture-grid, root-cause-box,
flow blocks, code-block, decision-row, migration-step, nongoal-list) that were part
of the original hand-written spec.

Run from ~/lobster/:
    uv run docs/wos/design/generate-multiposture-html.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root on sys.path for src.htmlgen imports
# docs/wos/design/generate-multiposture-html.py
#   .parent      = docs/wos/design/
#   .parent.parent = docs/wos/
#   .parent.parent.parent = docs/
#   .parent.parent.parent.parent = repo root (lobster/)
_SCRIPT_DIR = Path(__file__).parent          # docs/wos/design/
_REPO_ROOT = _SCRIPT_DIR.parent.parent.parent  # lobster/
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.htmlgen.renderer import render_and_upload  # noqa: E402

# ---------------------------------------------------------------------------
# Custom CSS for multiposture-spec components
# (copied from the original multiposture-spec.html, adapted for CSS variables)
# ---------------------------------------------------------------------------

_CUSTOM_CSS = """
  /* ── Posture cards ── */
  .posture-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 24px; }
  @media (max-width: 700px) { .posture-grid { grid-template-columns: 1fr; } }
  .posture-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }
  .posture-card.posture-1 { border-top: 3px solid var(--accent); }
  .posture-card.posture-2 { border-top: 3px solid var(--accent2); }
  .posture-card.posture-3 { border-top: 3px solid var(--accent3); }
  .posture-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; margin-bottom: 4px;
  }
  .posture-1 .posture-label { color: var(--accent); }
  .posture-2 .posture-label { color: var(--accent2); }
  .posture-3 .posture-label { color: var(--accent3); }
  .posture-name { font-size: 14px; font-weight: 700; color: var(--text); margin-bottom: 10px; }
  .posture-card p { font-size: 12px; color: var(--text2); }
  .posture-tag {
    display: inline-block; font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 4px; margin-top: 10px;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .tag-keep { background: rgba(110,168,254,.12); color: var(--accent); }
  .tag-new { background: rgba(167,139,250,.12); color: var(--accent2); }
  .tag-merged { background: rgba(52,211,153,.12); color: var(--accent3); }

  /* ── Root cause box ── */
  .root-cause-box {
    background: var(--surface);
    border: 1px solid var(--border2, var(--border));
    border-left: 3px solid var(--warn, #f59e0b);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 20px;
  }
  .root-cause-box .label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--warn, #f59e0b); margin-bottom: 8px;
  }

  /* ── Flow / ASCII diagrams ── */
  .flow {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 12px;
    line-height: 1.9;
    margin-bottom: 16px;
    overflow-x: auto;
    white-space: pre;
    color: var(--text2);
  }

  /* ── Inline code-block (non-pre) ── */
  .code-block {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 20px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 12px;
    line-height: 1.7;
    margin-bottom: 16px;
    overflow-x: auto;
    white-space: pre;
    color: var(--text2);
  }
  .code-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--text3);
    margin-bottom: 6px; font-family: 'SF Mono', 'Fira Code', monospace;
  }

  /* ── Pill badges ── */
  .pill {
    display: inline-block; font-size: 10px; font-weight: 700;
    padding: 2px 8px; border-radius: 12px;
    font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .pill-blue { background: rgba(110,168,254,.15); color: var(--accent); }
  .pill-purple { background: rgba(167,139,250,.15); color: var(--accent2); }
  .pill-green { background: rgba(52,211,153,.15); color: var(--accent3); }
  .pill-yellow { background: rgba(245,158,11,.15); color: var(--warn, #f59e0b); }
  .pill-red { background: rgba(248,113,113,.15); color: var(--danger, #f87171); }

  /* ── Decision row ── */
  .decision-row { display: flex; gap: 12px; margin-bottom: 12px; }
  .decision-option {
    flex: 1; background: var(--surface2);
    border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px;
  }
  .decision-option .opt-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .08em; color: var(--text3); margin-bottom: 6px;
  }
  .decision-option.recommended { border-color: var(--accent); }
  .decision-option.recommended .opt-label { color: var(--accent); }
  @media (max-width: 600px) { .decision-row { flex-direction: column; } }

  /* ── Non-goals list ── */
  .nongoal-list { list-style: none; padding: 0; }
  .nongoal-list li {
    display: flex; align-items: baseline; gap: 10px;
    padding: 10px 0; border-bottom: 1px solid var(--border);
    color: var(--text2); font-size: 13px;
  }
  .nongoal-list li:last-child { border-bottom: none; }
  .nongoal-list li::before {
    content: '✕'; color: var(--danger, #f87171);
    font-size: 11px; font-weight: 700; flex-shrink: 0;
  }

  /* ── Migration steps ── */
  .migration-step { display: flex; gap: 16px; margin-bottom: 16px; }
  .step-num {
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--surface2); border: 1px solid var(--border2, var(--border));
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 700; color: var(--accent);
    flex-shrink: 0; font-family: 'SF Mono', 'Fira Code', monospace;
  }
  .step-content { flex: 1; }
  .step-title { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
  .step-desc { font-size: 12px; color: var(--text2); }
"""


def main() -> None:
    manifest_path = _REPO_ROOT / "docs" / "wos" / "design" / "multiposture-manifest.json"
    output_filename = "multiposture-spec-v2.html"

    print(f"Rendering {manifest_path} via spec-document template...")

    url = render_and_upload(
        content_path=manifest_path,
        template_id="spec-document",
        output_filename=output_filename,
    )

    # Post-inject custom CSS into the rendered output
    uploads_dir = Path(os.environ.get("LOBSTER_INBOX_DIR", str(Path.home() / "messages" / "inbox"))).parent / "bisque-uploads"
    output_path = uploads_dir / output_filename

    html = output_path.read_text(encoding="utf-8")

    # Inject custom CSS before </style> (the renderer writes one <style> block in <head>)
    css_injection = _CUSTOM_CSS + "\n  </style>"
    if "</style>" in html:
        html = html.replace("</style>", css_injection, 1)
        output_path.write_text(html, encoding="utf-8")
        print("Custom CSS injected successfully.")
    else:
        print("WARNING: Could not find </style> tag — custom CSS not injected.")

    print(f"\nBisque URL: {url}")
    print(f"Output path: {output_path}")


if __name__ == "__main__":
    main()
