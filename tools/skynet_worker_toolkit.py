#!/usr/bin/env python3
"""
skynet_worker_toolkit.py -- Unified intelligence toolkit for Skynet workers.

Gives every worker the SAME technology as the orchestrator: bus access, knowledge
retrieval, learning store, hybrid search, self-awareness, reporting, and dispatch.

Usage (in any worker):
    from tools.skynet_worker_toolkit import WorkerToolkit
    tk = WorkerToolkit("delta")

    # Query knowledge
    facts = tk.recall("dispatch race condition")
    context = tk.get_context("build a REST API")

    # Search everything
    results = tk.search("authentication flow")

    # Learn and share
    tk.learn("File locks needed for cross-process coordination", category="infrastructure")

    # Check system state
    status = tk.system_status()
    pulse = tk.pulse()

    # Check my pending work
    work = tk.my_work()

    # Save artifacts
    tk.report(topic="audit_fix", task="Fixed auth bug", approach="Replaced JWT validation")
    tk.artifact(doc_type="diagnosis", slug="auth_bug", content="# Root Cause\n...")

    # Post to bus
    tk.bus_post("orchestrator", "result", "TASK_COMPLETE: fixed auth bug")
    tk.bus_read(limit=20)

CLI:
    python tools/skynet_worker_toolkit.py --worker delta status
    python tools/skynet_worker_toolkit.py --worker delta search "API endpoints"
    python tools/skynet_worker_toolkit.py --worker delta recall "dispatch"
    python tools/skynet_worker_toolkit.py --worker delta work
    python tools/skynet_worker_toolkit.py --worker delta learn "fact text" --category code
"""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

BUS_URL = "http://localhost:8420"


def _http_get(url: str, timeout: int = 5) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _http_post(url: str, body: dict, timeout: int = 5) -> Any:
    try:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


