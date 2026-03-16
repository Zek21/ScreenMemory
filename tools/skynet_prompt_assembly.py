"""Skynet Dynamic Prompt Assembly (RAG-Enhanced) — P2.08

Builds context-rich prompts by fusing multiple knowledge sources:
  1. Recent bus messages (recency-weighted)
  2. Worker learnings from LearningStore (BM25 relevance)
  3. Code graph context from skynet_code_graph (impact analysis)
  4. Task history from data/task_contexts/ (prior results)
  5. Incident patterns from data/incidents.json (lessons learned)

Usage:
    from tools.skynet_prompt_assembly import PromptAssembler
    pa = PromptAssembler()
    prompt = pa.assemble_prompt("Fix auth XSS bug", worker="gamma")

CLI:
    python tools/skynet_prompt_assembly.py preview --task "Fix auth XSS" --worker gamma
    python tools/skynet_prompt_assembly.py sources --task "Fix auth XSS"
    python tools/skynet_prompt_assembly.py budget --max-tokens 6000
"""
# signed: gamma

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# ── Paths ────────────────────────────────────────────────────────────
INCIDENTS_PATH = _REPO / "data" / "incidents.json"
CONTEXTS_DIR = _REPO / "data" / "task_contexts"
CODE_GRAPH_PATH = _REPO / "data" / "code_graph.json"

# ── Token Budget Constants ───────────────────────────────────────────
# signed: gamma
DEFAULT_MAX_TOKENS = 4000
CHARS_PER_TOKEN = 4  # conservative estimate for English text

# Budget allocation percentages (must sum to 1.0)
BUDGET_TASK = 0.15        # task description slot
BUDGET_CONTEXT = 0.25     # bus messages / general context
BUDGET_LEARNINGS = 0.25   # LearningStore facts
BUDGET_CODE_GRAPH = 0.15  # code graph impact
BUDGET_INCIDENTS = 0.10   # incident patterns
BUDGET_HISTORY = 0.10     # task history from contexts

# Relevance scoring weights
RECENCY_WEIGHT = 0.40
KEYWORD_WEIGHT = 0.60

# Limits for source queries
MAX_BUS_MESSAGES = 30
MAX_LEARNINGS = 10
MAX_INCIDENTS = 5
MAX_TASK_CONTEXTS = 5
MAX_CODE_GRAPH_CALLERS = 10


# ── Data Structures ─────────────────────────────────────────────────

@dataclass
class ContextChunk:
    """A scored piece of context from any source."""
    source: str          # "bus", "learning", "code_graph", "incident", "history"
    content: str         # text content
    relevance: float     # 0.0-1.0
    recency: float       # 0.0-1.0 (1.0 = now, decays with age)
    token_cost: int      # estimated token count
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Composite score: weighted blend of relevance and recency."""
        return KEYWORD_WEIGHT * self.relevance + RECENCY_WEIGHT * self.recency


@dataclass
class TokenBudget:
    """Token allocation tracker across context sources."""
    max_tokens: int
    allocated: Dict[str, int] = field(default_factory=dict)
    used: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.allocated = {
            "task": int(self.max_tokens * BUDGET_TASK),
            "context": int(self.max_tokens * BUDGET_CONTEXT),
            "learnings": int(self.max_tokens * BUDGET_LEARNINGS),
            "code_graph": int(self.max_tokens * BUDGET_CODE_GRAPH),
            "incidents": int(self.max_tokens * BUDGET_INCIDENTS),
            "history": int(self.max_tokens * BUDGET_HISTORY),
        }
        self.used = {k: 0 for k in self.allocated}

    def remaining(self, source: str) -> int:
        return max(0, self.allocated.get(source, 0) - self.used.get(source, 0))

    def consume(self, source: str, tokens: int) -> int:
        """Consume tokens from a source budget. Returns actual consumed."""
        avail = self.remaining(source)
        actual = min(tokens, avail)
        self.used[source] = self.used.get(source, 0) + actual
        return actual

    def total_remaining(self) -> int:
        return sum(self.remaining(s) for s in self.allocated)

    def summary(self) -> Dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "sources": {
                k: {"allocated": self.allocated[k],
                     "used": self.used[k],
                     "remaining": self.remaining(k)}
                for k in self.allocated
            },
            "total_used": sum(self.used.values()),
            "total_remaining": self.total_remaining(),
        }


# ── Utility Functions ────────────────────────────────────────────────
# signed: gamma

def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to fit within a token budget."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 20] + "\n... (truncated)"


def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from task text for relevance matching."""
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "and", "but", "or",
        "not", "no", "all", "each", "every", "both", "few", "more", "most",
        "other", "some", "such", "than", "too", "very", "just", "also",
        "it", "its", "this", "that", "these", "those", "then", "so",
        "if", "when", "what", "which", "who", "how", "where", "why",
    }
    words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text.lower())
    return [w for w in words if w not in stop_words and len(w) > 2]


