#!/usr/bin/env python3
"""
scripts/categorization.py — Categorization foundation for Lobster

Implements three operations over Category objects stored as JSON files in
~/lobster-user-config/memory/categories/:

  compress(threshold=0.92)
      Find category pairs whose centroid embeddings are close enough to merge,
      then merge them — keeping the larger category's id, synthesizing a new
      description via the Anthropic API.

  group(items, existing_categories)
      Assign each item to the nearest existing category (if similarity > 0.75),
      or create a new category.  Returns the updated category list.

  refactor(category_id, operation, params)
      Structural operations: split (sub-cluster), rename, or unbundle
      (move members to other categories).

Design principles:
  - Pure functions receive all state they need; I/O is isolated to load/save helpers
  - Categories are immutable dicts once returned from load; writes go through save_category
  - Embeddings are computed lazily and cached in memory for the process lifetime
  - The embedding model is the same 384-dim model already used in events_vec
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional heavy imports — deferred so the module is importable without them
# ---------------------------------------------------------------------------

try:
    import sqlite_vec as _sqlite_vec
    _SQLITE_VEC_AVAILABLE = True
except ImportError:
    _SQLITE_VEC_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_CATEGORIES_DIR = Path.home() / "lobster-user-config" / "memory" / "categories"
_DEFAULT_MEMORY_DB = Path.home() / "lobster-workspace" / "data" / "memory.db"
_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Embedding layer — thin wrapper, uses fastembed with a process-level cache
# ---------------------------------------------------------------------------

_embed_model = None
_embed_cache: dict[str, list[float]] = {}


def _get_embed_model():
    """Return a cached TextEmbedding model (lazy init)."""
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding()
    return _embed_model


def embed_text(text: str) -> list[float]:
    """
    Return a 384-dim embedding for *text*.

    Results are cached by text content for the process lifetime so repeated
    calls on the same string are free.
    """
    if text in _embed_cache:
        return _embed_cache[text]
    model = _get_embed_model()
    embedding = list(map(float, list(model.embed([text]))[0]))
    _embed_cache[text] = embedding
    return embedding


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of texts, using the cache where possible."""
    uncached = [t for t in texts if t not in _embed_cache]
    if uncached:
        model = _get_embed_model()
        for text, emb in zip(uncached, model.embed(uncached)):
            _embed_cache[text] = list(map(float, emb))
    return [_embed_cache[t] for t in texts]


# ---------------------------------------------------------------------------
# Vector math — pure functions over plain Python lists
# ---------------------------------------------------------------------------

def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity in [0, 1] for two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def centroid(embeddings: list[list[float]]) -> list[float]:
    """Return the element-wise mean of a list of equal-length vectors."""
    if not embeddings:
        return [0.0] * _EMBEDDING_DIM
    dim = len(embeddings[0])
    total = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            total[i] += v
    n = len(embeddings)
    return [v / n for v in total]


# ---------------------------------------------------------------------------
# Category data model — plain dicts, validated on load
# ---------------------------------------------------------------------------

def make_category(
    name: str,
    description: str,
    members: list[str] | None = None,
    centroid_embedding: list[float] | None = None,
    category_id: str | None = None,
    meta_thread_id: str | None = None,
) -> dict:
    """Return a new Category dict with all required fields."""
    now = _iso_now()
    return {
        "id": category_id or str(uuid.uuid4()),
        "name": name,
        "description": description,
        "members": members or [],
        "centroid_embedding": centroid_embedding or [],
        "created_at": now,
        "updated_at": now,
        "meta_thread_id": meta_thread_id,
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_ts(category: dict) -> dict:
    """Return a copy of *category* with updated_at set to now."""
    return {**category, "updated_at": _iso_now()}


# ---------------------------------------------------------------------------
# Persistence — I/O isolated to these two functions
# ---------------------------------------------------------------------------

def load_categories(categories_dir: Path | None = None) -> list[dict]:
    """
    Load all Category JSON files from *categories_dir*.

    Returns an empty list if the directory does not exist.
    """
    d = categories_dir or _DEFAULT_CATEGORIES_DIR
    if not d.exists():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(d.glob("*.json"))
    ]


