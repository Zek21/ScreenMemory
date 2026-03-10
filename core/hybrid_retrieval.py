"""
Hybrid Retrieval Engine with Reciprocal Rank Fusion (RRF).

Implements the paper's prescription: run vector search, BM25 keyword search,
and graph traversal in parallel, then fuse results using RRF.

RRF formula: score(d) = Σ 1/(k + rank_i(d))
where k=60 is the standard constant, and rank_i(d) is document d's rank
in retrieval method i.

This replaces naive single-method retrieval that either:
- Misses exact matches (vector-only)
- Misses semantic similarity (keyword-only)
- Loses relational context (both without graph)
"""
import re
import time
import math
import logging
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """A single retrieved memory/document."""
    id: str
    content: str
    score: float
    source: str         # "vector", "bm25", "graph", "fused"
    metadata: dict = field(default_factory=dict)
    rank: int = 0
    timestamp: float = 0.0


@dataclass
class FusedResult:
    """Result after RRF fusion across multiple retrieval methods."""
    id: str
    content: str
    rrf_score: float
    source_scores: Dict[str, float]   # Scores from each method
    source_ranks: Dict[str, int]      # Ranks from each method
    metadata: dict = field(default_factory=dict)
    timestamp: float = 0.0


class BM25Index:
    """
    Lightweight BM25 keyword search index.
    Implements Okapi BM25 for exact lexical matching — essential for
    error codes, identifiers, and specific terms that vector embeddings
    often misinterpret.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._documents: Dict[str, str] = {}  # id → content
        self._doc_lengths: Dict[str, int] = {}
        self._avg_doc_length: float = 0.0
        self._idf: Dict[str, float] = {}
        self._tf: Dict[str, Dict[str, int]] = {}  # doc_id → {term → count}
        self._dirty = True

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Simple whitespace + punctuation tokenizer."""
        return re.findall(r'\b\w+\b', text.lower())

    def add_document(self, doc_id: str, content: str):
        """Add or update a document in the index."""
        tokens = self._tokenize(content)
        self._documents[doc_id] = content
        self._doc_lengths[doc_id] = len(tokens)

        # Term frequency
        tf = defaultdict(int)
        for token in tokens:
            tf[token] += 1
        self._tf[doc_id] = dict(tf)
        self._dirty = True

    def remove_document(self, doc_id: str):
        """Remove a document from the index."""
        self._documents.pop(doc_id, None)
        self._doc_lengths.pop(doc_id, None)
        self._tf.pop(doc_id, None)
        self._dirty = True

    def _rebuild_idf(self):
        """Rebuild IDF scores (call after batch insertions)."""
        if not self._dirty or not self._documents:
            return

        n = len(self._documents)
        self._avg_doc_length = sum(self._doc_lengths.values()) / max(1, n)

        # Count document frequency for each term
        df = defaultdict(int)
        for doc_tf in self._tf.values():
            for term in doc_tf:
                df[term] += 1

        # IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        self._idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1)
            for term, freq in df.items()
        }
        self._dirty = False

    def search(self, query: str, limit: int = 10) -> List[RetrievalResult]:
        """Search the index using BM25 scoring."""
        self._rebuild_idf()
        query_tokens = self._tokenize(query)

        if not query_tokens or not self._documents:
            return []

        scores = {}
        for doc_id, doc_tf in self._tf.items():
            score = 0.0
            doc_len = self._doc_lengths.get(doc_id, 1)

            for token in query_tokens:
                if token not in doc_tf:
                    continue

                tf = doc_tf[token]
                idf = self._idf.get(token, 0)

                # BM25 formula
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / max(1, self._avg_doc_length))
                score += idf * numerator / denominator

            if score > 0:
                scores[doc_id] = score

        # Sort by score descending
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        return [
            RetrievalResult(
                id=doc_id,
                content=self._documents[doc_id],
                score=score,
                source="bm25",
                rank=i + 1,
            )
            for i, (doc_id, score) in enumerate(ranked)
        ]

    @property
    def size(self) -> int:
        return len(self._documents)