def _keyword_overlap(keywords: List[str], text: str) -> float:
    """Score 0.0-1.0 based on keyword overlap with text."""
    if not keywords:
        return 0.0
    text_lower = text.lower()
    hits = sum(1 for kw in keywords if kw in text_lower)
    return min(1.0, hits / max(1, len(keywords)))


def _recency_score(timestamp_str: str) -> float:
    """Score 0.0-1.0 based on how recent a timestamp is.

    Uses exponential decay with half-life of 1 hour.
    """
    try:
        from datetime import datetime, timezone
        if isinstance(timestamp_str, (int, float)):
            ts = float(timestamp_str)
        else:
            ts_clean = timestamp_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_clean)
            ts = dt.timestamp()
        age_s = max(0, time.time() - ts)
        half_life = 3600.0  # 1 hour
        return math.exp(-0.693 * age_s / half_life)
    except Exception:
        return 0.1  # unknown timestamp gets low recency


# ── Context Source Fetchers ──────────────────────────────────────────
# signed: gamma

def _fetch_bus_context(keywords: List[str], max_items: int = MAX_BUS_MESSAGES) -> List[ContextChunk]:
    """Fetch recent bus messages and score by relevance to task keywords."""
    chunks: List[ContextChunk] = []
    try:
        import urllib.request
        url = f"http://localhost:8420/bus/messages?limit={max_items}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return chunks

    messages = data if isinstance(data, list) else data.get("messages", [])
    for msg in messages:
        content = msg.get("content", "")
        sender = msg.get("sender", "?")
        topic = msg.get("topic", "?")
        msg_type = msg.get("type", "?")
        ts = msg.get("timestamp", "")

        # Skip system noise
        if msg_type in ("heartbeat", "daemon_health"):
            continue

        relevance = _keyword_overlap(keywords, content)
        recency = _recency_score(ts) if ts else 0.1

        summary = f"[{sender}/{topic}/{msg_type}] {content[:300]}"
        chunks.append(ContextChunk(
            source="bus",
            content=summary,
            relevance=relevance,
            recency=recency,
            token_cost=_estimate_tokens(summary),
            metadata={"sender": sender, "topic": topic, "type": msg_type},
        ))

    return chunks


def _fetch_learnings(keywords: List[str], max_items: int = MAX_LEARNINGS) -> List[ContextChunk]:
    """Query LearningStore for facts relevant to task keywords."""
    chunks: List[ContextChunk] = []
    query = " ".join(keywords[:8])  # BM25 query from top keywords
    if not query:
        return chunks

    try:
        from core.learning_store import LearningStore
        store = LearningStore()
        facts = store.recall(query, top_k=max_items)
    except Exception:
        return chunks

    for fact in facts:
        relevance = _keyword_overlap(keywords, fact.content)
        recency = _recency_score(fact.last_accessed) if fact.last_accessed else 0.3
        confidence_boost = fact.confidence * 0.2  # high-confidence facts get a bump

        text = f"[{fact.category}|conf={fact.confidence:.2f}] {fact.content}"
        if fact.tags:
            text += f" (tags: {', '.join(fact.tags[:5])})"

        chunks.append(ContextChunk(
            source="learning",
            content=text,
            relevance=min(1.0, relevance + confidence_boost),
            recency=recency,
            token_cost=_estimate_tokens(text),
            metadata={"fact_id": fact.fact_id, "category": fact.category,
                       "confidence": fact.confidence},
        ))

    return chunks


