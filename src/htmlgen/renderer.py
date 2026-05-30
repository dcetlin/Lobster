"""
renderer.py — Lobster HTML compilation pipeline (Phase 1 + Phase 2)

Wires together:
  - Layer 3: conventions.py (design tokens, layout rules)
  - Layer 3: templates/registry.py (template selection and metadata)
  - Layer 2: components/ (theme toggle, clipboard widget, D3 graph)

Produces a single self-contained .html file from a JSON content manifest.

CLI usage:
    uv run src/htmlgen/renderer.py --content <path> --template <template-id> --output <path>

Python API:
    from src.htmlgen.renderer import render_and_upload
    url = render_and_upload(content_path, template_id, output_filename)

Phase 1 scope: JSON content format (section content as plain text or Markdown strings).
Phase 2 addition: Markdown rendering in section content via the Python `markdown` library.
  - Section content is rendered as Markdown when the manifest includes
    `"content_format": "markdown"` at the top level, or when the section
    includes `"content_format": "markdown"`.
  - Plain text content is passed through Markdown rendering too
    (Markdown is a superset of plain text).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import markdown as _markdown_lib
    _MARKDOWN_AVAILABLE = True
except ImportError:
    _MARKDOWN_AVAILABLE = False

# ---------------------------------------------------------------------------
# Path setup — allow running as a script (not just as an imported module)
# ---------------------------------------------------------------------------

# The repo root (parent of src/) must be on sys.path for `from src.htmlgen.*` imports
# to resolve. When run via pytest, conftest.py already handles this. When run
# directly via `uv run src/htmlgen/renderer.py`, we add it here.
_REPO_ROOT = Path(__file__).parent.parent.parent  # src/htmlgen/renderer.py -> repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.htmlgen.conventions import (  # noqa: E402  (import after sys.path setup)
    load_conventions,
    get_all_color_tokens,
    get_typography,
    get_layout,
    build_css_custom_properties,
)
from src.htmlgen.templates.registry import get_template  # noqa: E402
from src.htmlgen.components import get_component  # noqa: E402


# ---------------------------------------------------------------------------
# Bisque upload helpers
# (copied from wos_dashboard.py pattern — originals are not changed)
# ---------------------------------------------------------------------------

def _uploads_dir() -> Path:
    """Return the bisque-uploads directory path."""
    messages_dir = Path(
        os.environ.get("LOBSTER_INBOX_DIR", str(Path.home() / "messages" / "inbox"))
    ).parent
    return messages_dir / "bisque-uploads"


def _bisque_base_url() -> str:
    """Return the HTTP base URL of the bisque relay server.

    Priority:
    1. BISQUE_RELAY_HTTP_URL env var
    2. LOBSTER_PUBLIC_IP env var with default port 9101
    3. Parse ~/lobster-config/config.env
    4. curl ifconfig.me fallback
    5. localhost last resort
    """
    env_url = os.environ.get("BISQUE_RELAY_HTTP_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    public_ip = os.environ.get("LOBSTER_PUBLIC_IP", "").strip()
    if not public_ip:
        config_file = Path.home() / "lobster-config" / "config.env"
        if config_file.exists():
            for line in config_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("LOBSTER_PUBLIC_IP="):
                    public_ip = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    break
                if stripped.startswith("BISQUE_RELAY_HTTP_URL="):
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'").rstrip("/")

    if not public_ip:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "5", "-4", "ifconfig.me"],
                capture_output=True, text=True, timeout=6,
            )
            public_ip = result.stdout.strip()
        except Exception:
            pass

    port = os.environ.get("BISQUE_RELAY_PORT", "9101")
    if public_ip:
        return f"http://{public_ip}:{port}"
    return f"http://localhost:{port}"


# ---------------------------------------------------------------------------
# Content manifest loading
# ---------------------------------------------------------------------------

def load_content_manifest(content_path: Path) -> dict[str, Any]:
    """Load and parse the JSON content manifest.

    Raises FileNotFoundError if the file does not exist.
    Raises ValueError if the JSON is malformed or required fields are missing.
    """
    if not content_path.exists():
        raise FileNotFoundError(f"Content manifest not found: {content_path}")

    try:
        data = json.loads(content_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Content manifest is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Content manifest must be a JSON object, got {type(data).__name__}")

    required = ["doc_id", "title", "version", "template_id", "sections"]
    for field in required:
        if field not in data:
            raise ValueError(f"Content manifest missing required field: '{field}'")

    if not isinstance(data["sections"], list):
        raise ValueError("Content manifest 'sections' must be an array")

    return data


# ---------------------------------------------------------------------------
# Post-render validation
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when post-render validation fails."""