class HybridRetriever:
    """
    Runs multiple retrieval strategies in parallel and fuses via RRF.

    Strategies:
    1. Vector Search — deep semantic matching (via LanceDB)
    2. BM25 Keyword Search — exact lexical matching (built-in)
    3. Graph Traversal — relational/temporal context (via memory system)

    RRF fusion: score(d) = Σ 1/(k + rank_i(d))
    """

    def __init__(self, k: int = 60, lance_store=None, memory=None):
        """
        Args:
            k: RRF constant (standard=60, lower=more weight to top results)
            lance_store: LanceDBStore for vector search
            memory: EpisodicMemory for graph/structured retrieval
        """
        self.k = k
        self.bm25 = BM25Index()
        self.lance_store = lance_store
        self.memory = memory
        self._index_count = 0

    def index_document(self, doc_id: str, content: str, metadata: dict = None):
        """Index a document across all retrieval methods."""
        # BM25 index
        self.bm25.add_document(doc_id, content)
        self._index_count += 1

    def search(self, query: str, limit: int = 10,
               methods: List[str] = None) -> List[FusedResult]:
        """
        Execute hybrid search and fuse results via RRF.

        Args:
            query: Search query
            limit: Max results to return
            methods: Which methods to use ["bm25", "vector", "memory"]
                     Defaults to all available.
        """
        t0 = time.perf_counter()

        if methods is None:
            methods = ["bm25"]
            if self.lance_store:
                methods.append("vector")
            if self.memory:
                methods.append("memory")

        # Run all retrieval methods
        all_results: Dict[str, List[RetrievalResult]] = {}

        if "bm25" in methods:
            all_results["bm25"] = self.bm25.search(query, limit=limit * 2)

        if "vector" in methods and self.lance_store:
            all_results["vector"] = self._vector_search(query, limit * 2)

        if "memory" in methods and self.memory:
            all_results["memory"] = self._memory_search(query, limit * 2)

        # RRF Fusion
        fused = self._reciprocal_rank_fusion(all_results, limit)

        elapsed = (time.perf_counter() - t0) * 1000
        method_counts = {m: len(r) for m, r in all_results.items()}
        logger.info(f"Hybrid search: query='{query[:50]}...' methods={method_counts} "
                    f"→ {len(fused)} fused results [{elapsed:.1f}ms]")

        return fused

    def _vector_search(self, query: str, limit: int) -> List[RetrievalResult]:
        """Vector search via LanceDB."""
        try:
            results = self.lance_store.search_text(query, limit=limit)
            return [
                RetrievalResult(
                    id=str(r.get("id", i)),
                    content=r.get("analysis_text", "") or r.get("ocr_text", ""),
                    score=r.get("_distance", 0.0),
                    source="vector",
                    rank=i + 1,
                    metadata=r,
                )
                for i, r in enumerate(results)
            ]
        except Exception as e:
            logger.warning(f"Vector search failed: {e}")
            return []

    def _memory_search(self, query: str, limit: int) -> List[RetrievalResult]:
        """Search episodic/semantic memory."""
        try:
            entries = self.memory.retrieve(query, limit=limit)
            return [
                RetrievalResult(
                    id=entry.id,
                    content=entry.content,
                    score=entry.effective_utility,
                    source="memory",
                    rank=i + 1,
                    metadata={"type": entry.memory_type.value, "tags": entry.tags},
                    timestamp=entry.timestamp,
                )
                for i, entry in enumerate(entries)
            ]
        except Exception as e:
            logger.warning(f"Memory search failed: {e}")
            return []

    def _reciprocal_rank_fusion(self, results_by_method: Dict[str, List[RetrievalResult]],
                                 limit: int) -> List[FusedResult]:
        """
        Reciprocal Rank Fusion — fuses rankings from multiple methods.
        RRF(d) = Σ 1/(k + rank_i(d))

        Advantages over other fusion methods:
        - No hyperparameter tuning beyond k
        - Robust to score scale differences between methods
        - Consistently outperforms single-method retrieval
        """
        # Collect all document IDs and their content
        doc_content: Dict[str, str] = {}
        doc_metadata: Dict[str, dict] = {}
        doc_timestamps: Dict[str, float] = {}
        doc_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
        doc_ranks: Dict[str, Dict[str, int]] = defaultdict(dict)
        rrf_scores: Dict[str, float] = defaultdict(float)

        for method, results in results_by_method.items():
            for result in results:
                doc_id = result.id
                doc_content[doc_id] = result.content
                doc_metadata[doc_id] = result.metadata
                if result.timestamp:
                    doc_timestamps[doc_id] = result.timestamp

                doc_scores[doc_id][method] = result.score
                doc_ranks[doc_id][method] = result.rank

                # RRF formula
                rrf_scores[doc_id] += 1.0 / (self.k + result.rank)

        # Sort by RRF score
        ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:limit]

        return [
            FusedResult(
                id=doc_id,
                content=doc_content.get(doc_id, ""),
                rrf_score=score,
                source_scores=dict(doc_scores.get(doc_id, {})),
                source_ranks=dict(doc_ranks.get(doc_id, {})),
                metadata=doc_metadata.get(doc_id, {}),
                timestamp=doc_timestamps.get(doc_id, 0.0),
            )
            for doc_id, score in ranked
        ]

    @property
    def stats(self) -> dict:
        return {
            "bm25_documents": self.bm25.size,
            "total_indexed": self._index_count,
            "has_vector": self.lance_store is not None,
            "has_memory": self.memory is not None,
        }