def _fetch_code_graph(keywords: List[str], max_callers: int = MAX_CODE_GRAPH_CALLERS) -> List[ContextChunk]:
    """Extract code graph context for files/functions mentioned in task."""
    chunks: List[ContextChunk] = []

    # Detect file paths and function names from keywords
    file_pattern = re.compile(r'[\w/\\]+\.py')
    func_pattern = re.compile(r'[a-z_][a-z0-9_]*')
    task_text = " ".join(keywords)

    # Try to load graph
    if not CODE_GRAPH_PATH.exists():
        return chunks
    try:
        with open(CODE_GRAPH_PATH, "r", encoding="utf-8") as f:
            graph = json.load(f)
    except Exception:
        return chunks

    # Find function nodes that match keywords
    functions = graph.get("nodes", {}).get("functions", {})
    matched_fqns: List[str] = []
    for fqn, info in functions.items():
        name = info.get("name", "")
        if name in keywords or any(kw in fqn.lower() for kw in keywords[:5]):
            matched_fqns.append(fqn)
            if len(matched_fqns) >= 5:
                break

    if not matched_fqns:
        return chunks

    # Get call edges for matched functions
    calls = graph.get("edges", {}).get("calls", [])
    for fqn in matched_fqns:
        callers = [e for e in calls if e.get("callee", "") == fqn][:max_callers]
        callees = [e for e in calls if e.get("caller", "") == fqn][:max_callers]

        if callers or callees:
            parts = [f"Function: {fqn}"]
            if callers:
                caller_strs = [f"  <- {c['caller']} ({c.get('file', '?')}:{c.get('line', '?')})"
                               for c in callers[:5]]
                parts.append(f"  Called by ({len(callers)} callers):")
                parts.extend(caller_strs)
            if callees:
                callee_strs = [f"  -> {c['callee']}" for c in callees[:5]]
                parts.append(f"  Calls ({len(callees)} functions):")
                parts.extend(callee_strs)

            text = "\n".join(parts)
            chunks.append(ContextChunk(
                source="code_graph",
                content=text,
                relevance=0.8,  # matched by keyword, so highly relevant
                recency=0.5,    # code graph is static, moderate recency
                token_cost=_estimate_tokens(text),
                metadata={"fqn": fqn, "callers": len(callers), "callees": len(callees)},
            ))

    return chunks


def _fetch_incidents(keywords: List[str], max_items: int = MAX_INCIDENTS) -> List[ContextChunk]:
    """Load incident patterns relevant to the task."""
    chunks: List[ContextChunk] = []
    if not INCIDENTS_PATH.exists():
        return chunks

    try:
        with open(INCIDENTS_PATH, "r", encoding="utf-8") as f:
            incidents = json.load(f)
    except Exception:
        return chunks

    if not isinstance(incidents, list):
        return chunks

    scored: List[Tuple[float, dict]] = []
    for inc in incidents:
        title = inc.get("title", "")
        root_causes = inc.get("root_causes", [])
        arch_knowledge = inc.get("architecture_knowledge", {})

        searchable = title
        for rc in root_causes:
            searchable += " " + rc.get("issue", "") + " " + rc.get("fix", "")
        for v in arch_knowledge.values():
            searchable += " " + str(v)

        relevance = _keyword_overlap(keywords, searchable)
        if relevance > 0.05:
            scored.append((relevance, inc))

    scored.sort(key=lambda x: x[0], reverse=True)

    for relevance, inc in scored[:max_items]:
        inc_id = inc.get("id", "?")
        title = inc.get("title", "?")
        severity = inc.get("severity", "?")
        causes = inc.get("root_causes", [])

        parts = [f"[{inc_id}] {title} (severity={severity})"]
        for rc in causes[:2]:
            parts.append(f"  Issue: {rc.get('issue', '?')[:150]}")
            parts.append(f"  Fix: {rc.get('fix', '?')[:150]}")

        arch = inc.get("architecture_knowledge", {})
        for k, v in list(arch.items())[:2]:
            parts.append(f"  Knowledge: {k}: {str(v)[:120]}")

        text = "\n".join(parts)
        chunks.append(ContextChunk(
            source="incident",
            content=text,
            relevance=relevance,
            recency=0.3,  # incidents are historical
            token_cost=_estimate_tokens(text),
            metadata={"id": inc_id, "severity": severity},
        ))

    return chunks