def validate_html(html: str, manifest: dict[str, Any]) -> None:
    """Post-render validation (Smell-3 from spec §9).

    Checks:
    1. doc-version meta tag is present
    2. All section IDs from manifest are present in the HTML
    3. No {{placeholder}} strings remaining
    4. No unclosed <script> tags (basic check)

    Raises ValidationError with details if any check fails.
    """
    errors: list[str] = []

    # Check 1: doc-version meta tag
    if not re.search(r'<meta[^>]+name=["\']doc-version["\']', html, re.IGNORECASE):
        errors.append("Missing required <meta name='doc-version'> tag")

    # Check 2: all section IDs from manifest are present
    section_ids = [s.get("id", "") for s in manifest.get("sections", [])]
    for sid in section_ids:
        if sid and f'id="{sid}"' not in html and f"id='{sid}'" not in html:
            errors.append(f"Section ID '{sid}' from manifest is missing in rendered HTML")

    # Check 3: no {{placeholder}} strings remaining
    placeholders = re.findall(r"\{\{[^}]+\}\}", html)
    if placeholders:
        errors.append(
            f"Unresolved placeholder(s) in output: {', '.join(set(placeholders))}"
        )

    # Check 4: script tags have matching closing tags (basic count check)
    open_scripts = len(re.findall(r"<script[\s>]", html, re.IGNORECASE))
    close_scripts = len(re.findall(r"</script>", html, re.IGNORECASE))
    if open_scripts != close_scripts:
        errors.append(
            f"Mismatched <script> tags: {open_scripts} opening, {close_scripts} closing"
        )

    if errors:
        raise ValidationError(
            "Post-render validation failed:\n  - " + "\n  - ".join(errors)
        )


# ---------------------------------------------------------------------------
# CSS assembly
# ---------------------------------------------------------------------------

