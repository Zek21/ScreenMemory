"""Skynet Advanced RAG Pipeline — P3.08

Three-stage Retrieval-Augmented Generation pipeline:

  1. **Sparse retrieval** — BM25 keyword search across indexed codebase files
  2. **Dense retrieval** — Semantic vector search via skynet_vector_memory
  3. **Re-ranking** — Score fusion with relevance, recency, and source-diversity
     bonuses; dedup by content hash; token-budget assembly

The pipeline yields ranked context chunks suitable for enriching prompts sent
to Skynet workers.  Integrates with ``skynet_prompt_assembly.PromptAssembler``
for end-to-end enriched prompt building.

Usage:
    python tools/skynet_rag.py query "How does ghost-type delivery work?"
    python tools/skynet_rag.py query "bus ring buffer" --max-tokens 2000
    python tools/skynet_rag.py index [--dirs tools core Skynet]
    python tools/skynet_rag.py stats
"""
# signed: alpha

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

DATA_DIR = _REPO / "data"
BM25_INDEX_FILE = DATA_DIR / "rag_bm25_index.json"

# ── Tokenisation helpers ────────────────────────────────────────────
# signed: alpha

_SPLIT_RE = re.compile(r"[a-zA-Z_]\w*|[0-9]+")


def _tokenize(text: str) -> List[str]:
    """Simple word-level tokeniser that also splits camelCase/snake_case."""
    raw = _SPLIT_RE.findall(text)  # preserve case for camelCase split
    tokens: List[str] = []
    for w in raw:
        # Split camelCase: fooBar → foo_Bar, then split on _
        parts = re.sub(r"([a-z])([A-Z])", r"\1_\2", w).split("_")
        tokens.extend(p.lower() for p in parts if len(p) >= 2)
    return tokens


def _estimate_tokens(text: str) -> int:
    """Rough token count (≈ words × 1.3 for code)."""
    return max(1, int(len(text.split()) * 1.3))


# ── BM25 Index ──────────────────────────────────────────────────────
# signed: alpha

@dataclass
class BM25Document:
    """A document in the BM25 index."""
    doc_id: str
    path: str
    content: str
    tokens: List[str] = field(default_factory=list)
    mtime: float = 0.0
    line_start: int = 0
    line_end: int = 0


