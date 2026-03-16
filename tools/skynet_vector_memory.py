"""Skynet Vector Memory Store — lightweight vector similarity search.

Provides a numpy-only vector memory with JSONL persistence, cosine
similarity search, hash-based auto-embedding (no external model needed),
content deduplication, and category filtering.

Categories:
    code_patterns, bug_fixes, learnings, architecture_decisions

Storage:
    data/vector_memory.jsonl  (one JSON object per line, append-friendly)

Usage:
    python tools/skynet_vector_memory.py add "text" --category learnings
    python tools/skynet_vector_memory.py search "query" --top-k 5
    python tools/skynet_vector_memory.py stats
    python tools/skynet_vector_memory.py export [--category X] [--output FILE]

Python API:
    from tools.skynet_vector_memory import store_learning, recall_relevant
    store_learning("beta", "Clipboard paste needs 3-retry verify", "bug_fixes")
    results = recall_relevant("clipboard issues", k=5)
"""
# signed: beta

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ── Paths ────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
JSONL_PATH = DATA_DIR / "vector_memory.jsonl"

# ── Constants ────────────────────────────────────────────────────
EMBED_DIM = 256  # hash-based embedding dimension (lightweight)
VALID_CATEGORIES = frozenset([
    "code_patterns",
    "bug_fixes",
    "learnings",
    "architecture_decisions",
])
# signed: beta


# ── Hash-based auto-embedding ────────────────────────────────────

def _tokenize(text: str) -> List[str]:
    """Simple whitespace + punctuation tokenizer, lowercased."""
    return re.findall(r"[a-z0-9_]+", text.lower())


def _hash_token(token: str, dim: int = EMBED_DIM) -> int:
    """Deterministic bucket index for a token via MD5."""
    h = hashlib.md5(token.encode("utf-8")).hexdigest()
    return int(h, 16) % dim


