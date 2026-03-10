"""
Episodic Memory System with Decay and Utility Scoring.

Implements a biologically-inspired memory architecture that separates:
- Episodic Memory: Specific events ("I opened Chrome at 6:28 and searched for AI agents")
- Semantic Memory: Extracted rules and patterns ("Chrome tabs can be switched with Ctrl+Tab")
- Working Memory: Current task context (limited capacity, high priority)

Features:
- Utility-based decay: Less-useful memories decay faster
- Consolidation: Repeated patterns get promoted from episodic → semantic
- Relevance scoring: Memories are retrieved by contextual relevance, not just recency
- Capacity management: Working memory limited to ~7 items (Miller's Law)

This prevents the "context inflation" problem where accumulated history
drowns out the current task.
"""
import time
import json
import math
import hashlib
import logging
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class MemoryType(Enum):
    EPISODIC = "episodic"    # Specific events with temporal context
    SEMANTIC = "semantic"    # Extracted rules, patterns, knowledge
    WORKING = "working"      # Current task context (high priority, limited)


@dataclass
class MemoryEntry:
    """A single memory entry with metadata for decay and retrieval."""
    id: str
    memory_type: MemoryType
    content: str
    context: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    utility_score: float = 1.0
    decay_rate: float = 0.1
    tags: list = field(default_factory=list)
    source_action: str = ""
    importance: float = 0.5  # 0=trivial, 1=critical

    @property
    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600

    @property
    def effective_utility(self) -> float:
        """Current utility after time-based decay."""
        age = self.age_hours
        recency_bonus = 1.0 / (1.0 + age * self.decay_rate)
        frequency_bonus = math.log(1 + self.access_count) * 0.2
        return (self.utility_score * recency_bonus + frequency_bonus) * self.importance

    def access(self):
        """Mark this memory as accessed (boosts utility)."""
        self.last_accessed = time.time()
        self.access_count += 1
        self.utility_score = min(2.0, self.utility_score + 0.1)


