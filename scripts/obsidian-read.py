#!/usr/bin/env python3
"""
obsidian-read.py — Lobster vault reader for Obsidian markdown files.

Usage:
  uv run ~/lobster/scripts/obsidian-read.py --query "meeting notes" --limit 5
  uv run ~/lobster/scripts/obsidian-read.py --path "Daily Notes/2026-03-23.md"
  uv run ~/lobster/scripts/obsidian-read.py --list
  uv run ~/lobster/scripts/obsidian-read.py --recent 10
"""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime

VAULT_DIR = Path.home() / "lobster-workspace" / "obsidian-vault"
EXCERPT_LINES = 10


def find_all_md_files(vault: Path) -> list[Path]:
    return sorted(
        vault.rglob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def excerpt(text: str, max_lines: int = EXCERPT_LINES) -> str:
    lines = text.splitlines()
    snippet = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        snippet += f"\n... ({len(lines) - max_lines} more lines)"
    return snippet


def search_query(vault: Path, query: str, limit: int) -> None:
    terms = query.lower().split()
    results = []

    for md_file in find_all_md_files(vault):
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        content_lower = content.lower()
        if all(term in content_lower for term in terms):
            rel = md_file.relative_to(vault)
            mtime = datetime.fromtimestamp(md_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            results.append((rel, mtime, content))
            if len(results) >= limit:
                break

    if not results:
        print(f"No results found for: {query}")
        return

    print(f"Found {len(results)} result(s) for '{query}':\n")
    for rel, mtime, content in results:
        print(f"=== {rel} (modified {mtime}) ===")
        print(excerpt(content))
        print()


def read_path(vault: Path, file_path: str) -> None:
    target = vault / file_path
    if not target.exists():
        matches = list(vault.rglob(file_path))
        if not matches:
            print(f"File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        target = matches[0]

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        rel = target.relative_to(vault)
        mtime = datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"=== {rel} (modified {mtime}) ===\n")
        print(content)
    except Exception as e:
        print(f"Error reading {file_path}: {e}", file=sys.stderr)
        sys.exit(1)


def list_files(vault: Path) -> None:
    files = find_all_md_files(vault)
    if not files:
        print("Vault is empty or not yet synced.")
        return
    print(f"Vault contains {len(files)} markdown file(s):\n")
    for f in files:
        rel = f.relative_to(vault)
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        size = f.stat().st_size
        print(f"  {rel}  ({mtime}, {size} bytes)")


def recent_files(vault: Path, n: int) -> None:
    files = find_all_md_files(vault)[:n]
    if not files:
        print("Vault is empty or not yet synced.")
        return
    print(f"Most recently modified {len(files)} file(s):\n")
    for f in files:
        rel = f.relative_to(vault)
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = ""
        print(f"=== {rel} (modified {mtime}) ===")
        print(excerpt(content, max_lines=5))
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Read and search Obsidian vault markdown files on the VPS."
    )
    parser.add_argument("--vault", default=str(VAULT_DIR), help="Path to vault directory")
    parser.add_argument("--query", "-q", help="Search query (all terms must match)")
    parser.add_argument("--path", "-p", help="Read a specific file by relative path")
    parser.add_argument("--list", "-l", action="store_true", help="List all markdown files")
    parser.add_argument("--recent", "-r", type=int, metavar="N", help="Show N most recently modified files")
    parser.add_argument("--limit", type=int, default=5, help="Max results for --query (default: 5)")

    args = parser.parse_args()
    vault = Path(args.vault)

    if not vault.exists():
        print(f"Vault directory not found: {vault}", file=sys.stderr)
        print("Run: mkdir -p ~/lobster-workspace/obsidian-vault", file=sys.stderr)
        sys.exit(1)

    if args.query:
        search_query(vault, args.query, args.limit)
    elif args.path:
        read_path(vault, args.path)
    elif args.list:
        list_files(vault)
    elif args.recent:
        recent_files(vault, args.recent)
    else:
        parser.print_help()
        print(f"\nVault directory: {vault}")
        files = find_all_md_files(vault)
        print(f"Current vault file count: {len(files)}")


if __name__ == "__main__":
    main()