def auto_embed(text: str, dim: int = EMBED_DIM) -> np.ndarray:
    """Generate a deterministic embedding from text using feature hashing.

    Each token is hashed to a bucket; a secondary hash decides the sign
    (+1 / −1).  The result is L2-normalised so cosine similarity equals
    dot product.  No external model or vocabulary required.

    Args:
        text: Input text to embed.
        dim:  Embedding dimensionality (default ``EMBED_DIM``).

    Returns:
        numpy float32 array of shape ``(dim,)``, unit-normalised.
    """
    vec = np.zeros(dim, dtype=np.float32)
    tokens = _tokenize(text)
    if not tokens:
        return vec

    for token in tokens:
        bucket = _hash_token(token, dim)
        # Secondary hash for sign (reduces collisions via ± cancellation)
        sign_hash = hashlib.sha1(token.encode("utf-8")).hexdigest()
        sign = 1.0 if int(sign_hash[0], 16) < 8 else -1.0
        vec[bucket] += sign

    # Bigram features for richer representation
    for i in range(len(tokens) - 1):
        bigram = tokens[i] + "_" + tokens[i + 1]
        bucket = _hash_token(bigram, dim)
        sign_hash = hashlib.sha1(bigram.encode("utf-8")).hexdigest()
        sign = 1.0 if int(sign_hash[0], 16) < 8 else -1.0
        vec[bucket] += sign * 0.5  # lower weight for bigrams

    # L2 normalise
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec
# signed: beta


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors (assumes unit-normalised)."""
    return float(np.dot(a, b))


def _content_hash(text: str) -> str:
    """SHA-256 content hash for deduplication."""
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


# ── Memory record ───────────────────────────────────────────────

class MemoryRecord:
    """A single vector memory entry."""

    __slots__ = (
        "record_id", "text", "category", "embedding",
        "metadata", "content_hash", "created_at", "worker",
    )

    def __init__(
        self,
        text: str,
        category: str,
        embedding: np.ndarray,
        metadata: Optional[Dict[str, Any]] = None,
        worker: str = "unknown",
        record_id: Optional[str] = None,
        content_hash: Optional[str] = None,
        created_at: Optional[str] = None,
    ):
        self.record_id = record_id or uuid.uuid4().hex[:12]
        self.text = text
        self.category = category
        self.embedding = embedding
        self.metadata = metadata or {}
        self.worker = worker
        self.content_hash = content_hash or _content_hash(text)
        self.created_at = created_at or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "text": self.text,
            "category": self.category,
            "embedding": self.embedding.tolist(),
            "metadata": self.metadata,
            "worker": self.worker,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryRecord":
        emb = np.array(d["embedding"], dtype=np.float32)
        return cls(
            text=d["text"],
            category=d.get("category", "learnings"),
            embedding=emb,
            metadata=d.get("metadata", {}),
            worker=d.get("worker", "unknown"),
            record_id=d.get("record_id"),
            content_hash=d.get("content_hash"),
            created_at=d.get("created_at"),
        )
# signed: beta


# ── Vector Memory Store ─────────────────────────────────────────

class VectorMemoryStore:
    """In-memory vector index with JSONL persistence.

    Thread-safe.  Records are loaded from ``data/vector_memory.jsonl``
    on construction and appended on ``add()``.  The full index is rebuilt
    in RAM for fast cosine-similarity search.

    Public API:
        add(text, embedding, metadata, category, worker) → record_id
        search(query_embedding, top_k, filters) → List[dict]
        remove(record_id) → bool
        stats() → dict
        export(category, limit) → List[dict]
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or JSONL_PATH
        self._lock = threading.Lock()
        self._records: Dict[str, MemoryRecord] = {}
        self._hashes: set = set()  # content hashes for dedup
        self._matrix: Optional[np.ndarray] = None  # (N, dim) search matrix
        self._ids: List[str] = []  # row→record_id mapping
        self._load()
    # signed: beta

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        """Load records from JSONL file."""
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        rec = MemoryRecord.from_dict(d)
                        self._records[rec.record_id] = rec
                        self._hashes.add(rec.content_hash)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass  # skip corrupt lines silently
        except OSError:
            pass
        self._rebuild_index()

    def _append_jsonl(self, record: MemoryRecord) -> None:
        """Append a single record to the JSONL file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def _rewrite_jsonl(self) -> None:
        """Rewrite the full JSONL file (after remove operations)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self._path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in self._records.values():
                f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        os.replace(tmp, str(self._path))

    # ── Index management ─────────────────────────────────────────

    def _rebuild_index(self) -> None:
        """Rebuild the numpy search matrix from in-memory records."""
        if not self._records:
            self._matrix = None
            self._ids = []
            return
        self._ids = list(self._records.keys())
        vecs = [self._records[rid].embedding for rid in self._ids]
        self._matrix = np.vstack(vecs)  # (N, dim)
    # signed: beta

    # ── Core API ─────────────────────────────────────────────────

    def add(
        self,
        text: str,
        embedding: Optional[np.ndarray] = None,
        metadata: Optional[Dict[str, Any]] = None,
        category: str = "learnings",
        worker: str = "unknown",
    ) -> Optional[str]:
        """Add a memory record.  Returns record_id or None if duplicate.

        Args:
            text:      The text content to store.
            embedding: Pre-computed embedding (auto-generated if None).
            metadata:  Arbitrary key-value metadata dict.
            category:  One of VALID_CATEGORIES.
            worker:    Worker name that produced this memory.

        Returns:
            record_id string, or None if content was a duplicate.
        """
        if category not in VALID_CATEGORIES:
            category = "learnings"  # safe fallback

        chash = _content_hash(text)
        with self._lock:
            if chash in self._hashes:
                return None  # duplicate

            if embedding is None:
                embedding = auto_embed(text)
            elif len(embedding.shape) == 0 or embedding.shape[0] != EMBED_DIM:
                # Dimension mismatch — re-embed
                embedding = auto_embed(text)

            rec = MemoryRecord(
                text=text,
                category=category,
                embedding=embedding,
                metadata=metadata,
                worker=worker,
                content_hash=chash,
            )
            self._records[rec.record_id] = rec
            self._hashes.add(chash)
            self._append_jsonl(rec)
            self._rebuild_index()
            return rec.record_id

    def search(
        self,
        query_embedding: Optional[np.ndarray] = None,
        query_text: Optional[str] = None,
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Find nearest neighbours by cosine similarity.

        Args:
            query_embedding: Query vector (auto-generated from query_text if None).
            query_text:      Text to embed as query (used if query_embedding is None).
            top_k:           Max results to return.
            filters:         Optional dict with keys ``category``, ``worker``,
                             ``min_score``.

        Returns:
            List of dicts with keys: record_id, text, category, score,
            worker, metadata, created_at.  Sorted by descending score.
        """
        if query_embedding is None:
            if query_text is None:
                return []
            query_embedding = auto_embed(query_text)

        with self._lock:
            if self._matrix is None or len(self._ids) == 0:
                return []

            # Filter candidates first
            candidate_mask = np.ones(len(self._ids), dtype=bool)
            if filters:
                cat_filter = filters.get("category")
                worker_filter = filters.get("worker")
                for i, rid in enumerate(self._ids):
                    rec = self._records[rid]
                    if cat_filter and rec.category != cat_filter:
                        candidate_mask[i] = False
                    if worker_filter and rec.worker != worker_filter:
                        candidate_mask[i] = False

            candidate_indices = np.where(candidate_mask)[0]
            if len(candidate_indices) == 0:
                return []

            # Cosine similarity (dot product on normalised vectors)
            q_norm = np.linalg.norm(query_embedding)
            if q_norm > 0:
                query_embedding = query_embedding / q_norm

            subset = self._matrix[candidate_indices]
            scores = subset @ query_embedding  # (M,)

            # Apply min_score filter
            min_score = filters.get("min_score", -1.0) if filters else -1.0
            score_mask = scores >= min_score
            candidate_indices = candidate_indices[score_mask]
            scores = scores[score_mask]

            # Top-k
            if len(scores) == 0:
                return []
            k = min(top_k, len(scores))
            top_indices = np.argpartition(scores, -k)[-k:]
            top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

            results = []
            for idx in top_indices:
                rid = self._ids[candidate_indices[idx]]
                rec = self._records[rid]
                results.append({
                    "record_id": rec.record_id,
                    "text": rec.text,
                    "category": rec.category,
                    "score": round(float(scores[idx]), 4),
                    "worker": rec.worker,
                    "metadata": rec.metadata,
                    "created_at": rec.created_at,
                })
            return results
    # signed: beta

    def remove(self, record_id: str) -> bool:
        """Remove a record by id.  Returns True if found and removed."""
        with self._lock:
            rec = self._records.pop(record_id, None)
            if rec is None:
                return False
            self._hashes.discard(rec.content_hash)
            self._rebuild_index()
            self._rewrite_jsonl()
            return True

    def get(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single record by id."""
        rec = self._records.get(record_id)
        if rec is None:
            return None
        d = rec.to_dict()
        del d["embedding"]  # omit large vector from API responses
        return d

    def count(self) -> int:
        """Total number of stored records."""
        return len(self._records)

    def stats(self) -> Dict[str, Any]:
        """Return summary statistics."""
        by_category: Dict[str, int] = {}
        by_worker: Dict[str, int] = {}
        for rec in self._records.values():
            by_category[rec.category] = by_category.get(rec.category, 0) + 1
            by_worker[rec.worker] = by_worker.get(rec.worker, 0) + 1

        return {
            "total_records": len(self._records),
            "embedding_dim": EMBED_DIM,
            "categories": by_category,
            "workers": by_worker,
            "storage_path": str(self._path),
            "file_exists": self._path.exists(),
            "file_size_kb": (
                round(self._path.stat().st_size / 1024, 1)
                if self._path.exists() else 0
            ),
        }

    def export(
        self,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Export records as dicts (without embeddings).

        Args:
            category: Filter to a single category (None = all).
            limit:    Max records to return.

        Returns:
            List of record dicts sorted by created_at descending.
        """
        records = list(self._records.values())
        if category:
            records = [r for r in records if r.category == category]
        records.sort(key=lambda r: r.created_at, reverse=True)
        records = records[:limit]
        out = []
        for rec in records:
            d = rec.to_dict()
            del d["embedding"]
            out.append(d)
        return out
    # signed: beta

    def clear(self) -> int:
        """Remove all records.  Returns count of removed records."""
        with self._lock:
            n = len(self._records)
            self._records.clear()
            self._hashes.clear()
            self._matrix = None
            self._ids = []
            if self._path.exists():
                self._path.unlink()
            return n


# ── Integration helpers ─────────────────────────────────────────

_store: Optional[VectorMemoryStore] = None


def _get_store() -> VectorMemoryStore:
    """Module-level singleton."""
    global _store
    if _store is None:
        _store = VectorMemoryStore()
    return _store


def store_learning(
    worker: str,
    text: str,
    category: str = "learnings",
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Store a learning into vector memory.

    Convenience wrapper used by workers after task completion.

    Args:
        worker:   Worker name (e.g. "beta").
        text:     The learning text.
        category: One of VALID_CATEGORIES.
        metadata: Optional extra metadata.

    Returns:
        record_id or None if duplicate.
    """
    store = _get_store()
    return store.add(
        text=text,
        category=category,
        worker=worker,
        metadata=metadata,
    )
# signed: beta


def recall_relevant(
    query: str,
    k: int = 5,
    category: Optional[str] = None,
    worker: Optional[str] = None,
    min_score: float = 0.0,
) -> List[Dict[str, Any]]:
    """Recall the most relevant memories for a query.

    Args:
        query:     Natural-language query text.
        k:         Max results.
        category:  Optional category filter.
        worker:    Optional worker filter.
        min_score: Minimum cosine similarity threshold.

    Returns:
        List of result dicts with text, score, category, etc.
    """
    store = _get_store()
    filters: Dict[str, Any] = {}
    if category:
        filters["category"] = category
    if worker:
        filters["worker"] = worker
    if min_score > 0:
        filters["min_score"] = min_score
    return store.search(query_text=query, top_k=k, filters=filters or None)


def memory_stats() -> Dict[str, Any]:
    """Return vector memory statistics."""
    return _get_store().stats()
# signed: beta


# ── CLI ──────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Skynet Vector Memory Store — similarity search over learnings"
    )
    sub = parser.add_subparsers(dest="command")

    # add
    add_p = sub.add_parser("add", help="Add a memory record")
    add_p.add_argument("text", help="Text to store")
    add_p.add_argument(
        "--category", "-c", default="learnings",
        choices=sorted(VALID_CATEGORIES),
        help="Category (default: learnings)",
    )
    add_p.add_argument("--worker", "-w", default="cli", help="Worker name")
    add_p.add_argument(
        "--meta", "-m", default=None,
        help='JSON metadata string, e.g. \'{"file":"foo.py"}\'',
    )

    # search
    srch_p = sub.add_parser("search", help="Search memory by similarity")
    srch_p.add_argument("query", help="Search query text")
    srch_p.add_argument("--top-k", "-k", type=int, default=5)
    srch_p.add_argument("--category", "-c", default=None)
    srch_p.add_argument("--worker", "-w", default=None)
    srch_p.add_argument("--min-score", type=float, default=0.0)

    # stats
    sub.add_parser("stats", help="Show memory statistics")

    # export
    exp_p = sub.add_parser("export", help="Export records as JSON")
    exp_p.add_argument("--category", "-c", default=None)
    exp_p.add_argument("--limit", "-l", type=int, default=100)
    exp_p.add_argument("--output", "-o", default=None, help="Output file path")

    args = parser.parse_args()

    if args.command == "add":
        meta = None
        if args.meta:
            try:
                meta = json.loads(args.meta)
            except json.JSONDecodeError:
                print("ERROR: --meta must be valid JSON", file=sys.stderr)
                sys.exit(1)
        store = VectorMemoryStore()
        rid = store.add(
            text=args.text,
            category=args.category,
            worker=args.worker,
            metadata=meta,
        )
        if rid:
            print(f"Added: {rid} (category={args.category})")
        else:
            print("Duplicate — not added.")

    elif args.command == "search":
        store = VectorMemoryStore()
        filters: Dict[str, Any] = {}
        if args.category:
            filters["category"] = args.category
        if args.worker:
            filters["worker"] = args.worker
        if args.min_score > 0:
            filters["min_score"] = args.min_score
        results = store.search(
            query_text=args.query,
            top_k=args.top_k,
            filters=filters or None,
        )
        if not results:
            print("No results found.")
        else:
            for i, r in enumerate(results, 1):
                print(
                    f"{i}. [{r['score']:.3f}] ({r['category']}) "
                    f"[{r['worker']}] {r['text'][:120]}"
                )

    elif args.command == "stats":
        store = VectorMemoryStore()
        st = store.stats()
        print(f"Total records:  {st['total_records']}")
        print(f"Embedding dim:  {st['embedding_dim']}")
        print(f"Storage:        {st['storage_path']}")
        print(f"File size:      {st['file_size_kb']} KB")
        print("Categories:")
        for cat, cnt in sorted(st.get("categories", {}).items()):
            print(f"  {cat}: {cnt}")
        print("Workers:")
        for w, cnt in sorted(st.get("workers", {}).items()):
            print(f"  {w}: {cnt}")

    elif args.command == "export":
        store = VectorMemoryStore()
        records = store.export(category=args.category, limit=args.limit)
        output = json.dumps(records, indent=2, ensure_ascii=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"Exported {len(records)} records to {args.output}")
        else:
            print(output)

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