class BM25Index:
    """Okapi BM25 index for sparse retrieval over codebase chunks.

    Each document is a file chunk (≤ ``chunk_lines`` lines).
    Index is persisted to JSON for fast reload.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75,
                 chunk_lines: int = 80):
        self.k1 = k1
        self.b = b
        self.chunk_lines = chunk_lines
        self.docs: List[BM25Document] = []
        self.df: Dict[str, int] = {}      # document frequency
        self.avg_dl: float = 0.0
        self._built = False

    # ── Indexing ─────────────────────────────────────────────────

    def add_file(self, filepath: Path) -> int:
        """Index a file as chunks of ``chunk_lines`` lines.

        Returns number of chunks added.
        """
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0

        lines = text.splitlines()
        mtime = filepath.stat().st_mtime
        rel = str(filepath.relative_to(_REPO)).replace("\\", "/")
        added = 0
        for start in range(0, len(lines), self.chunk_lines):
            end = min(start + self.chunk_lines, len(lines))
            chunk = "\n".join(lines[start:end])
            if not chunk.strip():
                continue
            tokens = _tokenize(chunk)
            if len(tokens) < 3:
                continue
            doc_id = f"{rel}:{start+1}-{end}"
            self.docs.append(BM25Document(
                doc_id=doc_id, path=rel, content=chunk,
                tokens=tokens, mtime=mtime,
                line_start=start + 1, line_end=end,
            ))
            added += 1
        self._built = False
        return added

    def build(self) -> None:
        """Compute DF and average document length."""
        self.df.clear()
        total_len = 0
        for doc in self.docs:
            total_len += len(doc.tokens)
            seen: Set[str] = set()
            for t in doc.tokens:
                if t not in seen:
                    self.df[t] = self.df.get(t, 0) + 1
                    seen.add(t)
        n = len(self.docs)
        self.avg_dl = total_len / n if n else 1.0
        self._built = True

    # ── Search ───────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 20) -> List[Tuple[BM25Document, float]]:
        """Return top-k documents scored by BM25.

        Returns list of (doc, score) sorted descending.
        """
        if not self._built:
            self.build()

        qtokens = _tokenize(query)
        if not qtokens:
            return []

        n = len(self.docs)
        scores: List[float] = [0.0] * n

        for qt in qtokens:
            df_t = self.df.get(qt, 0)
            if df_t == 0:
                continue
            idf = math.log((n - df_t + 0.5) / (df_t + 0.5) + 1.0)
            for i, doc in enumerate(self.docs):
                tf = doc.tokens.count(qt)
                if tf == 0:
                    continue
                dl = len(doc.tokens)
                numer = tf * (self.k1 + 1)
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self.avg_dl)
                scores[i] += idf * numer / denom

        ranked = sorted(enumerate(scores), key=lambda x: -x[1])
        result = []
        for idx, sc in ranked[:top_k]:
            if sc > 0:
                result.append((self.docs[idx], sc))
        return result

    # ── Persistence ──────────────────────────────────────────────

    def save(self, path: Optional[Path] = None) -> None:
        """Persist index to JSON."""
        p = path or BM25_INDEX_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "k1": self.k1, "b": self.b,
            "chunk_lines": self.chunk_lines,
            "avg_dl": self.avg_dl,
            "df": self.df,
            "docs": [
                {"doc_id": d.doc_id, "path": d.path,
                 "tokens": d.tokens, "mtime": d.mtime,
                 "line_start": d.line_start, "line_end": d.line_end}
                for d in self.docs
            ],
            "indexed_at": time.time(),
        }
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        tmp.replace(p)

    def load(self, path: Optional[Path] = None) -> bool:
        """Load index from JSON. Returns True if loaded."""
        p = path or BM25_INDEX_FILE
        if not p.exists():
            return False
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False
        self.k1 = data.get("k1", 1.5)
        self.b = data.get("b", 0.75)
        self.chunk_lines = data.get("chunk_lines", 80)
        self.avg_dl = data.get("avg_dl", 1.0)
        self.df = data.get("df", {})
        self.docs = []
        for dd in data.get("docs", []):
            self.docs.append(BM25Document(
                doc_id=dd["doc_id"], path=dd["path"], content="",
                tokens=dd.get("tokens", []), mtime=dd.get("mtime", 0),
                line_start=dd.get("line_start", 0),
                line_end=dd.get("line_end", 0),
            ))
        self._built = True
        return True

    def stats(self) -> Dict[str, Any]:
        return {
            "total_docs": len(self.docs),
            "vocabulary_size": len(self.df),
            "avg_doc_length": round(self.avg_dl, 1),
            "unique_files": len({d.path for d in self.docs}),
        }


# ── Chunk dataclass ─────────────────────────────────────────────────
# signed: alpha

@dataclass
class RAGChunk:
    """A retrieved chunk with scores and metadata."""
    chunk_id: str
    source: str            # "bm25" | "dense" | "both"
    path: str
    content: str
    line_start: int = 0
    line_end: int = 0
    bm25_score: float = 0.0
    dense_score: float = 0.0
    fused_score: float = 0.0
    recency_bonus: float = 0.0
    mtime: float = 0.0
    token_count: int = 0
    content_hash: str = ""

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.md5(
                self.content.encode("utf-8", errors="replace")
            ).hexdigest()[:12]
        if not self.token_count:
            self.token_count = _estimate_tokens(self.content)


# ── RAG Pipeline ────────────────────────────────────────────────────
# signed: alpha

# Weights for score fusion
W_BM25 = 0.40
W_DENSE = 0.45
W_RECENCY = 0.10
W_DIVERSITY = 0.05

# Recency: files modified in last N hours get a bonus
RECENCY_HOURS = 48.0

# Default directories to index
DEFAULT_INDEX_DIRS = ["tools", "core", "Skynet", "docs"]
# File extensions to index
INDEX_EXTENSIONS = {
    ".py", ".go", ".js", ".ts", ".md", ".ps1", ".html", ".json",
}
# Skip patterns
SKIP_PATTERNS = {
    "__pycache__", ".git", "node_modules", "env", ".venv",
    "screenshots", "data/worker_output",
}


class RAGPipeline:
    """Three-stage RAG pipeline: BM25 → dense → re-rank.

    Usage::

        rag = RAGPipeline()
        rag.ensure_index()
        result = rag.query("How does ghost-type delivery work?")
        print(result["context"])
    """

    def __init__(self, max_tokens: int = 4000, bm25_top_k: int = 30,
                 dense_top_k: int = 20, final_top_k: int = 10):
        self.max_tokens = max_tokens
        self.bm25_top_k = bm25_top_k
        self.dense_top_k = dense_top_k
        self.final_top_k = final_top_k
        self.bm25 = BM25Index()
        self._dense_store: Any = None
        self._indexed = False

    # ── Dense retriever (lazy init) ──────────────────────────────

    def _get_dense(self):
        """Lazy-load VectorMemoryStore."""
        if self._dense_store is None:
            try:
                from tools.skynet_vector_memory import VectorMemoryStore
                self._dense_store = VectorMemoryStore()
            except Exception:
                self._dense_store = False  # mark as unavailable
        return self._dense_store if self._dense_store is not False else None

    # ── Indexing ─────────────────────────────────────────────────

    def _should_skip(self, path: Path) -> bool:
        parts = set(path.parts)
        return bool(parts & SKIP_PATTERNS)

    def index_directory(self, directory: Path) -> int:
        """Index all eligible files under *directory*."""
        count = 0
        if not directory.exists():
            return 0
        for fp in directory.rglob("*"):
            if not fp.is_file():
                continue
            if fp.suffix not in INDEX_EXTENSIONS:
                continue
            if self._should_skip(fp):
                continue
            count += self.bm25.add_file(fp)
        return count

    def build_index(self, dirs: Optional[List[str]] = None) -> Dict[str, Any]:
        """Build BM25 index over specified directories.

        Args:
            dirs: List of directory names relative to repo root.
                  Defaults to DEFAULT_INDEX_DIRS.

        Returns:
            dict with indexing stats.
        """
        dir_names = dirs or DEFAULT_INDEX_DIRS
        self.bm25 = BM25Index()
        total = 0
        dir_counts: Dict[str, int] = {}
        for d in dir_names:
            dp = _REPO / d
            n = self.index_directory(dp)
            dir_counts[d] = n
            total += n
        self.bm25.build()
        self.bm25.save()
        self._indexed = True
        return {
            "total_chunks": total,
            "per_directory": dir_counts,
            "vocabulary_size": len(self.bm25.df),
            "avg_doc_length": round(self.bm25.avg_dl, 1),
        }

    def ensure_index(self, max_age_s: float = 3600.0) -> bool:
        """Load index from disk, rebuild if stale or missing.

        Returns True if index is ready.
        """
        if self._indexed:
            return True
        if self.bm25.load():
            self._indexed = True
            return True
        # No index — build one
        self.build_index()
        return self._indexed

    # ── Stage 1: Sparse (BM25) retrieval ─────────────────────────

    def _sparse_retrieve(self, query: str) -> List[RAGChunk]:
        """BM25 keyword search."""
        results = self.bm25.search(query, top_k=self.bm25_top_k)
        chunks: List[RAGChunk] = []
        for doc, score in results:
            content = doc.content
            if not content:
                # Content not stored in loaded index; read from disk
                try:
                    fp = _REPO / doc.path
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                    content = "\n".join(lines[doc.line_start - 1:doc.line_end])
                except OSError:
                    continue
            chunks.append(RAGChunk(
                chunk_id=doc.doc_id, source="bm25", path=doc.path,
                content=content, line_start=doc.line_start,
                line_end=doc.line_end, bm25_score=score,
                mtime=doc.mtime,
            ))
        return chunks

    # ── Stage 2: Dense (vector) retrieval ────────────────────────

    def _dense_retrieve(self, query: str) -> List[RAGChunk]:
        """Semantic search via VectorMemoryStore."""
        store = self._get_dense()
        if not store:
            return []
        try:
            results = store.search(query_text=query, top_k=self.dense_top_k)
        except Exception:
            return []
        chunks: List[RAGChunk] = []
        for r in results:
            text = r.get("text", "")
            if not text:
                continue
            meta = r.get("metadata", {})
            chunks.append(RAGChunk(
                chunk_id=r.get("id", hashlib.md5(text.encode()).hexdigest()[:12]),
                source="dense", path=meta.get("path", "memory"),
                content=text, dense_score=r.get("score", 0.0),
            ))
        return chunks

    # ── Stage 3: Re-ranking ──────────────────────────────────────

    def _compute_recency(self, mtime: float) -> float:
        """Recency bonus: 1.0 for just-modified, decays over RECENCY_HOURS."""
        if mtime <= 0:
            return 0.0
        age_h = (time.time() - mtime) / 3600.0
        if age_h <= 0:
            return 1.0
        return max(0.0, 1.0 - age_h / RECENCY_HOURS)

    def _rerank(self, sparse: List[RAGChunk],
                dense: List[RAGChunk]) -> List[RAGChunk]:
        """Fuse sparse + dense results, dedup, score, and sort.

        Fusion strategy:
          - Normalise BM25 and dense scores to [0, 1]
          - Compute fused = W_BM25*bm25 + W_DENSE*dense + W_RECENCY*recency
          - Diversity bonus for chunks from under-represented files
          - Dedup by content hash
        """
        # Merge into a single map by chunk_id
        merged: Dict[str, RAGChunk] = {}
        for c in sparse:
            merged[c.content_hash] = c
        for c in dense:
            if c.content_hash in merged:
                merged[c.content_hash].dense_score = c.dense_score
                merged[c.content_hash].source = "both"
            else:
                merged[c.content_hash] = c

        candidates = list(merged.values())
        if not candidates:
            return []

        # Normalise scores to [0, 1]
        max_bm25 = max((c.bm25_score for c in candidates), default=1.0) or 1.0
        max_dense = max((c.dense_score for c in candidates), default=1.0) or 1.0

        # Count files for diversity bonus
        file_counts: Counter = Counter(c.path for c in candidates)

        for c in candidates:
            norm_bm25 = c.bm25_score / max_bm25
            norm_dense = c.dense_score / max_dense
            c.recency_bonus = self._compute_recency(c.mtime)

            # Diversity: rare files get a small bonus
            diversity = 1.0 / max(1, file_counts[c.path])

            c.fused_score = (
                W_BM25 * norm_bm25
                + W_DENSE * norm_dense
                + W_RECENCY * c.recency_bonus
                + W_DIVERSITY * diversity
            )

        # Sort by fused score descending
        candidates.sort(key=lambda c: -c.fused_score)
        return candidates[:self.final_top_k * 2]  # keep extra for budget trim

    # ── Context assembly ─────────────────────────────────────────

    def _assemble_context(self, chunks: List[RAGChunk],
                          max_tokens: int) -> Tuple[str, List[RAGChunk]]:
        """Assemble context string from ranked chunks within token budget.

        Returns (context_text, used_chunks).
        """
        used: List[RAGChunk] = []
        seen_hashes: Set[str] = set()
        budget = max_tokens
        parts: List[str] = []

        for c in chunks:
            if c.content_hash in seen_hashes:
                continue
            if c.token_count > budget:
                continue
            seen_hashes.add(c.content_hash)
            header = f"--- {c.path}"
            if c.line_start:
                header += f" L{c.line_start}-{c.line_end}"
            header += f" (score={c.fused_score:.3f}) ---"
            block = header + "\n" + c.content
            block_tokens = _estimate_tokens(block)
            if block_tokens > budget:
                continue
            parts.append(block)
            used.append(c)
            budget -= block_tokens
            if len(used) >= self.final_top_k:
                break

        return "\n\n".join(parts), used

    # ── Public API ───────────────────────────────────────────────

    def query(self, question: str,
              max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Run the full RAG pipeline and return assembled context.

        Args:
            question:   Natural language query.
            max_tokens: Override default token budget.

        Returns:
            dict with keys: context, chunks_used, total_candidates,
            sparse_count, dense_count, token_budget, tokens_used,
            elapsed_ms.
        """
        t0 = time.time()
        budget = max_tokens or self.max_tokens
        self.ensure_index()

        # Stage 1: sparse
        sparse = self._sparse_retrieve(question)
        # Stage 2: dense
        dense = self._dense_retrieve(question)
        # Stage 3: re-rank
        ranked = self._rerank(sparse, dense)
        # Assemble context
        context, used = self._assemble_context(ranked, budget)

        elapsed = (time.time() - t0) * 1000
        tokens_used = budget - (budget - sum(c.token_count for c in used))
        return {
            "context": context,
            "chunks_used": len(used),
            "total_candidates": len(sparse) + len(dense),
            "sparse_count": len(sparse),
            "dense_count": len(dense),
            "token_budget": budget,
            "tokens_used": tokens_used,
            "elapsed_ms": round(elapsed, 1),
            "sources": [
                {"path": c.path, "lines": f"{c.line_start}-{c.line_end}",
                 "score": round(c.fused_score, 4), "source": c.source,
                 "tokens": c.token_count}
                for c in used
            ],
        }

    def enrich_prompt(self, task: str, worker: str = "",
                      max_tokens: Optional[int] = None) -> str:
        """Build a prompt enriched with RAG context.

        Integrates with PromptAssembler if available, otherwise
        returns task + context block.
        """
        result = self.query(task, max_tokens=max_tokens)
        context = result["context"]
        if not context:
            return task

        try:
            from tools.skynet_prompt_assembly import PromptAssembler
            pa = PromptAssembler(max_tokens=max_tokens or self.max_tokens)
            return pa.assemble_prompt(task, worker=worker,
                                      max_tokens=max_tokens)
        except Exception:
            pass

        # Fallback: simple template
        return (
            f"## Task\n{task}\n\n"
            f"## Relevant Context (RAG, {result['chunks_used']} chunks, "
            f"{result['tokens_used']} tokens)\n\n{context}"
        )

    def get_stats(self) -> Dict[str, Any]:
        """Return pipeline statistics."""
        self.ensure_index()
        bm25_stats = self.bm25.stats()
        dense = self._get_dense()
        dense_stats = {}
        if dense:
            try:
                dense_stats = dense.stats()
            except Exception:
                dense_stats = {"available": False}
        return {
            "bm25": bm25_stats,
            "dense": dense_stats,
            "config": {
                "max_tokens": self.max_tokens,
                "bm25_top_k": self.bm25_top_k,
                "dense_top_k": self.dense_top_k,
                "final_top_k": self.final_top_k,
                "weights": {
                    "bm25": W_BM25, "dense": W_DENSE,
                    "recency": W_RECENCY, "diversity": W_DIVERSITY,
                },
            },
        }


