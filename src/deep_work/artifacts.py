"""Artifact storage and retrieval for async deep work outputs."""
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ARTIFACTS_DIR = Path.home() / "lobster-workspace" / "artifacts"
MANIFEST_PATH = ARTIFACTS_DIR / "manifest.json"


@dataclass
class Artifact:
    slug: str
    title: str
    created_at: str  # ISO 8601
    source: str      # "user_request", "scheduled", etc.
    summary: str     # 2-3 sentence abstract
    path: str        # relative to ARTIFACTS_DIR
    tags: list[str]


def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    return json.loads(MANIFEST_PATH.read_text())


def _save_manifest(entries: list[dict]) -> None:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(entries, indent=2))


def write_artifact(
    slug: str,
    title: str,
    body: str,
    summary: str,
    source: str = "user_request",
    tags: list[str] | None = None,
) -> Path:
    """Write artifact markdown file with YAML front matter. Returns path."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    filename = f"{slug}.md"
    path = ARTIFACTS_DIR / filename

    front_matter = (
        f"---\n"
        f"slug: {slug}\n"
        f"title: {title}\n"
        f"created_at: {now}\n"
        f"source: {source}\n"
        f"summary: |\n"
        + "\n".join(f"  {line}" for line in summary.splitlines()) + "\n"
        f"tags: {json.dumps(tags or [])}\n"
        f"---\n\n"
    )
    path.write_text(front_matter + body)

    # Update manifest
    entries = _load_manifest()
    entries = [e for e in entries if e.get("slug") != slug]  # dedup
    entries.insert(0, {
        "slug": slug,
        "title": title,
        "created_at": now,
        "source": source,
        "summary": summary,
        "path": filename,
        "tags": tags or [],
    })
    _save_manifest(entries)
    return path


def list_artifacts(limit: int = 20) -> list[Artifact]:
    """Return most recent artifacts from manifest."""
    entries = _load_manifest()
    return [Artifact(**e) for e in entries[:limit]]


def get_artifact(slug: str) -> Optional[tuple[Artifact, str]]:
    """Return (Artifact metadata, full body text) or None if not found."""
    entries = _load_manifest()
    meta = next((e for e in entries if e["slug"] == slug), None)
    if not meta:
        return None
    path = ARTIFACTS_DIR / meta["path"]
    if not path.exists():
        return None
    return Artifact(**meta), path.read_text()


def find_recent_similar(title: str, days: int = 7) -> Optional[Artifact]:
    """Check if a similar artifact was produced within the last N days."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    title_words = set(title.lower().split())
    for entry in _load_manifest():
        created = datetime.fromisoformat(entry["created_at"])
        if created < cutoff:
            continue
        existing_words = set(entry["title"].lower().split())
        overlap = len(title_words & existing_words)
        if overlap >= max(2, len(title_words) // 2):
            return Artifact(**entry)
    return None
