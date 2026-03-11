"""
Knowledge Distillation Engine.

When episodic memories decay below the utility threshold, they aren't
simply deleted. Instead, a background process summarizes them into
concise factual entries that get promoted to semantic memory.

This mimics human cognitive consolidation: you forget the specific
details of last Tuesday but retain the general lesson learned.

Workflow:
    1. Scan episodic memory for entries below decay threshold
    2. Group related low-utility entries by tags/context
    3. Use Ollama LLM to generate concise factual summaries
    4. Transfer summaries to semantic memory store
    5. Remove original episodic entries to free capacity

This ensures the agent retains operational knowledge while keeping
the episodic store lean and fast.

LOG FORMAT:
    [DISTILL] scan      -- 847 episodic entries, 23 below threshold (0.1)
    [DISTILL] group     -- 23 entries grouped into 5 clusters
    [DISTILL] summarize -- cluster "chrome navigation" -> 1 semantic entry
    [DISTILL] promote   -- 5 new semantic entries, freed 23 episodic slots
    [DISTILL] complete  -- distillation cycle in 2.3s
"""
import time
import json
import logging
from typing import List, Optional, Dict, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class KnowledgeDistiller:
    """
    Transforms decaying episodic memories into durable semantic knowledge.
    
    Uses LLM (via Ollama) when available for high-quality summarization,
    falls back to rule-based extraction when not.
    """

    def __init__(self, memory=None, ollama_model: str = "qwen3:8b",
                 decay_threshold: float = 0.1,
                 min_cluster_size: int = 2,
                 ollama_base_url: str = "http://localhost:11434"):
        """
        Args:
            memory: EpisodicMemory instance to operate on
            ollama_model: Model to use for summarization
            decay_threshold: Episodic entries below this utility get distilled
            min_cluster_size: Minimum entries needed to form a distillation cluster
            ollama_base_url: Ollama API endpoint
        """
        self.memory = memory
        self.model = ollama_model
        self.decay_threshold = decay_threshold
        self.min_cluster_size = min_cluster_size
        self.ollama_url = ollama_base_url
        self._distillation_count = 0
        self._total_distilled = 0

    def distill(self) -> dict:
        """Run a full distillation cycle. Returns stats dict with counts."""
        if not self.memory:
            return {"error": "No memory instance"}

        start = time.perf_counter()
        logger.info(f"[DISTILL] scan: {len(self.memory._episodic)} episodic entries")

        decayed = [m for m in self.memory._episodic if m.effective_utility < self.decay_threshold]
        logger.info(f"[DISTILL] found: {len(decayed)} below threshold {self.decay_threshold}")

        if len(decayed) < self.min_cluster_size:
            return {"distilled": 0, "freed": 0, "reason": "not_enough_entries"}

        clusters = self._cluster_entries(decayed)
        logger.info(f"[DISTILL] grouped: {len(clusters)} clusters")

        summaries = self._summarize_all_clusters(clusters)
        self._promote_summaries(summaries)

        elapsed = (time.perf_counter() - start) * 1000
        self._distillation_count += 1
        freed = sum(len(entries) for _, _, entries in summaries)
        self._total_distilled += freed

        result = {
            "distilled": len(summaries), "freed": freed,
            "elapsed_ms": elapsed,
            "semantic_count": len(self.memory._semantic),
            "episodic_remaining": len(self.memory._episodic),
        }
        logger.info(f"[DISTILL] complete: {freed} entries distilled into "
                     f"{len(summaries)} semantic entries ({elapsed:.0f}ms)")
        return result

    def _summarize_all_clusters(self, clusters: dict) -> list:
        """Summarize each cluster that meets minimum size."""
        summaries = []
        for topic, entries in clusters.items():
            if len(entries) < self.min_cluster_size:
                continue
            summary = self._summarize_cluster(topic, entries)
            if summary:
                summaries.append((topic, summary, entries))
        return summaries

    def _promote_summaries(self, summaries: list):
        """Promote summarized clusters to semantic memory and remove originals."""
        for topic, summary, entries in summaries:
            tags = list(set(tag for entry in entries for tag in entry.tags))[:5]
            tags.append("distilled")
            self.memory.store_semantic(content=summary, tags=tags, importance=0.7)

            entry_ids = {e.id for e in entries}
            self.memory._episodic = [m for m in self.memory._episodic if m.id not in entry_ids]
            logger.info(f"[DISTILL] promote: '{topic}' -> semantic ({len(entries)} entries freed)")

    def _cluster_entries(self, entries: list) -> Dict[str, list]:
        """Cluster entries by tag overlap and keyword similarity."""
        clusters = defaultdict(list)

        for entry in entries:
            # Primary clustering by first tag
            if entry.tags:
                key = entry.tags[0]
            elif entry.source_action:
                key = entry.source_action
            else:
                # Extract key topic from content
                words = entry.content.lower().split()[:3]
                key = "_".join(words) if words else "misc"

            clusters[key].append(entry)

        return dict(clusters)

    def _summarize_cluster(self, topic: str, entries: list) -> Optional[str]:
        """
        Summarize a cluster of related entries.
        Uses Ollama LLM if available, falls back to rule-based.
        """
        # Try LLM summarization
        llm_summary = self._llm_summarize(topic, entries)
        if llm_summary:
            return llm_summary

        # Fallback: rule-based summarization
        return self._rule_summarize(topic, entries)

    def _llm_summarize(self, topic: str, entries: list) -> Optional[str]:
        """Use Ollama to generate a concise summary."""
        try:
            import requests

            content_list = "\n".join(f"- {e.content[:200]}" for e in entries[:10])
            prompt = (
                f"Summarize these {len(entries)} related events about '{topic}' "
                f"into ONE concise factual statement (1-2 sentences max). "
                f"Extract the key operational lesson or pattern.\n\n"
                f"Events:\n{content_list}\n\n"
                f"Summary:"
            )

            response = requests.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 100},
                },
                timeout=30,
            )

            if response.status_code == 200:
                data = response.json()
                summary = data.get("response", "").strip()
                if summary and len(summary) > 20:
                    return f"[Distilled from {len(entries)} events] {summary}"

        except Exception as e:
            logger.debug(f"[DISTILL] LLM unavailable: {e}")

        return None

    def _rule_summarize(self, topic: str, entries: list) -> str:
        """
        Rule-based summarization when LLM is unavailable.
        Extracts common words and constructs a factual summary.
        """
        # Collect all words from entries
        all_words = []
        for entry in entries:
            all_words.extend(entry.content.lower().split())

        # Find most common meaningful words
        word_counts = {}
        stopwords = {"the", "a", "an", "is", "was", "are", "were", "to", "for",
                      "in", "on", "at", "of", "and", "or", "but", "with", "from",
                      "by", "as", "it", "this", "that", "i", "my"}
        for word in all_words:
            cleaned = word.strip(".,!?;:'\"()[]{}").lower()
            if cleaned and len(cleaned) > 2 and cleaned not in stopwords:
                word_counts[cleaned] = word_counts.get(cleaned, 0) + 1

        # Top keywords
        top_keywords = sorted(word_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        keywords_str = ", ".join(k for k, _ in top_keywords)

        # Construct summary
        actions = set(e.source_action for e in entries if e.source_action)
        action_str = ", ".join(actions) if actions else "various actions"

        summary = (
            f"[Pattern: {topic}] Observed {len(entries)} events involving "
            f"{action_str}. Key topics: {keywords_str}."
        )

        return summary

    @property
    def stats(self) -> dict:
        return {
            "distillation_cycles": self._distillation_count,
            "total_entries_distilled": self._total_distilled,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from core.cognitive.memory import EpisodicMemory

    print("=== Knowledge Distillation Test ===\n")

    memory = EpisodicMemory(working_capacity=5, episodic_capacity=100)

    # Add many episodic entries that will decay
    for i in range(20):
        memory.store_episodic(
            f"Navigated to page {i} of search results",
            tags=["navigation", "search"],
            source_action="navigate",
            importance=0.2,
        )

    for i in range(10):
        memory.store_episodic(
            f"Clicked button on Chrome browser interface",
            tags=["chrome", "click"],
            source_action="click",
            importance=0.2,
        )

    print(f"Before: {memory.get_stats()}")

    # Force decay by setting low utility
    for entry in memory._episodic:
        entry.utility_score = 0.01
        entry.decay_rate = 10.0  # Fast decay

    # Run distillation
    distiller = KnowledgeDistiller(memory=memory, decay_threshold=0.5)
    result = distiller.distill()

    print(f"Distillation result: {json.dumps(result, indent=2)}")
    print(f"After: {memory.get_stats()}")

    # Show semantic entries
    for sem in memory._semantic:
        print(f"  Semantic: {sem.content[:100]}")