# ── CLI ──────────────────────────────────────────────────────────────
# signed: alpha

def _cli() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Skynet Advanced RAG Pipeline — P3.08",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/skynet_rag.py query "How does ghost-type delivery work?"
  python tools/skynet_rag.py query "bus ring buffer" --max-tokens 2000
  python tools/skynet_rag.py index --dirs tools core Skynet docs
  python tools/skynet_rag.py stats
""",
    )
    sub = parser.add_subparsers(dest="command")

    # query
    q_p = sub.add_parser("query", help="Run RAG query")
    q_p.add_argument("question", help="Natural language query")
    q_p.add_argument("--max-tokens", "-t", type=int, default=4000)
    q_p.add_argument("--json", action="store_true",
                     help="Output raw JSON instead of formatted text")

    # index
    i_p = sub.add_parser("index", help="Build/rebuild BM25 index")
    i_p.add_argument("--dirs", nargs="*", default=None,
                     help="Directories to index (default: tools core Skynet docs)")

    # stats
    sub.add_parser("stats", help="Show pipeline statistics")

    args = parser.parse_args()
    rag = RAGPipeline()

    if args.command == "query":
        result = rag.query(args.question, max_tokens=args.max_tokens)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"RAG Query: {args.question}")
            print(f"  Sparse hits: {result['sparse_count']}, "
                  f"Dense hits: {result['dense_count']}")
            print(f"  Chunks used: {result['chunks_used']} / "
                  f"{result['total_candidates']} candidates")
            print(f"  Tokens: {result['tokens_used']} / "
                  f"{result['token_budget']} budget")
            print(f"  Elapsed: {result['elapsed_ms']:.0f}ms")
            print("=" * 70)
            if result["sources"]:
                print("Sources:")
                for s in result["sources"]:
                    print(f"  {s['path']} L{s['lines']} "
                          f"score={s['score']:.4f} ({s['source']}) "
                          f"~{s['tokens']}tok")
                print("=" * 70)
            if result["context"]:
                # Show first 2000 chars
                ctx = result["context"]
                if len(ctx) > 2000:
                    ctx = ctx[:2000] + "\n... (truncated)"
                print(ctx)
            else:
                print("No relevant context found.")

    elif args.command == "index":
        print("Building BM25 index...")
        stats = rag.build_index(dirs=args.dirs)
        print(f"  Total chunks: {stats['total_chunks']}")
        for d, n in stats["per_directory"].items():
            print(f"    {d}: {n} chunks")
        print(f"  Vocabulary: {stats['vocabulary_size']} terms")
        print(f"  Avg doc length: {stats['avg_doc_length']} tokens")
        print("Index saved.")

    elif args.command == "stats":
        rag.ensure_index()
        stats = rag.get_stats()
        print("RAG Pipeline Statistics")
        print("=" * 50)
        b = stats["bm25"]
        print(f"  BM25 Index:")
        print(f"    Documents:  {b['total_docs']}")
        print(f"    Files:      {b['unique_files']}")
        print(f"    Vocab:      {b['vocabulary_size']}")
        print(f"    Avg length: {b['avg_doc_length']}")
        d = stats.get("dense", {})
        if d:
            print(f"  Dense Store:")
            for k, v in d.items():
                print(f"    {k}: {v}")
        c = stats["config"]
        print(f"  Config:")
        print(f"    max_tokens:  {c['max_tokens']}")
        print(f"    bm25_top_k:  {c['bm25_top_k']}")
        print(f"    dense_top_k: {c['dense_top_k']}")
        print(f"    final_top_k: {c['final_top_k']}")
        w = c["weights"]
        print(f"    weights: bm25={w['bm25']}, dense={w['dense']}, "
              f"recency={w['recency']}, diversity={w['diversity']}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: alpha