def _build_global_css(conventions: dict[str, Any]) -> str:
    """Build the global CSS block from conventions: custom properties + base styles."""
    dark_tokens = get_all_color_tokens("dark")
    light_tokens = get_all_color_tokens("light")
    typography = get_typography()
    layout = get_layout()

    dark_props = "\n".join(f"  --{k}: {v};" for k, v in dark_tokens.items())
    light_props = "\n".join(f"  --{k}: {v};" for k, v in light_tokens.items())

    font_stack = typography.get("font_stack", "system-ui, sans-serif")
    mono_stack = typography.get("mono_stack", "monospace")
    base_size = typography.get("base_size", "15px")
    line_height = typography.get("base_line_height", "1.7")
    wrap_max = layout.get("wrap_max_width", "820px")
    wrap_padding = layout.get("wrap_padding", "56px 28px 120px")

    return f"""
:root {{
{dark_props}
}}
body.light-mode {{
{light_props}
}}
*, *::before, *::after {{ box-sizing: border-box; }}
html {{ font-size: {base_size}; }}
body {{
  font-family: {font_stack};
  line-height: {line_height};
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 0;
}}
code, pre, kbd {{
  font-family: {mono_stack};
}}
.wrap {{
  max-width: {wrap_max};
  margin: 0 auto;
  padding: {wrap_padding};
}}
.doc-header {{ margin-bottom: 40px; border-bottom: 1px solid var(--border); padding-bottom: 24px; }}
.doc-title {{ font-size: 28px; font-weight: 800; color: var(--text); margin: 0 0 8px; }}
.doc-subtitle {{ font-size: 15px; color: var(--text2); margin: 0; }}
.doc-meta {{ margin-top: 12px; font-size: 12px; color: var(--text3); display: flex; gap: 16px; flex-wrap: wrap; }}
.section {{ margin-bottom: 48px; }}
.section-label {{ font-size: 11px; font-weight: 700; color: var(--text3); text-transform: uppercase;
  letter-spacing: .08em; margin-bottom: 6px; }}
.section h2 {{ font-size: 19px; font-weight: 700; color: var(--text); margin: 0 0 16px; }}
.section h3 {{ font-size: 16px; font-weight: 600; color: var(--text); margin: 20px 0 12px; }}
.section p {{ color: var(--text2); margin: 0 0 14px; }}
.section ul, .section ol {{ color: var(--text2); padding-left: 24px; }}
.section li {{ margin-bottom: 6px; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.doc-footer {{ margin-top: 64px; padding-top: 20px; border-top: 1px solid var(--border);
  font-size: 11px; color: var(--text3); display: flex; justify-content: space-between; }}
pre {{ background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
  padding: 16px; overflow-x: auto; margin: 16px 0; }}
pre code {{ background: none; border: none; padding: 0; font-size: 13px; color: var(--text2); }}
code {{ background: var(--surface2); border-radius: 4px; padding: 2px 6px;
  font-size: 13px; color: var(--accent2); }}
blockquote {{ border-left: 3px solid var(--accent); margin: 16px 0; padding: 8px 16px;
  background: var(--surface2); color: var(--text2); border-radius: 0 4px 4px 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; }}
th {{ background: var(--surface2); color: var(--text); font-weight: 600;
  padding: 10px 14px; text-align: left; border-bottom: 2px solid var(--border); }}
td {{ padding: 8px 14px; border-bottom: 1px solid var(--border); color: var(--text2); }}
tr:last-child td {{ border-bottom: none; }}
strong {{ color: var(--text); font-weight: 600; }}
em {{ color: var(--text2); }}
""".strip()


# ---------------------------------------------------------------------------
# Section rendering
# ---------------------------------------------------------------------------