class WorkerToolkit:
    """Unified Skynet intelligence toolkit -- gives workers full orchestrator-grade access."""

    def __init__(self, worker_name: str):
        self.name = worker_name
        self._learning_store = None
        self._retriever = None
        self._skynet_self = None
        self._db = None

    # ── Bus Communication ──────────────────────────────────────────────────

    def bus_post(self, topic: str, msg_type: str, content: str) -> bool:
        """Post a message to the Skynet bus via SpamGuard."""
        msg = {"sender": self.name, "topic": topic, "type": msg_type, "content": content}
        try:
            from tools.skynet_spam_guard import guarded_publish
            result = guarded_publish(msg)
            return bool(result and result.get("allowed", False))
        except ImportError:
            return _http_post(f"{BUS_URL}/bus/publish", msg) is not None
        # signed: gamma

    def bus_read(self, limit: int = 20, topic: str = None) -> List[dict]:
        """Read messages from the bus."""
        url = f"{BUS_URL}/bus/messages?limit={limit}"
        if topic:
            url += f"&topic={topic}"
        return _http_get(url) or []

    def bus_read_for_me(self, limit: int = 20) -> List[dict]:
        """Read bus messages addressed to this worker."""
        msgs = self.bus_read(limit=limit)
        return [m for m in msgs if isinstance(m, dict) and (
            m.get("topic") == self.name or
            m.get("topic") == f"worker_{self.name}" or
            self.name in str(m.get("content", "")).lower()
        )]

    # ── Knowledge & Learning ───────────────────────────────────────────────

    @property
    def learning_store(self):
        if self._learning_store is None:
            from core.learning_store import LearningStore
            self._learning_store = LearningStore()
        return self._learning_store

    def recall(self, query: str, top_k: int = 5) -> List[dict]:
        """Search knowledge base for relevant facts."""
        try:
            facts = self.learning_store.recall(query, top_k=top_k)
            return [{"content": f.content, "confidence": f.confidence,
                      "category": f.category, "source": f.source} for f in facts]
        except Exception as e:
            return [{"error": str(e)}]

    def recall_by_category(self, category: str, top_k: int = 10) -> List[dict]:
        """Recall facts by category."""
        try:
            facts = self.learning_store.recall_by_category(category, top_k=top_k)
            return [{"content": f.content, "confidence": f.confidence} for f in facts]
        except Exception:
            return []

    def learn(self, content: str, category: str = "worker_learning",
              tags: List[str] = None) -> str:
        """Store a learned fact and broadcast to peers."""
        fact_id = self.learning_store.learn(
            content=content, category=category,
            source=self.name, tags=tags or [self.name]
        )
        try:
            from skynet_knowledge import broadcast_learning
            broadcast_learning(self.name, content, category, tags)
        except Exception:
            pass
        return fact_id

    def absorb_peer_learnings(self) -> int:
        """Absorb learnings broadcast by other workers."""
        try:
            from skynet_knowledge import absorb_learnings
            return absorb_learnings(self.name)
        except Exception:
            return 0

    def get_context(self, task_description: str, top_k: int = 5) -> str:
        """Get relevant context for a task (learnings + past solutions)."""
        try:
            from core.learning_store import PersistentLearningSystem
            pls = PersistentLearningSystem()
            return pls.get_context_for_task(task_description, top_k=top_k)
        except Exception:
            # Fallback to basic recall
            facts = self.recall(task_description, top_k=top_k)
            if facts and "error" not in facts[0]:
                return "\n".join(f"- {f['content']}" for f in facts)
            return ""

    def knowledge_stats(self) -> dict:
        """Get knowledge system statistics."""
        try:
            return self.learning_store.stats()
        except Exception as e:
            return {"error": str(e)}

    # ── Hybrid Search ──────────────────────────────────────────────────────

    @property
    def retriever(self):
        if self._retriever is None:
            from core.hybrid_retrieval import HybridRetriever
            self._retriever = HybridRetriever()
        return self._retriever

    def search(self, query: str, limit: int = 10) -> List[dict]:
        """Hybrid search across vector + BM25 + knowledge graph."""
        try:
            results = self.retriever.search(query, limit=limit)
            return [{"doc_id": r.doc_id, "score": round(r.score, 4),
                      "content": r.content[:200]} for r in results]
        except Exception as e:
            return [{"error": str(e)}]

    # ── Screen Memory Database ─────────────────────────────────────────────

    @property
    def db(self):
        if self._db is None:
            from core.database import ScreenMemoryDB
            self._db = ScreenMemoryDB()
        return self._db

    def search_screens(self, query: str, limit: int = 20) -> List[dict]:
        """Search captured screen history."""
        try:
            return self.db.search_text(query, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    def recent_screens(self, limit: int = 10) -> List[dict]:
        """Get most recent screen captures."""
        try:
            return self.db.get_recent(limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    # ── System Awareness ───────────────────────────────────────────────────

    @property
    def skynet_self(self):
        if self._skynet_self is None:
            from skynet_self import SkynetSelf
            self._skynet_self = SkynetSelf()
        return self._skynet_self

    def system_status(self) -> dict:
        """Get full Skynet system status (backend + workers + engines)."""
        return _http_get(f"{BUS_URL}/status") or {"error": "backend unreachable"}

    def pulse(self) -> dict:
        """Quick system health pulse."""
        try:
            return self.skynet_self.quick_pulse()
        except Exception as e:
            return {"error": str(e)}

    def worker_states(self) -> Dict[str, str]:
        """Get all worker states (IDLE/PROCESSING/DEAD)."""
        status = self.system_status()
        agents = status.get("agents", {})
        return {name: data.get("status", "UNKNOWN") if isinstance(data, dict)
                else str(data) for name, data in agents.items()}

    def engine_status(self) -> dict:
        """Get engine health from GOD Console."""
        return _http_get("http://localhost:8421/engines") or {"error": "GOD Console unreachable"}

    # ── Task & Work Management ─────────────────────────────────────────────

    def my_work(self) -> dict:
        """Check what pending work I have."""
        try:
            from skynet_worker_poll import poll_for_work
            return poll_for_work(self.name)
        except Exception as e:
            return {"has_work": False, "error": str(e)}

    def my_todos(self) -> List[dict]:
        """Get my pending TODOs."""
        try:
            todos_file = ROOT / "data" / "todos.json"
            if todos_file.exists():
                data = json.loads(todos_file.read_text(encoding="utf-8"))
                all_todos = data.get("todos", []) if isinstance(data, dict) else []
                return [t for t in all_todos if t.get("worker") == self.name
                        and t.get("status") in ("pending", "active")]
            return []
        except Exception:
            return []

    # ── Reporting & Artifacts ──────────────────────────────────────────────

    def report(self, topic: str, task: str, **kwargs) -> str:
        """Write a comprehensive report and post to bus."""
        from skynet_report import post_report
        return post_report(worker=self.name, topic=topic, task=task, **kwargs)

    def artifact(self, doc_type: str, slug: str, content: str,
                 summary: str = "", **kwargs) -> str:
        """Save a typed artifact (report/diagnosis/roadmap/proposal/audit)."""
        from skynet_report import save_artifact
        return save_artifact(worker=self.name, doc_type=doc_type, slug=slug,
                             content=content, summary=summary, **kwargs)

    # ── Cognitive Engines ──────────────────────────────────────────────────

    def plan(self, goal: str) -> dict:
        """Use the cognitive planner to decompose a goal."""
        try:
            from core.cognitive.planner import HierarchicalPlanner
            planner = HierarchicalPlanner()
            return planner.plan(goal)
        except Exception as e:
            return {"error": str(e)}

    def reflect(self, query: str, error: str, attempt: int = 1) -> dict:
        """Use reflexion engine to analyze a failure."""
        try:
            from core.cognitive.reflexion import ReflexionEngine
            engine = ReflexionEngine()
            return engine.reflect(query, error, attempt)
        except Exception as e:
            return {"error": str(e)}

    # ── Collective Intelligence ────────────────────────────────────────────

    def propose_improvement(self, title: str, description: str,
                            target_files: List[str] = None, priority: str = "normal") -> bool:
        """Propose a system improvement for peer review."""
        try:
            from skynet_knowledge import propose_improvement
            return propose_improvement(self.name, title, description, target_files, priority)
        except Exception:
            return self.bus_post("planning", "proposal",
                                json.dumps({"title": title, "description": description,
                                            "worker": self.name, "priority": priority}))

    def request_convene(self, topic: str, question: str) -> bool:
        """Request a multi-worker convene session."""
        return self.bus_post("convene", "request",
                             json.dumps({"topic": topic, "question": question,
                                         "initiator": self.name}))

    # ── Utility ────────────────────────────────────────────────────────────

    def result(self, content: str) -> bool:
        """Post a task result to the orchestrator (standard completion message)."""
        return self.bus_post("orchestrator", "result", content)

    def alert(self, content: str) -> bool:
        """Post an alert to the orchestrator."""
        return self.bus_post("orchestrator", "alert", content)

    def summary_table(self) -> str:
        """Print a summary of available capabilities."""
        caps = [
            ("Bus", "bus_post, bus_read, bus_read_for_me, result, alert"),
            ("Knowledge", "recall, learn, absorb_peer_learnings, get_context, knowledge_stats"),
            ("Search", "search (hybrid), search_screens, recent_screens"),
            ("Awareness", "system_status, pulse, worker_states, engine_status"),
            ("Tasks", "my_work, my_todos"),
            ("Reporting", "report, artifact"),
            ("Cognition", "plan, reflect"),
            ("Collective", "propose_improvement, request_convene"),
        ]
        lines = [f"WorkerToolkit({self.name}) -- {len(caps)} capability groups:\n"]
        for group, methods in caps:
            lines.append(f"  {group:12s}: {methods}")
        return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Skynet Worker Toolkit CLI")
    parser.add_argument("--worker", "-w", required=True, help="Worker name")
    parser.add_argument("command", choices=["status", "search", "recall", "work",
                                            "todos", "learn", "engines", "capabilities"])
    parser.add_argument("query", nargs="?", default="", help="Query or fact text")
    parser.add_argument("--category", default="worker_learning", help="Learning category")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    tk = WorkerToolkit(args.worker)

    if args.command == "status":
        status = tk.system_status()
        print(json.dumps(status, indent=2, default=str))
    elif args.command == "search":
        results = tk.search(args.query, limit=args.limit)
        for r in results:
            print(f"  [{r.get('score', '?')}] {r.get('doc_id', '?')}: {r.get('content', '')[:100]}")
    elif args.command == "recall":
        facts = tk.recall(args.query, top_k=args.limit)
        for f in facts:
            print(f"  [{f.get('confidence', '?')}] {f.get('content', '')[:120]}")
    elif args.command == "work":
        work = tk.my_work()
        print(json.dumps(work, indent=2, default=str))
    elif args.command == "todos":
        todos = tk.my_todos()
        for t in todos:
            print(f"  [{t.get('status')}] {t.get('task', '?')[:100]}")
        if not todos:
            print("  No pending TODOs")
    elif args.command == "learn":
        if not args.query:
            print("Usage: --worker NAME learn 'fact text' --category code")
            return
        fid = tk.learn(args.query, category=args.category)
        print(f"  Learned: {fid}")
    elif args.command == "engines":
        engines = tk.engine_status()
        print(json.dumps(engines, indent=2, default=str))
    elif args.command == "capabilities":
        print(tk.summary_table())


if __name__ == "__main__":
    main()