class EpisodicMemory:
    """
    Manages the agent's memory across episodic, semantic, and working stores.
    
    Design Principles:
    1. Working memory is small (7±2 items) and always current
    2. Episodic memory stores specific events with temporal decay
    3. Semantic memory stores durable knowledge extracted from patterns
    4. Retrieval is by relevance (keyword match + utility score), not just recency
    5. Low-utility memories are automatically pruned
    
    LOG FORMAT:
        [MEMORY] store_episodic — id=ep_001, content="Opened Chrome...", importance=0.6
        [MEMORY] promote_semantic — pattern="tabs switch with ctrl+tab" (seen 5 times)
        [MEMORY] prune — removed 12 episodic memories below utility threshold 0.1
        [MEMORY] retrieve — query="chrome tabs", found 3 episodic + 1 semantic (12ms)
    """

    def __init__(self, working_capacity: int = 7,
                 episodic_capacity: int = 1000,
                 semantic_capacity: int = 500,
                 prune_threshold: float = 0.05,
                 consolidation_threshold: int = 3):
        self.working_capacity = working_capacity
        self.episodic_capacity = episodic_capacity
        self.semantic_capacity = semantic_capacity
        self.prune_threshold = prune_threshold
        self.consolidation_threshold = consolidation_threshold

        self._working: List[MemoryEntry] = []
        self._episodic: List[MemoryEntry] = []
        self._semantic: List[MemoryEntry] = []
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    # ── Store Operations ──

    def store_working(self, content: str, context: dict = None, importance: float = 0.8) -> MemoryEntry:
        """
        Store in working memory. If at capacity, evict lowest-utility entry
        (which drops to episodic memory).
        """
        entry = MemoryEntry(
            id=self._next_id("wk"),
            memory_type=MemoryType.WORKING,
            content=content,
            context=context or {},
            importance=importance,
            decay_rate=0.5,  # Working memory decays fast
        )

        self._working.append(entry)

        # Enforce capacity limit
        while len(self._working) > self.working_capacity:
            # Evict lowest utility
            self._working.sort(key=lambda m: m.effective_utility, reverse=True)
            evicted = self._working.pop()
            # Demote to episodic
            evicted.memory_type = MemoryType.EPISODIC
            evicted.decay_rate = 0.1
            self._episodic.append(evicted)
            logger.debug(f"Working memory eviction: {evicted.id} → episodic")

        logger.info(f"Working memory stored: {content[:80]}")
        return entry

    def store_episodic(self, content: str, context: dict = None,
                       importance: float = 0.5, tags: list = None,
                       source_action: str = "") -> MemoryEntry:
        """Store a specific event in episodic memory."""
        entry = MemoryEntry(
            id=self._next_id("ep"),
            memory_type=MemoryType.EPISODIC,
            content=content,
            context=context or {},
            importance=importance,
            tags=tags or [],
            source_action=source_action,
            decay_rate=0.1,
        )

        self._episodic.append(entry)

        # Check for consolidation into semantic memory
        self._check_consolidation(content, tags or [])

        # Prune if over capacity
        if len(self._episodic) > self.episodic_capacity:
            self._prune_episodic()

        return entry

    def store_semantic(self, content: str, tags: list = None,
                       importance: float = 0.7) -> MemoryEntry:
        """Store durable knowledge in semantic memory."""
        entry = MemoryEntry(
            id=self._next_id("sm"),
            memory_type=MemoryType.SEMANTIC,
            content=content,
            importance=importance,
            tags=tags or [],
            decay_rate=0.01,  # Semantic memory decays very slowly
        )

        self._semantic.append(entry)
        logger.info(f"Semantic memory stored: {content[:80]}")
        return entry

    # ── Retrieval Operations ──

    def retrieve(self, query: str, limit: int = 10,
                 memory_types: List[MemoryType] = None,
                 min_utility: float = 0.0) -> List[MemoryEntry]:
        """
        Retrieve memories by relevance.
        Scores: keyword_match * utility_score → sorted descending.
        """
        if memory_types is None:
            memory_types = [MemoryType.WORKING, MemoryType.SEMANTIC, MemoryType.EPISODIC]

        candidates = []
        for mtype in memory_types:
            store = self._get_store(mtype)
            candidates.extend(store)

        # Score by relevance
        query_lower = query.lower()
        query_words = set(query_lower.split())

        scored = []
        for mem in candidates:
            if mem.effective_utility < min_utility:
                continue

            # Keyword relevance
            content_lower = mem.content.lower()
            tag_text = " ".join(mem.tags).lower()
            combined = content_lower + " " + tag_text

            word_matches = sum(1 for w in query_words if w in combined)
            if word_matches == 0 and query_lower not in combined:
                continue

            keyword_score = word_matches / max(len(query_words), 1)
            total_score = keyword_score * 0.6 + mem.effective_utility * 0.4

            scored.append((total_score, mem))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Mark as accessed
        results = []
        for score, mem in scored[:limit]:
            mem.access()
            results.append(mem)

        return results

    def get_working_context(self) -> List[MemoryEntry]:
        """Get current working memory (sorted by utility)."""
        self._working.sort(key=lambda m: m.effective_utility, reverse=True)
        return list(self._working)

    def get_recent_episodic(self, n: int = 10) -> List[MemoryEntry]:
        """Get most recent episodic memories."""
        sorted_ep = sorted(self._episodic, key=lambda m: m.timestamp, reverse=True)
        return sorted_ep[:n]

    def get_all_semantic(self) -> List[MemoryEntry]:
        """Get all semantic (durable) knowledge."""
        return list(self._semantic)

    # ── Maintenance Operations ──

    def _check_consolidation(self, content: str, tags: list):
        """
        Check if a pattern has appeared enough times in episodic memory
        to warrant promotion to semantic memory.
        """
        # Hash the content for pattern matching
        content_hash = hashlib.md5(content.lower().encode()).hexdigest()[:8]

        # Count similar episodic entries
        similar_count = 0
        for mem in self._episodic[-100:]:  # Check recent entries
            if self._content_similarity(content, mem.content) > 0.7:
                similar_count += 1

        if similar_count >= self.consolidation_threshold:
            # Promote to semantic
            self.store_semantic(
                content=f"[Pattern] {content}",
                tags=tags + ["consolidated"],
                importance=0.8,
            )
            logger.info(f"Consolidated to semantic: '{content[:60]}' (seen {similar_count} times)")

    def _content_similarity(self, a: str, b: str) -> float:
        """Simple word-overlap similarity between two strings."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def _prune_episodic(self):
        """Remove low-utility episodic memories."""
        before = len(self._episodic)
        self._episodic = [
            m for m in self._episodic
            if m.effective_utility >= self.prune_threshold
        ]
        pruned = before - len(self._episodic)
        if pruned > 0:
            logger.info(f"Pruned {pruned} episodic memories (threshold={self.prune_threshold})")

    def _get_store(self, mtype: MemoryType) -> List[MemoryEntry]:
        """Get the memory store for a given type."""
        if mtype == MemoryType.WORKING:
            return self._working
        elif mtype == MemoryType.EPISODIC:
            return self._episodic
        elif mtype == MemoryType.SEMANTIC:
            return self._semantic
        return []

    def clear_working(self):
        """Clear working memory (e.g., when switching tasks)."""
        for mem in self._working:
            mem.memory_type = MemoryType.EPISODIC
            mem.decay_rate = 0.1
            self._episodic.append(mem)
        self._working.clear()
        logger.info("Working memory cleared → episodic")

    # ── Serialization ──

    def get_stats(self) -> dict:
        return {
            "working": len(self._working),
            "working_capacity": self.working_capacity,
            "episodic": len(self._episodic),
            "semantic": len(self._semantic),
            "total": len(self._working) + len(self._episodic) + len(self._semantic),
        }

    def to_context_string(self, max_chars: int = 2000) -> str:
        """
        Serialize current memory state into a context string for VLM prompts.
        Prioritizes working memory, then recent/relevant episodic + semantic.
        """
        parts = []

        # Working memory (always included)
        if self._working:
            parts.append("## Current Task Context")
            for mem in self.get_working_context():
                parts.append(f"- {mem.content}")

        # Semantic knowledge (always included, it's durable)
        if self._semantic:
            parts.append("\n## Known Patterns")
            for mem in self._semantic[-5:]:
                parts.append(f"- {mem.content}")

        # Recent episodic (if space allows)
        recent = self.get_recent_episodic(5)
        if recent:
            parts.append("\n## Recent Actions")
            for mem in recent:
                parts.append(f"- {mem.content}")

        result = "\n".join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n[...truncated]"
        return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== Episodic Memory System Test ===\n")

    mem = EpisodicMemory(working_capacity=5)

    # Store working memory
    mem.store_working("Current task: Build autonomous web navigator", importance=1.0)
    mem.store_working("Using Set-of-Mark grounding for visual interaction", importance=0.8)
    mem.store_working("Target: Chrome browser on right monitor", importance=0.7)

    # Store episodic events
    mem.store_episodic("Captured screenshot of Chrome with Google Gemini open",
                       tags=["chrome", "gemini", "screenshot"], source_action="capture")
    mem.store_episodic("Detected 18 interactive regions on screen",
                       tags=["grounding", "regions"], source_action="ground")
    mem.store_episodic("VLM identified Chrome browser with search results",
                       tags=["vlm", "chrome", "analysis"], source_action="analyze")

    # Store semantic knowledge
    mem.store_semantic("Chrome tabs can be switched with Ctrl+Tab",
                       tags=["chrome", "navigation", "keyboard"])
    mem.store_semantic("VS Code terminal accepts PowerShell commands",
                       tags=["vscode", "terminal", "powershell"])

    # Retrieve by query
    print("Query: 'chrome browser'")
    results = mem.retrieve("chrome browser", limit=5)
    for r in results:
        print(f"  [{r.memory_type.value}] {r.content[:80]} (utility={r.effective_utility:.2f})")

    # Stats
    print(f"\nMemory stats: {json.dumps(mem.get_stats())}")

    # Context string
    print(f"\nContext for VLM:\n{mem.to_context_string(500)}")