def _fetch_task_history(keywords: List[str], worker: str = "",
                        max_items: int = MAX_TASK_CONTEXTS) -> List[ContextChunk]:
    """Load relevant task context history."""
    chunks: List[ContextChunk] = []
    if not CONTEXTS_DIR.exists():
        return chunks

    try:
        from tools.skynet_task_context import list_contexts
        contexts = list_contexts()
    except Exception:
        return chunks

    scored: List[Tuple[float, dict]] = []
    for ctx_summary in contexts:
        goal = ctx_summary.get("goal", "")
        relevance = _keyword_overlap(keywords, goal)
        # Boost relevance if same worker
        if worker and ctx_summary.get("assignee") == worker:
            relevance = min(1.0, relevance + 0.15)
        if relevance > 0.05 or ctx_summary.get("assignee") == worker:
            scored.append((max(relevance, 0.1), ctx_summary))

    scored.sort(key=lambda x: x[0], reverse=True)

    for relevance, ctx_s in scored[:max_items]:
        status = ctx_s.get("status", "?")
        assignee = ctx_s.get("assignee", "?")
        phases = ctx_s.get("phases_completed", 0)
        tests_passed = ctx_s.get("tests_passed", 0)
        tests_run = ctx_s.get("tests_run", 0)
        goal = ctx_s.get("goal", "?")[:100]

        text = (f"[{ctx_s.get('task_id', '?')}] {goal} "
                f"(status={status}, assignee={assignee}, "
                f"phases={phases}, tests={tests_passed}/{tests_run})")
        recency = _recency_score(ctx_s.get("created_at", "")) if ctx_s.get("created_at") else 0.2

        chunks.append(ContextChunk(
            source="history",
            content=text,
            relevance=relevance,
            recency=recency,
            token_cost=_estimate_tokens(text),
            metadata={"task_id": ctx_s.get("task_id"), "status": status},
        ))

    return chunks


# ── Template System ──────────────────────────────────────────────────
# signed: gamma

DEFAULT_TEMPLATE = """{{task}}

{{context}}

{{learnings}}

{{code_graph}}

{{incidents}}

{{history}}"""

MINIMAL_TEMPLATE = """{{task}}"""


def _render_template(template: str, slots: Dict[str, str]) -> str:
    """Render a template by replacing {{slot}} placeholders."""
    result = template
    for key, value in slots.items():
        placeholder = "{{" + key + "}}"
        result = result.replace(placeholder, value)
    # Remove any unfilled slots
    result = re.sub(r'\{\{[a-z_]+\}\}', '', result)
    # Collapse multiple blank lines
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def _format_section(title: str, chunks: List[ContextChunk]) -> str:
    """Format a list of context chunks into a titled section."""
    if not chunks:
        return ""
    lines = [f"── {title} ──"]
    for chunk in chunks:
        lines.append(chunk.content)
    return "\n".join(lines)


# ── PromptAssembler ──────────────────────────────────────────────────
# signed: gamma

