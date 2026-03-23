#!/usr/bin/env python3
"""
scripts/meta_threads.py — Meta-thread system for Lobster

Meta-threads are persistent semantic threads that track recurring topics,
open questions, and key observations across conversations.  They are not
tied to any single conversation — they persist and evolve as new messages
arrive.

Storage: JSON files in ~/lobster-user-config/memory/meta-threads/
Embeddings: 384-dim via fastembed (same model as events_vec in memory.db)

Key design:
  - Dispatcher matches incoming messages against inquiry_embedding (the open
    question the thread is tracking), NOT the category centroid.
  - After match, surface observations from the category centroid (what to inject).
  - Meta-thread evolution atomically updates both category structure AND
    inquiry embedding.

Public API:
  search(message_text, threshold=0.7) -> list[MetaThread]
  inject_context(threads) -> str
  update(thread_id, new_observation=None, new_open_question=None)
  bootstrap_from_history(since_days=90)

Dispatcher integration:
  Call search(message_text) before processing each message.
  If any results exceed the threshold, call inject_context(results) and
  prepend the returned string to the system context for that message.

  Example (pseudocode):
      relevant = meta_threads.search(message.text, threshold=0.7)
      if relevant:
          context_prefix = meta_threads.inject_context(relevant)
          system_context = context_prefix + "\\n\\n" + system_context

  This call completes in <1s for typical thread counts because embeddings
  are cached in the process and the similarity check is pure Python math.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_THREADS_DIR = Path.home() / "lobster-user-config" / "memory" / "meta-threads"
_DEFAULT_MEMORY_DB = Path.home() / "lobster-workspace" / "data" / "memory.db"
_DEFAULT_CATEGORIES_DIR = Path.home() / "lobster-user-config" / "memory" / "categories"


# ---------------------------------------------------------------------------
# Embedding utilities (re-used from categorization module when available,
# duplicated here so meta_threads.py is standalone-importable)
# ---------------------------------------------------------------------------

_embed_model = None
_embed_cache: dict[str, list[float]] = {}


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from fastembed import TextEmbedding
        _embed_model = TextEmbedding()
    return _embed_model


def _embed(text: str) -> list[float]:
    """Return a 384-dim embedding, cached by text."""
    if text in _embed_cache:
        return _embed_cache[text]
    model = _get_embed_model()
    embedding = list(map(float, list(model.embed([text]))[0]))
    _embed_cache[text] = embedding
    return embedding


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


def _centroid(embeddings: list[list[float]]) -> list[float]:
    if not embeddings:
        return []
    dim = len(embeddings[0])
    total = [0.0] * dim
    for emb in embeddings:
        for i, v in enumerate(emb):
            total[i] += v
    n = len(embeddings)
    return [v / n for v in total]


# ---------------------------------------------------------------------------
# MetaThread data model
# ---------------------------------------------------------------------------

MetaThread = dict[str, Any]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_meta_thread(
    name: str,
    current_open_question: str,
    key_observations: list[str] | None = None,
    related_thread_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    thread_id: str | None = None,
    active: bool = True,
) -> MetaThread:
    """
    Create a new MetaThread dict.

    The inquiry_embedding is computed from current_open_question — this is
    what the dispatcher searches against when deciding whether to inject context.
    """
    inquiry_emb = _embed(current_open_question)
    now = _iso_now()
    return {
        "id": thread_id or str(uuid.uuid4()),
        "name": name,
        "current_open_question": current_open_question,
        "key_observations": key_observations or [],
        "related_thread_ids": related_thread_ids or [],
        "category_ids": category_ids or [],
        "inquiry_embedding": inquiry_emb,
        "last_updated": now,
        "active": active,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_threads(threads_dir: Path | None = None) -> list[MetaThread]:
    """
    Load all MetaThread JSON files from *threads_dir*.

    Returns an empty list if the directory does not exist.  Only threads
    with active=True are returned by default.
    """
    d = threads_dir or _DEFAULT_THREADS_DIR
    if not d.exists():
        return []
    threads = []
    for p in sorted(d.glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            threads.append(t)
        except (json.JSONDecodeError, OSError):
            pass
    return threads


def save_thread(thread: MetaThread, threads_dir: Path | None = None) -> None:
    """Write *thread* atomically to {threads_dir}/{id}.json."""
    d = threads_dir or _DEFAULT_THREADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{thread['id']}.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(thread, indent=2), encoding="utf-8")
    tmp.replace(target)


def get_thread(thread_id: str, threads_dir: Path | None = None) -> MetaThread | None:
    """Load a single thread by id, or None if not found."""
    d = threads_dir or _DEFAULT_THREADS_DIR
    path = d / f"{thread_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# search — find threads relevant to an incoming message
# ---------------------------------------------------------------------------

def search(
    message_text: str,
    threshold: float = 0.7,
    threads_dir: Path | None = None,
    active_only: bool = True,
) -> list[MetaThread]:
    """
    Return meta-threads whose inquiry_embedding is similar to *message_text*.

    Matching is done against inquiry_embedding (the open question the thread
    is tracking), not the category centroid.  This captures messages that are
    relevant to what the thread is TRYING TO UNDERSTAND, not just what it has
    already accumulated.

    Args:
        message_text: The incoming message text to match against.
        threshold:    Minimum cosine similarity to include a thread (0–1).
        threads_dir:  Override for storage directory.
        active_only:  If True, skip threads with active=False.

    Returns:
        List of matching MetaThread dicts, sorted by descending similarity.
        Empty list if no threads match or the directory does not exist.
    """
    threads = load_threads(threads_dir)
    if not threads:
        return []

    if active_only:
        threads = [t for t in threads if t.get("active", True)]

    if not threads:
        return []

    msg_emb = _embed(message_text)
    scored: list[tuple[float, MetaThread]] = []

    for thread in threads:
        inq_emb = thread.get("inquiry_embedding", [])
        if not inq_emb or len(inq_emb) != len(msg_emb):
            continue
        sim = _cosine(msg_emb, inq_emb)
        if sim >= threshold:
            scored.append((sim, thread))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored]


# ---------------------------------------------------------------------------
# inject_context — format thread state for dispatcher context injection
# ---------------------------------------------------------------------------

def inject_context(threads: list[MetaThread]) -> str:
    """
    Format matched meta-threads into a string suitable for prepending to
    the dispatcher's system context.

    The output is structured but concise — designed to be readable in a
    mobile context window without overwhelming the model.

    Args:
        threads: List of MetaThread dicts (typically from search()).

    Returns:
        Formatted string with thread names, open questions, and key observations.
        Empty string if *threads* is empty.
    """
    if not threads:
        return ""

    sections: list[str] = ["## Relevant ongoing threads\n"]
    for thread in threads:
        name = thread.get("name", "Unnamed thread")
        question = thread.get("current_open_question", "")
        observations = thread.get("key_observations", [])

        section = [f"**{name}**"]
        if question:
            section.append(f"Open question: {question}")
        if observations:
            section.append("Key observations:")
            for obs in observations[:3]:  # cap at 3 to keep context tight
                section.append(f"  - {obs}")
        sections.append("\n".join(section))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# update — atomically update a thread's state
# ---------------------------------------------------------------------------

def update(
    thread_id: str,
    new_observation: str | None = None,
    new_open_question: str | None = None,
    threads_dir: Path | None = None,
) -> MetaThread | None:
    """
    Update a meta-thread's state atomically.

    When new_open_question is provided, recomputes inquiry_embedding so
    future searches reflect the updated question.

    When new_observation is provided, prepends it to key_observations and
    trims the list to the most recent 10 observations.

    Meta-thread evolution is atomic: both the category-level structure
    (key_observations) and the inquiry-level signal (inquiry_embedding) are
    updated in the same save call.

    Args:
        thread_id:         ID of the thread to update.
        new_observation:   String observation to add.
        new_open_question: New open question (recomputes inquiry_embedding).
        threads_dir:       Override for storage directory.

    Returns:
        The updated MetaThread, or None if thread_id was not found.
    """
    thread = get_thread(thread_id, threads_dir)
    if thread is None:
        return None

    updated = dict(thread)

    if new_observation:
        observations = [new_observation] + updated.get("key_observations", [])
        updated["key_observations"] = observations[:10]

    if new_open_question:
        updated["current_open_question"] = new_open_question
        # Recompute inquiry_embedding — this is the atomic coupling the design requires
        updated["inquiry_embedding"] = _embed(new_open_question)

    updated["last_updated"] = _iso_now()
    save_thread(updated, threads_dir)
    return updated


# ---------------------------------------------------------------------------
# bootstrap_from_history — one-time import from conversation history
# ---------------------------------------------------------------------------

def bootstrap_from_history(
    since_days: int = 90,
    memory_db_path: Path | None = None,
    threads_dir: Path | None = None,
    dry_run: bool = False,
    min_cluster_size: int = 3,
) -> list[MetaThread]:
    """
    One-time bootstrap: run a grouping pass over conversation history and
    promote coherent recurring clusters to meta-thread candidates.

    Algorithm:
      1. Load events from memory.db (type != 'proprioceptive' to get content-rich events)
      2. Embed each event's content
      3. Cluster using cosine-distance agglomeration
      4. For each cluster with >= min_cluster_size items, prompt Claude to name
         the theme and generate an open question
      5. Create a MetaThread for each candidate

    Args:
        since_days:       Only consider events from the last N days.
        memory_db_path:   Override for memory.db path.
        threads_dir:      Override for threads storage directory.
        dry_run:          If True, return candidates without saving.
        min_cluster_size: Minimum cluster size to become a meta-thread candidate.

    Returns:
        List of MetaThread dicts created (or would-be-created in dry_run).
    """
    try:
        import sqlite3
        import sqlite_vec
    except ImportError:
        print("sqlite_vec not available — cannot bootstrap from history", file=sys.stderr)
        return []

    db_path = memory_db_path or _DEFAULT_MEMORY_DB
    if not db_path.exists():
        print(f"memory.db not found at {db_path}", file=sys.stderr)
        return []

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    rows = conn.execute(
        "SELECT id, content, type FROM events WHERE timestamp >= ? AND content != '' ORDER BY timestamp",
        (cutoff,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No events found since {cutoff}", file=sys.stderr)
        return []

    print(f"Loaded {len(rows)} events for bootstrapping")

    # Embed all event contents
    contents = [r[1] for r in rows]
    event_ids = [str(r[0]) for r in rows]

    from fastembed import TextEmbedding
    model = TextEmbedding()
    embeddings = [list(map(float, emb)) for emb in model.embed(contents)]
    print(f"Embedded {len(embeddings)} events")

    # Agglomerative clustering: greedily merge items above a cosine threshold
    clusters = _agglomerate(event_ids, embeddings, threshold=0.72)
    print(f"Found {len(clusters)} clusters")

    # Filter by minimum size
    viable = [
        (members, embs)
        for members, embs in clusters
        if len(members) >= min_cluster_size
    ]
    print(f"{len(viable)} clusters meet min_cluster_size={min_cluster_size}")

    # For each viable cluster, generate a meta-thread
    threads: list[MetaThread] = []
    for members, embs in viable:
        # Get representative content for the cluster (first 3 items)
        sample_contents = [contents[event_ids.index(m)] for m in members[:3] if m in event_ids]
        cluster_centroid = _centroid(embs)

        name, question = _name_cluster(sample_contents)
        thread = make_meta_thread(
            name=name,
            current_open_question=question,
            key_observations=sample_contents[:5],
            category_ids=[],
        )
        # Override inquiry_embedding with the question embedding (already done
        # by make_meta_thread), but also note the cluster centroid separately
        # for future category linking
        thread["_bootstrap_centroid"] = cluster_centroid
        thread["_bootstrap_member_ids"] = members

        if not dry_run:
            # Remove internal keys before saving
            saved = {k: v for k, v in thread.items() if not k.startswith("_")}
            save_thread(saved, threads_dir)
        threads.append(thread)

    return threads


def _agglomerate(
    ids: list[str],
    embeddings: list[list[float]],
    threshold: float = 0.72,
) -> list[tuple[list[str], list[list[float]]]]:
    """
    Simple greedy agglomerative clustering.

    Each item starts as its own cluster.  Pairs of clusters whose centroid
    cosine similarity exceeds *threshold* are merged (smallest cluster absorbed
    into largest).  Runs until no more merges are possible.

    This is O(n^2) but suitable for the expected data sizes (hundreds of events).
    """
    # Each cluster: (member_ids, member_embeddings)
    clusters: list[list] = [[i, e] for i, e in zip(ids, embeddings)]
    # clusters[i] = [[id, ...], [emb, ...]]
    cluster_list: list[tuple[list[str], list[list[float]]]] = [
        ([i], [e]) for i, e in zip(ids, embeddings)
    ]

    changed = True
    while changed:
        changed = False
        n = len(cluster_list)
        merged_indices: set[int] = set()
        new_clusters: list[tuple[list[str], list[list[float]]]] = []

        for i in range(n):
            if i in merged_indices:
                continue
            best_j = -1
            best_sim = -1.0
            c_i_emb = _centroid(cluster_list[i][1])
            for j in range(i + 1, n):
                if j in merged_indices:
                    continue
                c_j_emb = _centroid(cluster_list[j][1])
                sim = _cosine(c_i_emb, c_j_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_j = j

            if best_j >= 0 and best_sim >= threshold:
                # Merge j into i
                merged_members = cluster_list[i][0] + cluster_list[best_j][0]
                merged_embs = cluster_list[i][1] + cluster_list[best_j][1]
                new_clusters.append((merged_members, merged_embs))
                merged_indices.add(i)
                merged_indices.add(best_j)
                changed = True
            else:
                new_clusters.append(cluster_list[i])
                merged_indices.add(i)

        cluster_list = new_clusters

    return cluster_list


def _name_cluster(sample_contents: list[str]) -> tuple[str, str]:
    """
    Use Claude to generate a name and open question for a cluster.

    Falls back to a content-derived name if the API is unavailable.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()
        joined = "\n".join(f"- {c[:120]}" for c in sample_contents)
        prompt = (
            "These observations come from a recurring conversational theme:\n\n"
            f"{joined}\n\n"
            "1. Give this theme a short, precise name (5 words max).\n"
            "2. Write the single most important open question this theme raises "
            "   (one sentence, ending with a question mark).\n\n"
            "Reply with exactly two lines:\n"
            "Name: <name>\n"
            "Question: <question>"
        )
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        name_line = next((l for l in text.splitlines() if l.startswith("Name:")), "")
        question_line = next((l for l in text.splitlines() if l.startswith("Question:")), "")
        name = name_line.replace("Name:", "").strip() or sample_contents[0][:40]
        question = question_line.replace("Question:", "").strip() or "What patterns emerge here?"
        return name, question
    except Exception:
        # Fallback: use first content as name proxy
        name = sample_contents[0][:40].strip() if sample_contents else "Unnamed cluster"
        return name, "What patterns emerge in this area?"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Lobster meta-thread management"
    )
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search threads matching a message")
    p_search.add_argument("message", help="Message text to match against")
    p_search.add_argument("--threshold", type=float, default=0.7)

    p_inject = sub.add_parser("inject", help="Show injection context for a message")
    p_inject.add_argument("message")
    p_inject.add_argument("--threshold", type=float, default=0.7)

    p_update = sub.add_parser("update", help="Update a thread")
    p_update.add_argument("thread_id")
    p_update.add_argument("--observation", default=None)
    p_update.add_argument("--question", default=None)

    p_bootstrap = sub.add_parser("bootstrap", help="Bootstrap threads from history")
    p_bootstrap.add_argument("--since-days", type=int, default=90)
    p_bootstrap.add_argument("--dry-run", action="store_true")
    p_bootstrap.add_argument("--min-cluster-size", type=int, default=3)

    p_list = sub.add_parser("list", help="List all meta-threads")

    p_test = sub.add_parser("test", help="Run a self-test")
    # Keep --test for backward-compat with the spec
    parser.add_argument("--test", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    # Handle legacy --test flag
    if hasattr(args, "test") and args.test:
        _run_test()
        return

    if args.command == "search":
        results = search(args.message, threshold=args.threshold)
        print(f"Found {len(results)} matching thread(s):")
        for t in results:
            print(f"  [{t['id'][:8]}] {t['name']} — {t['current_open_question']}")

    elif args.command == "inject":
        threads = search(args.message, threshold=args.threshold)
        ctx = inject_context(threads)
        if ctx:
            print(ctx)
        else:
            print("(no matching threads)")

    elif args.command == "update":
        result = update(
            args.thread_id,
            new_observation=args.observation,
            new_open_question=args.question,
        )
        if result:
            print(f"Updated thread: {result['name']}")
        else:
            print(f"Thread {args.thread_id} not found")

    elif args.command == "bootstrap":
        threads = bootstrap_from_history(
            since_days=args.since_days,
            dry_run=args.dry_run,
            min_cluster_size=args.min_cluster_size,
        )
        print(f"Bootstrap {'would create' if args.dry_run else 'created'} {len(threads)} thread(s):")
        for t in threads:
            print(f"  {t['name']}: {t['current_open_question']}")

    elif args.command == "list":
        threads = load_threads()
        if not threads:
            print("No meta-threads found.")
            return
        for t in threads:
            status = "active" if t.get("active", True) else "inactive"
            print(f"[{t['id'][:8]}] {t['name']} ({status})")
            print(f"  Q: {t['current_open_question']}")
            print(f"  Observations: {len(t.get('key_observations', []))}")

    elif args.command == "test":
        _run_test()

    else:
        parser.print_help()


def _run_test() -> None:
    """
    Self-test: create a dummy thread, search it, print injection output.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        print("--- meta_threads self-test ---")

        # Create a dummy thread about distributed systems
        thread = make_meta_thread(
            name="Distributed Systems Reliability",
            current_open_question="How should Lobster handle split-brain scenarios during network partitions?",
            key_observations=[
                "WFM stalls have occurred 3 times this month during high message load",
                "The dispatcher currently has no partition detection logic",
                "Health check measures WFM freshness, not actual message throughput",
            ],
        )
        save_thread(thread, d)
        print(f"Created thread: {thread['name']}")
        print(f"Thread ID: {thread['id'][:8]}...")

        # Search with a related message
        test_message = "The dispatcher keeps getting stuck when there are too many messages"
        results = search(test_message, threshold=0.4, threads_dir=d)
        print(f"\nSearch: '{test_message}'")
        print(f"Found {len(results)} matching thread(s)")

        # Search with an unrelated message
        unrelated = "What should I make for dinner tonight?"
        results_unrelated = search(unrelated, threshold=0.6, threads_dir=d)
        print(f"\nSearch: '{unrelated}'")
        print(f"Found {len(results_unrelated)} matching thread(s) (expect 0)")

        # Inject context
        if results:
            ctx = inject_context(results)
            print(f"\nInjection output:\n{ctx}")

        # Update the thread
        updated = update(
            thread["id"],
            new_observation="Dispatcher blocked on main thread 3x this week",
            new_open_question="Is the root cause the WFM call or the message processing loop itself?",
            threads_dir=d,
        )
        print(f"\nUpdated thread question: {updated['current_open_question']}")
        print(f"Observations count: {len(updated['key_observations'])}")

        # Verify the inquiry embedding was updated
        old_emb = thread["inquiry_embedding"]
        new_emb = updated["inquiry_embedding"]
        similarity = _cosine(old_emb, new_emb)
        print(f"Old vs new inquiry similarity: {similarity:.3f} (should be < 1.0)")

        print("\n--- self-test passed ---")


if __name__ == "__main__":
    main()