def _render_section(section: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Render a single section from the manifest into an HTML div."""
    sid = section.get("id", "")
    title = section.get("title", "")
    label = section.get("label", "")
    content = section.get("content", "")

    # If content_key is provided, look up content in manifest top-level
    content_key = section.get("content_key", "")
    if content_key and content_key in manifest:
        section_data = manifest[content_key]
        if isinstance(section_data, dict):
            content = section_data.get("content", content)
            if not title:
                title = section_data.get("title", title)

    # Render content — Markdown (Phase 2) or plain text paragraphs (fallback)
    content_html = ""
    if content:
        # Determine rendering mode: check section then manifest top-level
        section_fmt = section.get("content_format", "")
        manifest_fmt = manifest.get("content_format", "")
        use_markdown = (
            section_fmt == "markdown"
            or manifest_fmt == "markdown"
            or (section_fmt == "" and manifest_fmt == "" and _MARKDOWN_AVAILABLE)
        )

        if use_markdown and _MARKDOWN_AVAILABLE:
            content_html = _markdown_lib.markdown(
                content,
                extensions=["tables", "fenced_code", "nl2br"],
            )
        else:
            # Fallback: split on double newlines for paragraphs
            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            if paragraphs:
                content_html = "\n".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)
            else:
                content_html = f"<p>{content}</p>"

    label_html = f'<div class="section-label">{label}</div>' if label else ""
    title_html = f"<h2>{title}</h2>" if title else ""
    id_attr = f' id="{sid}"' if sid else ""

    return f"""<div class="section"{id_attr}>
  {label_html}
  {title_html}
  {content_html}
</div>"""


# ---------------------------------------------------------------------------
# Component config resolution
# ---------------------------------------------------------------------------

def _resolve_component_configs(
    manifest: dict[str, Any],
    template: dict[str, Any],
) -> dict[str, dict]:
    """Build the config dict for each component declared in the template.

    Priority:
    1. Per-document config from manifest components[] array (may reference config_file)
    2. Auto-populated defaults (clipboard widget gets sections from manifest)
    3. Empty config as fallback
    """
    # Index manifest component configs by id
    manifest_component_configs: dict[str, dict] = {}
    for comp_entry in manifest.get("components", []):
        comp_id = comp_entry.get("id", "")
        if comp_id:
            manifest_component_configs[comp_id] = comp_entry.get("config", {})

    # Template component list (may include components not in registry — skip those)
    template_components: list[str] = template.get("components", [])

    # Build resolved configs
    resolved: dict[str, dict] = {}
    sections = manifest.get("sections", [])

    for comp_id in template_components:
        if comp_id in manifest_component_configs:
            resolved[comp_id] = manifest_component_configs[comp_id]
        elif comp_id == "clipboard-copy-widget":
            # Auto-populate section list from manifest
            resolved[comp_id] = {
                "sections": [
                    {"id": s.get("id", ""), "label": s.get("label", s.get("id", ""))}
                    for s in sections
                    if s.get("id")
                ]
            }
        elif comp_id == "theme-toggle":
            # Use template override for mode if present
            overrides = template.get("conventions_overrides", {})
            mode = overrides.get("theme_toggle_mode", "js-toggle")
            resolved[comp_id] = {"mode": mode}
        else:
            resolved[comp_id] = {}

    return resolved


# ---------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------

def _assemble_html(
    manifest: dict[str, Any],
    template: dict[str, Any],
    conventions: dict[str, Any],
    component_fragments: dict[str, str],
    needs_d3: bool,
    render_timestamp: str,
) -> str:
    """Assemble the full HTML document from all parts."""

    title = manifest.get("title", "Untitled")
    version = manifest.get("version", "1.0")
    doc_id = manifest.get("doc_id", "document")
    subtitle = manifest.get("subtitle", "")
    updated_at = manifest.get("updated_at", render_timestamp)

    # --- Component versions for meta tag ---
    component_ids = template.get("components", [])
    component_versions: list[str] = []
    for comp_id in component_ids:
        try:
            mod = get_component(comp_id)
            ver = getattr(mod, "COMPONENT_VERSION", "?")
            component_versions.append(f"{comp_id}@{ver}")
        except ValueError:
            pass  # skip unknown components (e.g., dashboard-filter-bar not in registry)

    lobster_components_content = ", ".join(component_versions)

    # --- Global CSS ---
    global_css = _build_global_css(conventions)

    # --- Component CSS (extracted from fragments, injected into <head>) ---
    # We collect <style> blocks separately from the fragment bodies
    component_css_blocks: list[str] = []
    component_body_fragments: list[str] = []

    # theme-toggle goes at top of body as first child
    theme_fragment = component_fragments.get("theme-toggle", "")
    other_fragments: list[str] = []

    for comp_id, fragment in component_fragments.items():
        if comp_id == "theme-toggle":
            continue
        # Clipboard widget goes above footer (bottom of body)
        # D3 network goes inline in sections (handled separately if needed)
        other_fragments.append(fragment)

    # --- Section HTML ---
    sections_html = "\n".join(
        _render_section(s, manifest) for s in manifest.get("sections", [])
    )

    # --- Doc header ---
    subtitle_html = f'<p class="doc-subtitle">{subtitle}</p>' if subtitle else ""
    doc_header = f"""<header class="doc-header">
  <h1 class="doc-title">{title}</h1>
  {subtitle_html}
  <div class="doc-meta">
    <span>Version {version}</span>
    <span>Updated {updated_at}</span>
  </div>
</header>"""

    # --- Footer ---
    footer = f"""<footer class="doc-footer">
  <span>doc-id: {doc_id}</span>
  <span>Rendered by Lobster · {render_timestamp}</span>
</footer>"""

    # --- D3 CDN script tag ---
    d3_script_tag = ""
    if needs_d3:
        d3_script_tag = '<script src="https://d3js.org/d3.v7.min.js"></script>'

    # --- Assemble <head> ---
    head = f"""<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="doc-version" content="{version}">
  <meta name="doc-updated" content="{render_timestamp}">
  <meta name="lobster-components" content="{lobster_components_content}">
  <title>{title}</title>
  <style>
{global_css}
  </style>
</head>"""

    # --- Assemble <body> ---
    other_fragments_html = "\n".join(other_fragments)
    body = f"""<body>
{theme_fragment}
<div class="wrap">
  {doc_header}
  {sections_html}
  {other_fragments_html}
  {footer}
</div>
{d3_script_tag}
</body>"""

    return f"""<!DOCTYPE html>
<html lang="en">
{head}
{body}
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(
    content_path: Path | str,
    template_name: str,
    output_path: Path | str,
) -> str:
    """Compile a JSON content manifest into an HTML file.

    Args:
        content_path: Path to the JSON content manifest.
        template_name: Template ID from registry.yaml (e.g., 'document-class').
        output_path: Destination path for the compiled HTML file.

    Returns:
        The full compiled HTML as a string.

    Raises:
        FileNotFoundError: If content_path or conventions/template files are missing.
        ValueError: If the manifest or template is malformed.
        ValidationError: If post-render validation fails.
    """
    content_path = Path(content_path)
    output_path = Path(output_path)

    # Step a: load conventions
    conventions = load_conventions()

    # Step b: load template
    template = get_template(template_name)

    # Step c: load content manifest
    manifest = load_content_manifest(content_path)

    # Override template_id from manifest if provided (use caller's template_name as authoritative)
    render_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Step c.5: resolve component configs
    component_configs = _resolve_component_configs(manifest, template)

    # Step d: call each component's render() and collect fragments
    component_fragments: dict[str, str] = {}
    needs_d3 = False

    for comp_id, config in component_configs.items():
        try:
            mod = get_component(comp_id)
            fragment = mod.render(config)
            component_fragments[comp_id] = fragment
            if comp_id == "d3-vocabulary-network":
                needs_d3 = True
        except ValueError:
            # Component not in registry (e.g., dashboard-filter-bar) — skip
            pass

    # Step d.5: check if d3 is requested via manifest components overriding template
    for comp_entry in manifest.get("components", []):
        if comp_entry.get("id") == "d3-vocabulary-network":
            if "d3-vocabulary-network" not in component_fragments:
                try:
                    mod = get_component("d3-vocabulary-network")
                    cfg = comp_entry.get("config", {"nodes": [], "links": []})
                    component_fragments["d3-vocabulary-network"] = mod.render(cfg)
                    needs_d3 = True
                except ValueError:
                    pass

    # Step e: assemble full HTML
    html = _assemble_html(
        manifest=manifest,
        template=template,
        conventions=conventions,
        component_fragments=component_fragments,
        needs_d3=needs_d3,
        render_timestamp=render_timestamp,
    )

    # Step f: post-render validation
    validate_html(html, manifest)

    # Step g: write output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    return html


def render_and_upload(
    content_path: Path | str,
    template_id: str,
    output_filename: str,
) -> str:
    """Compile HTML and write to the bisque-uploads directory.

    Args:
        content_path: Path to the JSON content manifest.
        template_id: Template ID from registry.yaml.
        output_filename: Filename for the output HTML (e.g., 'my-doc.html').

    Returns:
        The public Bisque URL for the output file.
    """
    uploads = _uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    output_path = uploads / output_filename

    render(content_path, template_id, output_path)

    base_url = _bisque_base_url()
    return f"{base_url}/files/{output_filename}"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lobster HTML renderer — compile a JSON content manifest into HTML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run src/htmlgen/renderer.py --content manifest.json --template document-class --output out.html
  uv run src/htmlgen/renderer.py --content manifest.json --template spec-document --output /tmp/spec.html
        """,
    )
    parser.add_argument("--content", required=True, help="Path to JSON content manifest")
    parser.add_argument("--template", required=True, help="Template ID (document-class, spec-document, dashboard-class)")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    parser.add_argument("--bisque", action="store_true", help="Write to bisque-uploads dir and print Bisque URL")

    args = parser.parse_args()

    content_path = Path(args.content)
    output_path = Path(args.output)

    if args.bisque:
        url = render_and_upload(content_path, args.template, output_path.name)
        print(url)
    else:
        try:
            render(content_path, args.template, output_path)
            print(f"Rendered: {output_path}")
        except ValidationError as exc:
            print(f"Validation failed: {exc}", file=sys.stderr)
            sys.exit(1)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