class PromptAssembler:
    """RAG-enhanced prompt builder that fuses multiple knowledge sources.

    Fetches context from 5 sources (bus, learnings, code graph, incidents,
    task history), scores by relevance + recency, allocates a token budget
    across sources, and renders a template-based prompt.

    Args:
        max_tokens:  Total token budget for the assembled prompt.
        template:    Template string with {{slot}} placeholders.
                     Slots: task, context, learnings, code_graph, incidents, history.
    """

    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS,
                 template: Optional[str] = None):
        self.max_tokens = max_tokens
        self.template = template or DEFAULT_TEMPLATE
        self._last_budget: Optional[TokenBudget] = None
        self._last_chunks: Dict[str, List[ContextChunk]] = {}

    def assemble_prompt(self, task: str, worker: str = "",
                        max_tokens: Optional[int] = None) -> str:
        """Build a context-rich prompt for the given task and worker.

        Fetches relevant context from all sources, scores and ranks it,
        then assembles within the token budget.

        Args:
            task:       The task description / goal.
            worker:     Worker name for personalization (optional).
            max_tokens: Override the instance-level token budget.

        Returns:
            Assembled prompt string ready for dispatch.
        """
        budget = TokenBudget(max_tokens or self.max_tokens)
        keywords = _extract_keywords(task)

        # ── Fetch from all sources ───────────────────────────────
        bus_chunks = _fetch_bus_context(keywords)
        learning_chunks = _fetch_learnings(keywords)
        graph_chunks = _fetch_code_graph(keywords)
        incident_chunks = _fetch_incidents(keywords)
        history_chunks = _fetch_task_history(keywords, worker=worker)

        # ── Sort each source by composite score ──────────────────
        for chunk_list in [bus_chunks, learning_chunks, graph_chunks,
                           incident_chunks, history_chunks]:
            chunk_list.sort(key=lambda c: c.score, reverse=True)

        # ── Select chunks within budget per source ───────────────
        source_map = {
            "context": bus_chunks,
            "learnings": learning_chunks,
            "code_graph": graph_chunks,
            "incidents": incident_chunks,
            "history": history_chunks,
        }

        selected: Dict[str, List[ContextChunk]] = {k: [] for k in source_map}

        for source_key, chunks in source_map.items():
            for chunk in chunks:
                avail = budget.remaining(source_key)
                if avail <= 0:
                    break
                if chunk.token_cost <= avail:
                    budget.consume(source_key, chunk.token_cost)
                    selected[source_key].append(chunk)

        # ── Redistribute unused budget to hungry sources ─────────
        # If a source has leftover budget, redistribute to sources that
        # still have high-scoring chunks waiting.
        leftover = sum(budget.remaining(s) for s in source_map
                       if not source_map[s])  # sources with no data
        if leftover > 0:
            # Find sources that could use more
            hungry = [(k, cs) for k, cs in source_map.items()
                      if len(selected[k]) < len(cs)]
            for source_key, chunks in hungry:
                if leftover <= 0:
                    break
                already = len(selected[source_key])
                for chunk in chunks[already:]:
                    if chunk.token_cost <= leftover:
                        leftover -= chunk.token_cost
                        budget.used[source_key] = budget.used.get(source_key, 0) + chunk.token_cost
                        selected[source_key].append(chunk)

        # ── Build task slot ──────────────────────────────────────
        task_text = _truncate_to_tokens(task, budget.allocated.get("task", 600))
        budget.consume("task", _estimate_tokens(task_text))

        # ── Render slots ─────────────────────────────────────────
        slots = {
            "task": task_text,
            "context": _format_section("RECENT BUS CONTEXT", selected["context"]),
            "learnings": _format_section("RELEVANT LEARNINGS", selected["learnings"]),
            "code_graph": _format_section("CODE GRAPH", selected["code_graph"]),
            "incidents": _format_section("INCIDENT PATTERNS", selected["incidents"]),
            "history": _format_section("TASK HISTORY", selected["history"]),
        }

        # If zero context from all sources, return task-only (fallback)
        total_context_chunks = sum(len(v) for v in selected.values())
        if total_context_chunks == 0:
            self._last_budget = budget
            self._last_chunks = selected
            return task_text

        prompt = _render_template(self.template, slots)

        # Final safety truncation
        final_max = (max_tokens or self.max_tokens) * CHARS_PER_TOKEN
        if len(prompt) > final_max:
            prompt = prompt[:final_max - 20] + "\n... (truncated)"

        self._last_budget = budget
        self._last_chunks = selected
        return prompt

    def get_budget_summary(self) -> Dict[str, Any]:
        """Return the token budget summary from the last assembly."""
        if self._last_budget:
            return self._last_budget.summary()
        return {"error": "No assembly performed yet"}

    def get_source_stats(self) -> Dict[str, Any]:
        """Return stats about context sources from the last assembly."""
        if not self._last_chunks:
            return {"error": "No assembly performed yet"}
        stats: Dict[str, Any] = {}
        for source, chunks in self._last_chunks.items():
            if chunks:
                scores = [c.score for c in chunks]
                stats[source] = {
                    "chunks_selected": len(chunks),
                    "total_tokens": sum(c.token_cost for c in chunks),
                    "avg_score": round(sum(scores) / len(scores), 3),
                    "max_score": round(max(scores), 3),
                }
            else:
                stats[source] = {"chunks_selected": 0, "total_tokens": 0}
        return stats

    def preview(self, task: str, worker: str = "",
                max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Assemble a prompt and return it with diagnostics.

        Returns a dict with the prompt, budget summary, and source stats.
        Useful for debugging and tuning context selection.
        """
        prompt = self.assemble_prompt(task, worker=worker, max_tokens=max_tokens)
        return {
            "prompt": prompt,
            "prompt_tokens": _estimate_tokens(prompt),
            "budget": self.get_budget_summary(),
            "sources": self.get_source_stats(),
            "keywords": _extract_keywords(task)[:15],
            "worker": worker or "(none)",
        }


# ── Module-Level Convenience Functions ───────────────────────────────
# signed: gamma

_default_assembler: Optional[PromptAssembler] = None


def _get_assembler() -> PromptAssembler:
    global _default_assembler
    if _default_assembler is None:
        _default_assembler = PromptAssembler()
    return _default_assembler


def assemble_prompt(task: str, worker: str = "",
                    max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """Module-level convenience: assemble a context-rich prompt.

    Args:
        task:       Task description / goal.
        worker:     Worker name for personalization.
        max_tokens: Token budget.

    Returns:
        Assembled prompt string.
    """
    pa = _get_assembler()
    return pa.assemble_prompt(task, worker=worker, max_tokens=max_tokens)


def preview_prompt(task: str, worker: str = "",
                   max_tokens: int = DEFAULT_MAX_TOKENS) -> Dict[str, Any]:
    """Module-level convenience: preview prompt with diagnostics."""
    pa = _get_assembler()
    return pa.preview(task, worker=worker, max_tokens=max_tokens)


# ── CLI ──────────────────────────────────────────────────────────────
# signed: gamma

def _cli():
    # Force UTF-8 output on Windows to avoid cp1252 encoding errors
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(
        description="Skynet Dynamic Prompt Assembly (RAG-Enhanced) -- P2.08"
    )
    sub = parser.add_subparsers(dest="command")

    # preview
    prev = sub.add_parser("preview",
                          help="Assemble and preview a prompt with diagnostics")
    prev.add_argument("--task", required=True, help="Task description")
    prev.add_argument("--worker", default="", help="Worker name")
    prev.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                      help=f"Token budget (default: {DEFAULT_MAX_TOKENS})")

    # sources
    src = sub.add_parser("sources",
                         help="Show which context sources would be used")
    src.add_argument("--task", required=True, help="Task description")
    src.add_argument("--worker", default="", help="Worker name")
    src.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)

    # budget
    bdg = sub.add_parser("budget",
                         help="Show token budget allocation for given max tokens")
    bdg.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)

    # keywords
    kw = sub.add_parser("keywords",
                        help="Show extracted keywords from a task description")
    kw.add_argument("--task", required=True, help="Task description")

    args = parser.parse_args()

    if args.command == "preview":
        result = preview_prompt(args.task, worker=args.worker,
                                max_tokens=args.max_tokens)
        print("═══ ASSEMBLED PROMPT ═══")
        print(result["prompt"])
        print()
        print(f"═══ DIAGNOSTICS ═══")
        print(f"Prompt tokens: ~{result['prompt_tokens']}")
        print(f"Worker: {result['worker']}")
        print(f"Keywords: {', '.join(result['keywords'])}")
        print()
        print("Budget:")
        budget = result["budget"]
        for src_name, info in budget.get("sources", {}).items():
            bar_len = 20
            used_frac = info["used"] / max(1, info["allocated"])
            filled = int(bar_len * used_frac)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"  {src_name:<12} [{bar}] {info['used']:>4}/{info['allocated']:>4} tokens")
        print(f"  {'TOTAL':<12} {budget.get('total_used', 0):>4}/{budget.get('max_tokens', 0):>4} tokens")
        print()
        print("Sources:")
        for src_name, info in result.get("sources", {}).items():
            if info.get("chunks_selected", 0) > 0:
                print(f"  {src_name:<12} {info['chunks_selected']} chunks, "
                      f"~{info['total_tokens']} tokens, "
                      f"avg_score={info.get('avg_score', 0):.3f}")
            else:
                print(f"  {src_name:<12} (no data)")

    elif args.command == "sources":
        pa = PromptAssembler(max_tokens=args.max_tokens)
        keywords = _extract_keywords(args.task)
        print(f"Keywords: {', '.join(keywords[:15])}")
        print()

        sources = {
            "bus": _fetch_bus_context(keywords),
            "learnings": _fetch_learnings(keywords),
            "code_graph": _fetch_code_graph(keywords),
            "incidents": _fetch_incidents(keywords),
            "history": _fetch_task_history(keywords, worker=args.worker),
        }

        for name, chunks in sources.items():
            print(f"── {name.upper()} ({len(chunks)} chunks) ──")
            for i, chunk in enumerate(sorted(chunks, key=lambda c: c.score,
                                             reverse=True)[:5]):
                print(f"  [{i+1}] score={chunk.score:.3f} "
                      f"(rel={chunk.relevance:.2f} rec={chunk.recency:.2f}) "
                      f"~{chunk.token_cost}tok")
                preview = chunk.content[:120].replace("\n", " ")
                print(f"      {preview}")
            print()

    elif args.command == "budget":
        budget = TokenBudget(args.max_tokens)
        print(f"Token Budget Allocation (max={args.max_tokens}):")
        print()
        for src_name, alloc in budget.allocated.items():
            pct = alloc / args.max_tokens * 100
            print(f"  {src_name:<12} {alloc:>5} tokens ({pct:.0f}%)")
        print(f"  {'TOTAL':<12} {sum(budget.allocated.values()):>5} tokens")

    elif args.command == "keywords":
        keywords = _extract_keywords(args.task)
        print(f"Extracted {len(keywords)} keywords:")
        for i, kw in enumerate(keywords):
            print(f"  {i+1}. {kw}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: gamma