def save_category(category: dict, categories_dir: Path | None = None) -> None:
    """
    Write *category* to {categories_dir}/{id}.json atomically (write-then-rename).
    """
    d = categories_dir or _DEFAULT_CATEGORIES_DIR
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{category['id']}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(category, indent=2), encoding="utf-8")
    tmp.replace(target)


def delete_category(category_id: str, categories_dir: Path | None = None) -> None:
    """Remove a category file if it exists."""
    d = categories_dir or _DEFAULT_CATEGORIES_DIR
    path = d / f"{category_id}.json"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# LLM helper — synthesize a description for a merged category
# ---------------------------------------------------------------------------

def _synthesize_description(name_a: str, desc_a: str, name_b: str, desc_b: str) -> str:
    """
    Call the Anthropic API to synthesize a merged description.

    Falls back to a simple concatenation if the API is unavailable.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = (
            f"Two semantic categories are being merged.\n\n"
            f"Category A — {name_a}: {desc_a}\n"
            f"Category B — {name_b}: {desc_b}\n\n"
            "Write a single concise sentence (max 30 words) describing what belongs "
            "in the merged category. Focus on the unifying theme. No preamble."
        )
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception:
        return f"{desc_a} Also includes: {desc_b}"


# ---------------------------------------------------------------------------
# compress — merge near-duplicate categories
# ---------------------------------------------------------------------------

def compress(
    threshold: float = 0.92,
    categories_dir: Path | None = None,
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """
    Find all category pairs whose centroid cosine similarity >= *threshold*
    and merge them.

    Merging keeps the larger category's id, absorbs the smaller's members,
    and synthesizes a new description via LLM.

    Args:
        threshold:      Minimum cosine similarity to trigger a merge (0–1).
        categories_dir: Override for storage directory.
        dry_run:        If True, return merge candidates without writing.

    Returns:
        List of (kept_id, absorbed_id) pairs that were merged.
    """
    categories = load_categories(categories_dir)
    merged_pairs: list[tuple[str, str]] = []

    # Build a set of ids we've already absorbed so they're skipped
    absorbed: set[str] = set()

    # Work with a mutable list for in-place updates within this function
    cat_by_id: dict[str, dict] = {c["id"]: dict(c) for c in categories}
    ids = list(cat_by_id.keys())

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            id_a, id_b = ids[i], ids[j]
            if id_a in absorbed or id_b in absorbed:
                continue

            cat_a = cat_by_id[id_a]
            cat_b = cat_by_id[id_b]

            emb_a = cat_a.get("centroid_embedding", [])
            emb_b = cat_b.get("centroid_embedding", [])
            if not emb_a or not emb_b or len(emb_a) != len(emb_b):
                continue

            sim = cosine_similarity(emb_a, emb_b)
            if sim < threshold:
                continue

            # Decide which to keep (the one with more members)
            len_a = len(cat_a.get("members", []))
            len_b = len(cat_b.get("members", []))
            if len_a >= len_b:
                kept, gone = cat_a, cat_b
            else:
                kept, gone = cat_b, cat_a

            merged_pairs.append((kept["id"], gone["id"]))
            absorbed.add(gone["id"])

            if not dry_run:
                # Merge members (deduplicated)
                merged_members = list(
                    dict.fromkeys(kept.get("members", []) + gone.get("members", []))
                )
                # Recompute centroid over merged members
                new_desc = _synthesize_description(
                    kept["name"], kept["description"],
                    gone["name"], gone["description"],
                )
                merged_emb = centroid([emb_a, emb_b])
                updated = _update_ts({
                    **kept,
                    "members": merged_members,
                    "centroid_embedding": merged_emb,
                    "description": new_desc,
                })
                cat_by_id[kept["id"]] = updated
                save_category(updated, categories_dir)
                delete_category(gone["id"], categories_dir)

    return merged_pairs


# ---------------------------------------------------------------------------
# group — assign items to categories or create new ones
# ---------------------------------------------------------------------------

def group(
    items: list[dict],
    existing_categories: list[dict],
    assign_threshold: float = 0.75,
    categories_dir: Path | None = None,
) -> list[dict]:
    """
    Best-effort semantic grouping.

    Each item in *items* must have at least one of the keys 'text', 'content',
    or 'description'.  Items are embedded and assigned to the nearest existing
    category if similarity >= *assign_threshold*; otherwise a new category is
    created.

    The centroid of each modified category is recomputed after all assignments.

    Args:
        items:               List of dicts with text content.
        existing_categories: Current category list (not mutated).
        assign_threshold:    Minimum cosine similarity for assignment (0–1).
        categories_dir:      Override for storage directory.

    Returns:
        Updated list of all categories (existing + any new ones).
    """
    # Shallow-copy categories so we don't mutate the caller's list
    cats: list[dict] = [dict(c) for c in existing_categories]

    def _item_text(item: dict) -> str:
        return item.get("text") or item.get("content") or item.get("description") or ""

    def _item_id(item: dict) -> str:
        return str(item.get("id") or item.get("message_id") or _item_text(item)[:40])

    # Batch-embed all items
    texts = [_item_text(it) for it in items]
    embeddings = embed_texts(texts)

    # Track new members added to each category (by category list index)
    added_members: dict[int, list[str]] = {i: [] for i in range(len(cats))}
    new_categories: list[dict] = []

    for item, emb in zip(items, embeddings):
        if not emb:
            continue
        item_id = _item_id(item)

        # Find nearest existing category
        best_idx: int | None = None
        best_sim = 0.0
        for idx, cat in enumerate(cats):
            cat_emb = cat.get("centroid_embedding", [])
            if not cat_emb or len(cat_emb) != len(emb):
                continue
            sim = cosine_similarity(emb, cat_emb)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx

        if best_idx is not None and best_sim >= assign_threshold:
            if item_id not in cats[best_idx].get("members", []):
                cats[best_idx] = {
                    **cats[best_idx],
                    "members": cats[best_idx].get("members", []) + [item_id],
                }
                added_members[best_idx].append(item_id)
        else:
            # Create a new category from this item
            text = _item_text(item)
            name = text[:60].strip() or "Unnamed"
            new_cat = make_category(
                name=name,
                description=text[:200],
                members=[item_id],
                centroid_embedding=emb,
            )
            new_categories.append(new_cat)

    # Recompute centroids for modified existing categories
    for idx, new_member_ids in added_members.items():
        if not new_member_ids:
            continue
        cat = cats[idx]
        # Re-embed all members to get an accurate centroid
        # (we only have text for items passed in this call; use existing centroid
        #  weighted with new embeddings as an approximation)
        new_embs = [embed_text(_item_text(it)) for it in items
                    if _item_id(it) in new_member_ids]
        existing_emb = cat.get("centroid_embedding", [])
        if existing_emb and new_embs:
            updated_emb = centroid([existing_emb] + new_embs)
        elif new_embs:
            updated_emb = centroid(new_embs)
        else:
            updated_emb = existing_emb
        cats[idx] = _update_ts({**cat, "centroid_embedding": updated_emb})

    # Save modified existing categories
    if categories_dir or _DEFAULT_CATEGORIES_DIR.exists():
        for idx in added_members:
            if added_members[idx]:
                save_category(cats[idx], categories_dir)

    # Save new categories
    for new_cat in new_categories:
        save_category(new_cat, categories_dir)

    return cats + new_categories


# ---------------------------------------------------------------------------
# refactor — structural operations
# ---------------------------------------------------------------------------

def refactor(
    category_id: str,
    operation: str,
    params: dict,
    categories_dir: Path | None = None,
) -> list[dict]:
    """
    Structural refactoring operations on a single category.

    Args:
        category_id: ID of the category to operate on.
        operation:   One of "split", "rename", "unbundle".
        params:      Operation-specific parameters (see below).
        categories_dir: Override for storage directory.

    Returns:
        List of Category dicts that resulted from the operation.
        For rename: [updated_category]
        For split:  [new_category_1, new_category_2, ...]
        For unbundle: [] (members moved to other categories, original deleted)

    Operation params:
        rename:   {"name": str, "description": str}
        split:    {"num_clusters": int}  (default 2)
        unbundle: {"target_category_ids": [str]}  (move members to these categories)
    """
    categories = load_categories(categories_dir)
    cat_by_id = {c["id"]: c for c in categories}

    if category_id not in cat_by_id:
        raise ValueError(f"Category {category_id!r} not found")

    category = cat_by_id[category_id]

    if operation == "rename":
        new_name = params.get("name", category["name"])
        new_desc = params.get("description", category["description"])
        # Re-embed the new description to update the centroid signal
        new_emb = embed_text(new_desc)
        updated = _update_ts({
            **category,
            "name": new_name,
            "description": new_desc,
            "centroid_embedding": new_emb,
        })
        save_category(updated, categories_dir)
        return [updated]

    elif operation == "split":
        num_clusters = int(params.get("num_clusters", 2))
        members = category.get("members", [])
        if len(members) < num_clusters:
            raise ValueError(
                f"Cannot split {len(members)} members into {num_clusters} clusters"
            )
        # Embed all member ids (members are strings — we use them as text proxies)
        member_embs = embed_texts(members)
        # Simple greedy k-means-like split by cosine distance from random seeds
        clusters = _kmeans_split(members, member_embs, num_clusters)
        new_cats: list[dict] = []
        for cluster_idx, (cluster_members, cluster_embs) in enumerate(clusters):
            if not cluster_members:
                continue
            new_cat = make_category(
                name=f"{category['name']} (part {cluster_idx + 1})",
                description=category["description"],
                members=cluster_members,
                centroid_embedding=centroid(cluster_embs),
            )
            save_category(new_cat, categories_dir)
            new_cats.append(new_cat)
        delete_category(category_id, categories_dir)
        return new_cats

    elif operation == "unbundle":
        target_ids: list[str] = params.get("target_category_ids", [])
        members = category.get("members", [])
        member_embs = embed_texts(members)

        if target_ids:
            # Assign each member to the nearest target category
            targets = [cat_by_id[tid] for tid in target_ids if tid in cat_by_id]
            for member, emb in zip(members, member_embs):
                best_target = _nearest_category(emb, targets)
                if best_target:
                    updated = _update_ts({
                        **best_target,
                        "members": list(
                            dict.fromkeys(best_target.get("members", []) + [member])
                        ),
                    })
                    cat_by_id[best_target["id"]] = updated
                    save_category(updated, categories_dir)

        delete_category(category_id, categories_dir)
        return []

    else:
        raise ValueError(f"Unknown operation {operation!r}. Must be split, rename, or unbundle.")


# ---------------------------------------------------------------------------
# Internal helpers for refactor
# ---------------------------------------------------------------------------

def _kmeans_split(
    members: list[str],
    embeddings: list[list[float]],
    k: int,
) -> list[tuple[list[str], list[list[float]]]]:
    """
    Partition *members* into *k* groups using simple iterative k-means.

    Seeds are chosen by spreading across the embedding space (pick the first,
    then iteratively pick the member furthest from all existing seeds).

    Returns list of (member_list, embedding_list) tuples.
    """
    n = len(members)
    # Seed selection: start with index 0, then farthest from chosen seeds
    seed_indices = [0]
    for _ in range(k - 1):
        max_min_dist = -1.0
        best_idx = 0
        for idx in range(n):
            if idx in seed_indices:
                continue
            min_sim = min(
                cosine_similarity(embeddings[idx], embeddings[s])
                for s in seed_indices
            )
            if min_sim > max_min_dist:
                max_min_dist = min_sim
                best_idx = idx
        seed_indices.append(best_idx)

    # Iterative assignment
    assignments = [0] * n
    for _ in range(10):  # max 10 iterations
        # Compute cluster centroids
        cluster_embs: list[list[list[float]]] = [[] for _ in range(k)]
        for idx, cluster in enumerate(assignments):
            cluster_embs[cluster].append(embeddings[idx])
        centroids = [centroid(ce) if ce else embeddings[seed_indices[ci]]
                     for ci, ce in enumerate(cluster_embs)]
        # Reassign each point to nearest centroid
        new_assignments = [
            max(range(k), key=lambda ci: cosine_similarity(embeddings[idx], centroids[ci]))
            for idx in range(n)
        ]
        if new_assignments == assignments:
            break
        assignments = new_assignments

    # Build result clusters
    clusters: list[tuple[list[str], list[list[float]]]] = [
        ([], []) for _ in range(k)
    ]
    for idx, cluster in enumerate(assignments):
        clusters[cluster][0].append(members[idx])
        clusters[cluster][1].append(embeddings[idx])

    return clusters


def _nearest_category(emb: list[float], categories: list[dict]) -> dict | None:
    """Return the category with the highest centroid cosine similarity to *emb*."""
    best_sim = -1.0
    best_cat: dict | None = None
    for cat in categories:
        cat_emb = cat.get("centroid_embedding", [])
        if not cat_emb or len(cat_emb) != len(emb):
            continue
        sim = cosine_similarity(emb, cat_emb)
        if sim > best_sim:
            best_sim = sim
            best_cat = cat
    return best_cat


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Lobster categorization operations"
    )
    sub = parser.add_subparsers(dest="command")

    p_compress = sub.add_parser("compress", help="Merge near-duplicate categories")
    p_compress.add_argument("--threshold", type=float, default=0.92)
    p_compress.add_argument("--dry-run", action="store_true")

    p_group = sub.add_parser("group", help="Group a JSON-lines file of items")
    p_group.add_argument("items_file", help="Path to JSONL file of items to group")

    p_refactor = sub.add_parser("refactor", help="Structural refactoring")
    p_refactor.add_argument("category_id")
    p_refactor.add_argument("operation", choices=["split", "rename", "unbundle"])
    p_refactor.add_argument("--params", type=json.loads, default={})

    p_test = sub.add_parser("test", help="Run a self-test")

    args = parser.parse_args(argv)

    if args.command == "compress":
        pairs = compress(threshold=args.threshold, dry_run=args.dry_run)
        if args.dry_run:
            print(f"Would merge {len(pairs)} pair(s):")
        else:
            print(f"Merged {len(pairs)} pair(s):")
        for kept, gone in pairs:
            print(f"  kept={kept} absorbed={gone}")

    elif args.command == "group":
        items_path = Path(args.items_file)
        items = [json.loads(line) for line in items_path.read_text().splitlines() if line.strip()]
        existing = load_categories()
        result = group(items, existing)
        print(f"Result: {len(result)} categories total")

    elif args.command == "refactor":
        result = refactor(args.category_id, args.operation, args.params)
        print(f"Result: {len(result)} categories")
        for cat in result:
            print(f"  {cat['id']}: {cat['name']}")

    elif args.command == "test":
        _run_test()

    else:
        parser.print_help()


def _run_test() -> None:
    """Quick self-test: create dummy categories, compress them."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        print("--- categorization self-test ---")

        # Create two semantically similar categories
        emb_a = embed_text("machine learning and neural networks")
        emb_b = embed_text("deep learning and AI models")
        emb_c = embed_text("cooking recipes and meal planning")

        cat_a = make_category(
            "ML Models", "Machine learning and neural network architectures",
            members=["item1", "item2"], centroid_embedding=emb_a
        )
        cat_b = make_category(
            "Deep Learning", "Deep learning models and AI research",
            members=["item3"], centroid_embedding=emb_b
        )
        cat_c = make_category(
            "Cooking", "Food recipes and meal prep",
            members=["item4", "item5", "item6"], centroid_embedding=emb_c
        )

        for cat in [cat_a, cat_b, cat_c]:
            save_category(cat, d)

        sim_ab = cosine_similarity(emb_a, emb_b)
        sim_ac = cosine_similarity(emb_a, emb_c)
        print(f"Similarity ML<->DL: {sim_ab:.3f}")
        print(f"Similarity ML<->Cooking: {sim_ac:.3f}")

        pairs = compress(threshold=0.70, categories_dir=d)
        print(f"Compress (threshold=0.70): merged {len(pairs)} pair(s)")
        for kept, gone in pairs:
            print(f"  kept={kept[:8]} absorbed={gone[:8]}")

        remaining = load_categories(d)
        print(f"Remaining categories: {[c['name'] for c in remaining]}")

        # Test group
        items = [
            {"id": "new1", "text": "Transformer attention mechanisms in NLP"},
            {"id": "new2", "text": "Sourdough bread baking technique"},
        ]
        all_cats = group(items, remaining, assign_threshold=0.50, categories_dir=d)
        print(f"After grouping: {len(all_cats)} categories")
        for c in all_cats:
            print(f"  {c['name']}: {len(c['members'])} members")

        print("--- self-test passed ---")


if __name__ == "__main__":
    main()
