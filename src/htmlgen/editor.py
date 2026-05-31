"""
editor.py — DOM-aware HTML editor for Lobster HTML artifacts (Phase 3)

Applies structured edit instructions to existing HTML files without requiring
agents to load or regenerate the full document. Implements GP-3 (abstract edit
instructions, deterministic execution) from the HTML doc model spec.

The split: the LLM identifies *what* to change and expresses it as an edit
instruction. This script applies the change deterministically. The LLM never
outputs modified HTML; the DOM is always the execution target.

Instruction types
-----------------
replace_section_content(section_id, new_content)
    Replace the prose body of a section, preserving the section's <div>,
    heading, comment-area, and label elements intact.

add_section(section_id, after_id, title, label, content)
    Insert a new section after the named section.

remove_section(section_id)
    Remove a section element (and all its children) from the document.
    The section_id is retired — callers must not reuse it.

update_version_stamp(version, timestamp)
    Update the doc-version and doc-updated meta tags.

patch_js_block(block_id, new_code)
    Replace a named <script> block. The block must carry a data-block-id
    attribute so the editor can locate it unambiguously.

Usage
-----
    from src.htmlgen.editor import apply_edit, apply_edits, find_sections_by_content
    from pathlib import Path

    # Single edit — returns (modified_html, EditTrace)
    html, trace = apply_edit(
        html_path=Path("path/to/file.html"),
        instruction={
            "op": "replace_section_content",
            "section_id": "s9",
            "new_content": "Revised prose here.",
        },
    )

    # Batch edits — returns (modified_html, EditTrace)
    html, trace = apply_edits(
        html_path=Path("path/to/file.html"),
        instructions=[
            {"op": "replace_section_content", "section_id": "s3", "new_content": "..."},
            {"op": "update_version_stamp", "version": "1.4", "timestamp": "2026-05-31T00:00:00Z"},
        ],
    )

    # Discover sections by content — read-only, returns list of section IDs
    ids = find_sections_by_content(Path("path/to/file.html"), "search term")
    ids = find_sections_by_content(Path("path/to/file.html"), "p.highlight", mode="css")

Edit instruction format
-----------------------
Every instruction is a dict with at minimum an "op" key.

replace_section_content:
    {
        "op": "replace_section_content",
        "section_id": "s9",
        "new_content": "<p>Prose or raw HTML to place inside the section.</p>"
    }
    new_content may be raw HTML or plain text (plain text is wrapped in <p> tags).

add_section:
    {
        "op": "add_section",
        "section_id": "s10",
        "after_id": "s9",
        "title": "New Section Title",
        "label": "§10",
        "content": "Section content here."
    }

remove_section:
    {
        "op": "remove_section",
        "section_id": "s4"
    }

update_version_stamp:
    {
        "op": "update_version_stamp",
        "version": "1.4",
        "timestamp": "2026-05-31T00:00:00Z"
    }
    Both version and timestamp are optional; omit either to leave unchanged.

patch_js_block:
    {
        "op": "patch_js_block",
        "block_id": "my-block",
        "new_code": "console.log('hello');"
    }
    The target <script> must have data-block-id="my-block".

All ops are applied in order. If any op raises EditError, the batch stops and
the file is not written (atomic on success, no partial-write on error).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

# Path where edit traces are appended as JSONL.
# Exposed as a module-level constant so tests can monkeypatch it.
TRACE_LOG_PATH = Path.home() / "lobster-workspace" / "data" / "html-edit-traces.jsonl"


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class EditError(Exception):
    """Raised when an edit instruction cannot be applied."""


# ---------------------------------------------------------------------------
# EditTrace — lightweight record of what an edit call accessed and changed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EditTrace:
    """Immutable record of what a single apply_edit / apply_edits call did.

    Returned as the second element of the tuple from apply_edit and apply_edits,
    and appended to TRACE_LOG_PATH as a JSONL entry.
    """
    doc_path: str
    doc_size_bytes: int
    sections_total: int           # total sections in the document after the edit
    sections_accessed: list[str]  # section IDs that were read or targeted
    operations_attempted: int
    operations_succeeded: int
    write_succeeded: bool
    operation_types: list[str]    # op names attempted, e.g. ["replace_section_content"]


# ---------------------------------------------------------------------------
# Parser helpers — pure functions over BeautifulSoup trees
# ---------------------------------------------------------------------------

_PARSER = "html.parser"


def _parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, _PARSER)


def _find_section(soup: BeautifulSoup, section_id: str) -> Tag:
    """Return the <div class="section" id="section_id"> element, or raise EditError."""
    el = soup.find("div", {"class": "section", "id": section_id})
    if el is None:
        raise EditError(
            f"Section '{section_id}' not found. "
            "The file must contain <div class=\"section\" id=\"{section_id}\">."
        )
    return el


def _is_structural_child(tag: Tag) -> bool:
    """Return True if tag is a structural element that must be preserved during
    a replace_section_content operation (label, heading, comment-area)."""
    cls = tag.get("class", [])
    if isinstance(cls, str):
        cls = cls.split()
    return any(c in cls for c in ("section-label", "comment-area")) or tag.name in ("h1", "h2", "h3")


# ---------------------------------------------------------------------------
# Individual operation functions — pure transformations on a soup tree
# ---------------------------------------------------------------------------

def _op_replace_section_content(
    soup: BeautifulSoup,
    section_id: str,
    new_content: str,
) -> None:
    """Replace the content body of a section, preserving structural children."""
    section = _find_section(soup, section_id)

    # Collect structural elements to preserve (label div, heading, comment-area)
    structural = [
        child for child in list(section.children)
        if isinstance(child, Tag) and _is_structural_child(child)
    ]

    # Clear the section's children
    section.clear()

    # Re-insert structural elements first
    for el in structural:
        section.append(el)

    # Append new content — parse as HTML fragment
    new_soup = BeautifulSoup(new_content, _PARSER)
    # If new_content is plain text (no block tags), wrap in <p>
    block_tags = {"p", "ul", "ol", "pre", "blockquote", "table", "h4", "h5", "h6", "div"}
    has_block = any(
        isinstance(child, Tag) and child.name in block_tags
        for child in new_soup.children
    )
    if not has_block and new_content.strip():
        p = soup.new_tag("p")
        p.string = new_content.strip()
        section.append(p)
    else:
        for child in list(new_soup.children):
            section.append(child.__copy__() if hasattr(child, "__copy__") else child)


def _op_add_section(
    soup: BeautifulSoup,
    section_id: str,
    after_id: str,
    title: str,
    label: str,
    content: str,
) -> None:
    """Insert a new section after the section with after_id."""
    after_el = _find_section(soup, after_id)

    # Verify the new section_id doesn't already exist
    existing = soup.find("div", {"class": "section", "id": section_id})
    if existing is not None:
        raise EditError(
            f"Section '{section_id}' already exists. "
            "Section IDs must be unique and permanent."
        )

    # Build the new section element
    new_section = BeautifulSoup(
        _build_section_html(section_id, label, title, content), _PARSER
    ).find("div", {"class": "section"})

    if new_section is None:
        raise EditError(f"Failed to build new section element for '{section_id}'")

    after_el.insert_after(new_section)


def _op_remove_section(
    soup: BeautifulSoup,
    section_id: str,
) -> None:
    """Remove a section and all its children from the document."""
    section = _find_section(soup, section_id)
    section.decompose()


def _op_update_version_stamp(
    soup: BeautifulSoup,
    version: str | None,
    timestamp: str | None,
) -> None:
    """Update doc-version and/or doc-updated meta tags."""
    if version is not None:
        meta_version = soup.find("meta", {"name": "doc-version"})
        if meta_version is None:
            raise EditError(
                "doc-version meta tag not found. "
                "The document must have <meta name=\"doc-version\" content=\"...\">."
            )
        meta_version["content"] = version

    if timestamp is not None:
        meta_updated = soup.find("meta", {"name": "doc-updated"})
        if meta_updated is None:
            raise EditError(
                "doc-updated meta tag not found. "
                "The document must have <meta name=\"doc-updated\" content=\"...\">."
            )
        meta_updated["content"] = timestamp


def _op_patch_js_block(
    soup: BeautifulSoup,
    block_id: str,
    new_code: str,
) -> None:
    """Replace the content of a named <script data-block-id="block_id"> element."""
    script = soup.find("script", {"data-block-id": block_id})
    if script is None:
        raise EditError(
            f"No <script data-block-id=\"{block_id}\"> found. "
            "JS blocks must carry a data-block-id attribute to be patchable."
        )
    script.clear()
    script.append(soup.new_string(new_code))


# ---------------------------------------------------------------------------
# Section HTML builder (used by add_section)
# ---------------------------------------------------------------------------

def _build_section_html(
    section_id: str,
    label: str,
    title: str,
    content: str,
) -> str:
    """Produce a well-formed section HTML string."""
    label_html = f'<div class="section-label">{label}</div>' if label else ""
    title_html = (
        f'<h2>{title} <button class="comment-btn" onclick="toggleComment(this)">&#128172;</button></h2>'
        if title else ""
    )
    comment_area_html = (
        f'<div class="comment-area"><textarea placeholder="[{label or section_id}] "></textarea></div>'
        if title else ""
    )
    # Wrap plain text content in <p> if it has no block tags
    block_re = re.compile(r"<(p|ul|ol|pre|blockquote|table|h[4-6]|div)[\s>]", re.IGNORECASE)
    if content.strip() and not block_re.search(content):
        content_html = f"<p>{content}</p>"
    else:
        content_html = content

    return (
        f'<div class="section" id="{section_id}">\n'
        f"  {label_html}\n"
        f"  {title_html}\n"
        f"  {comment_area_html}\n"
        f"  {content_html}\n"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Instruction dispatcher — pure mapping from instruction dict to op function
# ---------------------------------------------------------------------------

def _apply_instruction(
    soup: BeautifulSoup,
    instruction: dict[str, Any],
    sections_accessed: list[str],
) -> None:
    """Dispatch a single edit instruction to its operation function.

    Mutates sections_accessed in place: appends any section_id that the
    instruction targets so the caller can build an EditTrace.
    """
    op = instruction.get("op")
    if not op:
        raise EditError("Instruction missing required 'op' field.")

    if op == "replace_section_content":
        _require_fields(instruction, ["section_id", "new_content"])
        sid = instruction["section_id"]
        sections_accessed.append(sid)
        _op_replace_section_content(
            soup,
            section_id=sid,
            new_content=instruction["new_content"],
        )

    elif op == "add_section":
        _require_fields(instruction, ["section_id", "after_id", "title", "label"])
        sid = instruction["section_id"]
        after_id = instruction["after_id"]
        if sid not in sections_accessed:
            sections_accessed.append(sid)
        if after_id not in sections_accessed:
            sections_accessed.append(after_id)
        _op_add_section(
            soup,
            section_id=sid,
            after_id=after_id,
            title=instruction["title"],
            label=instruction["label"],
            content=instruction.get("content", ""),
        )

    elif op == "remove_section":
        _require_fields(instruction, ["section_id"])
        sid = instruction["section_id"]
        sections_accessed.append(sid)
        _op_remove_section(soup, section_id=sid)

    elif op == "update_version_stamp":
        _op_update_version_stamp(
            soup,
            version=instruction.get("version"),
            timestamp=instruction.get("timestamp"),
        )

    elif op == "patch_js_block":
        _require_fields(instruction, ["block_id", "new_code"])
        _op_patch_js_block(
            soup,
            block_id=instruction["block_id"],
            new_code=instruction["new_code"],
        )

    else:
        raise EditError(
            f"Unknown op: '{op}'. "
            "Valid ops: replace_section_content, add_section, remove_section, "
            "update_version_stamp, patch_js_block."
        )


def _require_fields(instruction: dict[str, Any], fields: list[str]) -> None:
    """Raise EditError if any required field is missing from the instruction."""
    missing = [f for f in fields if f not in instruction]
    if missing:
        raise EditError(
            f"Instruction op='{instruction.get('op')}' missing required fields: "
            + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# Trace helpers — pure functions for building and persisting EditTrace
# ---------------------------------------------------------------------------

def _build_trace(
    html_path: Path,
    soup: BeautifulSoup,
    sections_accessed: list[str],
    operations_attempted: int,
    operations_succeeded: int,
    write_succeeded: bool,
    operation_types: list[str],
    doc_size_bytes: int,
) -> EditTrace:
    """Construct an EditTrace from the post-edit soup tree and accumulated metrics."""
    sections_total = len(soup.find_all("div", class_="section"))
    return EditTrace(
        doc_path=str(html_path),
        doc_size_bytes=doc_size_bytes,
        sections_total=sections_total,
        sections_accessed=list(sections_accessed),
        operations_attempted=operations_attempted,
        operations_succeeded=operations_succeeded,
        write_succeeded=write_succeeded,
        operation_types=list(operation_types),
    )


def _append_trace(trace: EditTrace) -> None:
    """Append an EditTrace as a JSONL entry to TRACE_LOG_PATH.

    Creates the file and parent directories if they do not exist.
    The entry includes an ISO-8601 UTC timestamp field alongside the trace fields.
    Errors during append are swallowed to avoid masking edit results.
    """
    try:
        TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **asdict(trace),
        }
        with TRACE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass  # trace logging must never break the edit pipeline


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_edit(
    html_path: Path | str,
    instruction: dict[str, Any],
) -> tuple[str, EditTrace]:
    """Apply a single edit instruction to an HTML file.

    Reads the file, applies the instruction, writes the file back, and appends
    an EditTrace entry to TRACE_LOG_PATH.

    Args:
        html_path: Path to the HTML file to modify.
        instruction: A single edit instruction dict (see module docstring).

    Returns:
        A tuple (modified_html, trace) where modified_html is the full HTML
        string after the edit and trace is an EditTrace capturing what was done.

    Raises:
        FileNotFoundError: If html_path does not exist.
        EditError: If the instruction is malformed or cannot be applied.
    """
    return apply_edits(html_path, [instruction])


def apply_edits(
    html_path: Path | str,
    instructions: list[dict[str, Any]],
) -> tuple[str, EditTrace]:
    """Apply a batch of edit instructions to an HTML file.

    All instructions are applied to the same soup tree in order. If any
    instruction raises EditError, the batch stops and the file is NOT written
    (atomic on success, no partial-write on error). An EditTrace is built and
    appended to TRACE_LOG_PATH regardless of success or failure.

    Args:
        html_path: Path to the HTML file to modify.
        instructions: Ordered list of edit instruction dicts.

    Returns:
        A tuple (modified_html, trace) where modified_html is the full HTML
        string after the edit and trace is an EditTrace capturing what was done.

    Raises:
        FileNotFoundError: If html_path does not exist.
        EditError: If any instruction is malformed or cannot be applied.
    """
    html_path = Path(html_path)
    if not html_path.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")

    original = html_path.read_text(encoding="utf-8")
    doc_size_bytes = len(original.encode("utf-8"))
    soup = _parse(original)

    sections_accessed: list[str] = []
    operation_types: list[str] = []
    operations_succeeded = 0

    for i, instruction in enumerate(instructions):
        op = instruction.get("op", "")
        operation_types.append(op)
        try:
            _apply_instruction(soup, instruction, sections_accessed)
            operations_succeeded += 1
        except EditError as exc:
            trace = _build_trace(
                html_path=html_path,
                soup=soup,
                sections_accessed=sections_accessed,
                operations_attempted=len(instructions),
                operations_succeeded=operations_succeeded,
                write_succeeded=False,
                operation_types=operation_types,
                doc_size_bytes=doc_size_bytes,
            )
            _append_trace(trace)
            raise EditError(
                f"Instruction {i} (op={instruction.get('op')!r}) failed: {exc}"
            ) from exc

    modified = str(soup)
    html_path.write_text(modified, encoding="utf-8")

    trace = _build_trace(
        html_path=html_path,
        soup=soup,
        sections_accessed=sections_accessed,
        operations_attempted=len(instructions),
        operations_succeeded=operations_succeeded,
        write_succeeded=True,
        operation_types=operation_types,
        doc_size_bytes=doc_size_bytes,
    )
    _append_trace(trace)
    return modified, trace


def find_sections_by_content(
    html_path: Path | str,
    pattern: str,
    mode: str = "text",
) -> list[str]:
    """Return section IDs for sections whose content matches the given pattern.

    This is a read-only operation — the document is not modified.

    Args:
        html_path: Path to the HTML file to search.
        pattern: The search pattern. In text mode, a case-insensitive substring
            to look for in each section's text content. In CSS mode, a CSS
            selector evaluated within each section element.
        mode: "text" (default) searches section text content for a
            case-insensitive substring match. "css" applies pattern as a CSS
            selector within each section element.

    Returns:
        List of section ID strings (in document order) for sections that
        contain a match. Returns an empty list if no sections match or if the
        document has no sections.

    Raises:
        FileNotFoundError: If html_path does not exist.
    """
    soup = parse_html(html_path)
    sections = soup.find_all("div", class_="section")
    matched: list[str] = []

    for section in sections:
        sid = section.get("id")
        if not sid:
            continue
        sid = str(sid)

        if mode == "css":
            if section.select(pattern):
                matched.append(sid)
        else:
            # text mode: case-insensitive substring match on the section's full text
            if pattern.lower() in section.get_text().lower():
                matched.append(sid)

    return matched


def parse_html(html_path: Path | str) -> BeautifulSoup:
    """Parse an HTML file and return the BeautifulSoup tree.

    Utility for callers that need to inspect the DOM before composing edit
    instructions (e.g., to enumerate existing section IDs).

    Args:
        html_path: Path to the HTML file.

    Returns:
        A BeautifulSoup tree.

    Raises:
        FileNotFoundError: If html_path does not exist.
    """
    html_path = Path(html_path)
    if not html_path.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")
    return _parse(html_path.read_text(encoding="utf-8"))


def list_section_ids(html_path: Path | str) -> list[str]:
    """Return all section IDs present in an HTML file.

    Reads <div class="section" id="..."> elements and returns their IDs in
    document order. Useful for coverage checks and section enumeration without
    loading the full file into an agent's context.

    Args:
        html_path: Path to the HTML file.

    Returns:
        List of section ID strings in document order.

    Raises:
        FileNotFoundError: If html_path does not exist.
    """
    soup = parse_html(html_path)
    return [
        str(div["id"])
        for div in soup.find_all("div", class_="section")
        if div.get("id")
    ]
