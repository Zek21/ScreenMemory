"""
Central Orchestrator — The Main Brain.

Ties together all advanced systems into a unified autonomous orchestration engine:
- DAAO Router → difficulty-aware query routing
- Agent Factory → dynamic agent instantiation
- DAG Engine → runtime workflow generation + execution
- Hybrid Retrieval → RRF-fused memory search
- Input Guard → prompt injection defense
- Process Guardian → lifecycle management

This is the single entry point for all autonomous operations.
Replaces the old agent.py with a more powerful, self-adjusting architecture.

Architecture (from paper):
    Query → InputGuard → DAAORouter → AgentFactory → DAGEngine → Execute
                                         ↕                    ↕
                                   HybridRetrieval      ProcessGuardian
                                         ↕
                                   Memory (4 pillars)
"""
import time
import json
import logging
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Central brain for the multi-agent system.

    Usage:
        brain = Orchestrator()
        result = brain.process("Analyze Bitcoin whale behavior")
        # Automatically routes to correct depth, instantiates agents, executes DAG
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self._init_time = time.time()
        self._init_core_systems(config)
        self._init_cognitive_engines()
        self._history: List[dict] = []
        self._total_queries = 0
        self._total_blocked = 0
        self._sop_registry: Dict[str, dict] = {}
        logger.info("Orchestrator initialized — all systems online")

    def _init_core_systems(self, config: dict):
        """Initialize core routing, agent, DAG, retrieval, and guard systems."""
        from core.difficulty_router import DAAORouter, QueryDifficulty
        from core.agent_factory import AgentFactory, AgentRegistry
        from core.dag_engine import DAGBuilder, DAGExecutor, ExecutionContext
        from core.hybrid_retrieval import HybridRetriever
        from core.input_guard import InputGuard

        self.router = DAAORouter(config.get("backends"))
        self.factory = AgentFactory()
        self.dag_builder = DAGBuilder()
        self.executor = DAGExecutor(
            max_feedback_loops=config.get("max_feedback_loops", 3)
        )
        self.retriever = HybridRetriever()
        self.guard = InputGuard(
            block_threshold=config.get("block_threshold", 0.75),
            warn_threshold=config.get("warn_threshold", 0.40),
        )
        self._memory = None
        self._lance_store = None
        self._guardian = None

    def _init_cognitive_engines(self):
        """Initialize cognitive engines with graceful degradation."""
        self._planner = None
        self._reflexion = None
        self._episodic_memory = None
        try:
            from core.cognitive.planner import HierarchicalPlanner
            self._planner = HierarchicalPlanner()
            logger.info("Cognitive: HierarchicalPlanner connected")
        except Exception as e:
            logger.warning(f"Cognitive: Planner unavailable: {e}")
        try:
            from core.cognitive.reflexion import ReflexionEngine
            self._reflexion = ReflexionEngine()
            logger.info("Cognitive: ReflexionEngine connected")
        except Exception as e:
            logger.warning(f"Cognitive: Reflexion unavailable: {e}")
        try:
            from core.cognitive.memory import EpisodicMemory
            self._episodic_memory = EpisodicMemory()
            if self._reflexion and self._episodic_memory:
                self._reflexion.memory = self._episodic_memory
            logger.info("Cognitive: EpisodicMemory connected")
        except Exception as e:
            logger.warning(f"Cognitive: EpisodicMemory unavailable: {e}")

    def connect_memory(self, memory):
        """Connect the existing EpisodicMemory system."""
        self._memory = memory
        self.retriever.memory = memory
        logger.info("Memory system connected")

    def connect_lance(self, lance_store):
        """Connect LanceDB for vector retrieval."""
        self._lance_store = lance_store
        self.retriever.lance_store = lance_store
        logger.info("LanceDB connected for vector retrieval")

    def connect_guardian(self, guardian):
        """Connect ProcessGuardian for lifecycle management."""
        self._guardian = guardian
        logger.info("ProcessGuardian connected")

    def process(self, query: str, context: dict = None,
                budget: float = 1.0) -> dict:
        """
        Process a query through the full orchestration pipeline.

        Pipeline:
        1. InputGuard scans for injection
        2. DAAO routes to optimal depth
        3. Memory retrieval augments context
        4. AgentFactory creates team
        5. DAGBuilder generates workflow
        6. DAGExecutor runs with retries
        7. Results stored in procedural memory
        """
        t0 = time.perf_counter()
        self._total_queries += 1
        context = context or {}
        result = {
            "query": query, "status": "processing", "pipeline": [],
            "output": None, "errors": [], "metrics": {},
        }

        try:
            safe_query = self._preprocess(query, context, result)
            if result["status"] == "blocked":
                return result
            plan = self._plan_and_retrieve(safe_query, budget, context, result)
            team, exec_ctx, dag = self._execute_dag_pipeline(
                safe_query, plan, context, result
            )
            self._post_process(safe_query, plan, dag, team, exec_ctx, result)
        except Exception as e:
            self._handle_pipeline_error(query, e, result)

        self._finalize_metrics(query, result, t0)
        return result

    def _preprocess(self, query: str, context: dict, result: dict) -> str:
        """Run cognitive planning and security scan; return sanitized query."""
        if self._planner:
            try:
                cognitive_plan = self._planner.create_plan(
                    query, context.get("retrieved_memories", "")
                )
                result["pipeline"].append({
                    "step": "cognitive_plan",
                    "subtask_count": len(cognitive_plan.subtasks),
                    "subtasks": [s.description for s in cognitive_plan.subtasks],
                })
            except Exception as e:
                logger.warning(f"Cognitive planner failed (degraded): {e}")

        scan = self.guard.scan(query)
        result["pipeline"].append({
            "step": "input_guard",
            "threat_level": scan.threat_level.value,
            "score": scan.score,
            "triggers": scan.triggers,
        })

        if scan.blocked:
            self._total_blocked += 1
            result["status"] = "blocked"
            result["errors"].append(f"Input blocked: {scan.triggers}")
            logger.warning(f"Query BLOCKED: {scan.triggers}")

        return scan.sanitized_input

    def _plan_and_retrieve(self, safe_query: str, budget: float,
                           context: dict, result: dict):
        """Route query, augment with memory, and check SOP registry."""
        plan = self.router.route(safe_query, budget)
        result["pipeline"].append({
            "step": "daao_route",
            "difficulty": plan.difficulty.level.name,
            "operator": plan.operator.value,
            "backend": plan.backend,
            "roles": plan.agent_roles,
            "tools": plan.tool_bindings,
            "cost_estimate": plan.cost_estimate,
        })

        if self.retriever.bm25.size > 0 or self._lance_store or self._memory:
            memories = self.retriever.search(safe_query, limit=5)
            if memories:
                memory_context = "\n".join([
                    f"[{m.rrf_score:.3f}] {m.content[:200]}"
                    for m in memories
                ])
                context["retrieved_memories"] = memory_context
                result["pipeline"].append({
                    "step": "memory_retrieval",
                    "results_count": len(memories),
                    "top_score": memories[0].rrf_score if memories else 0,
                })

        sop = self._find_matching_sop(safe_query)
        if sop:
            result["pipeline"].append({
                "step": "sop_match",
                "matched_sop": sop["name"],
            })

        return plan

    def _execute_dag_pipeline(self, safe_query: str, plan, context: dict,
                              result: dict) -> tuple:
        """Create agent team, build DAG, and execute it."""
        team = self.factory.create_team(
            roles=plan.agent_roles,
            backend=plan.backend,
        )
        result["pipeline"].append({
            "step": "agent_creation",
            "team_size": len(team),
            "agents": [a.to_dict() for a in team],
        })

        from core.dag_engine import ExecutionContext
        dag = self.dag_builder.from_workflow_plan(plan)
        exec_context = ExecutionContext(
            query=safe_query,
            shared_state=context,
        )
        result["pipeline"].append({
            "step": "dag_generated",
            "dag_stats": dag.stats,
        })

        exec_context = self.executor.execute(dag, exec_context)
        result["pipeline"].append({
            "step": "dag_executed",
            "final_stats": dag.stats,
            "node_outputs": {
                k: str(v)[:200] for k, v in exec_context.node_outputs.items()
            },
            "errors": exec_context.errors,
        })

        result["output"] = exec_context.node_outputs
        result["status"] = "success" if not exec_context.errors else "partial"
        result["errors"] = exec_context.errors
        return team, exec_context, dag

    def _post_process(self, safe_query: str, plan, dag, team,
                      exec_context, result: dict):
        """Store SOP, record episodic memory, send feedback, and clean up."""
        if result["status"] == "success":
            self._store_sop(safe_query, plan, dag)

        if self._episodic_memory:
            try:
                importance = 0.9 if result["status"] == "success" else 0.6
                difficulty_tag = "unknown"
                for step in result["pipeline"]:
                    if step.get("step") == "daao_route":
                        difficulty_tag = step.get("difficulty", "unknown")
                        break
                self._episodic_memory.store_episodic(
                    content=(
                        f"Query: {safe_query[:100]} | "
                        f"Status: {result['status']} | "
                        f"Steps: {len(result['pipeline'])}"
                    ),
                    tags=["orchestrator", result["status"], difficulty_tag],
                    source_action="orchestrator.process",
                    importance=importance,
                )
            except Exception as mem_err:
                logger.warning(f"Episodic memory store failed: {mem_err}")

        from core.difficulty_router import QueryDifficulty
        actual_difficulty = plan.difficulty.level
        self.router.feedback(safe_query, actual_difficulty,
                             result["status"] == "success")
        self.factory.destroy_team(team)

    def _handle_pipeline_error(self, query: str, error: Exception,
                               result: dict):
        """Handle pipeline errors and run cognitive reflexion."""
        result["status"] = "error"
        result["errors"].append(str(error))
        logger.error(f"Orchestrator error: {error}", exc_info=True)

        if self._reflexion:
            try:
                from core.cognitive.reflexion import FailureContext
                failure = FailureContext(
                    action_type="orchestrate",
                    action_target=query[:100],
                    error_message=str(error),
                    error_type=type(error).__name__,
                    expected_outcome="successful pipeline execution",
                    actual_outcome=f"error: {str(error)[:200]}",
                )
                reflection = self._reflexion.on_failure(failure)
                result["pipeline"].append({
                    "step": "reflexion",
                    "critique": reflection.critique[:200],
                    "lesson": reflection.lesson[:200],
                    "adjustment": reflection.action_adjustment[:200],
                })
                logger.info(f"Reflexion: {reflection.lesson[:100]}")
            except Exception as ref_err:
                logger.warning(f"Reflexion engine failed: {ref_err}")

    def _finalize_metrics(self, query: str, result: dict, t0: float):
        """Record timing metrics and append to execution history."""
        elapsed = (time.perf_counter() - t0) * 1000
        result["metrics"] = {
            "total_ms": round(elapsed, 1),
            "pipeline_steps": len(result["pipeline"]),
        }
        self._history.append({
            "query": query[:100],
            "status": result["status"],
            "difficulty": next(
                (p.get("difficulty", "unknown")
                 for p in result["pipeline"]
                 if p.get("step") == "daao_route"),
                "unknown"
            ),
            "elapsed_ms": elapsed,
            "timestamp": time.time(),
        })
        logger.info(
            f"Orchestrator: {result['status']} in {elapsed:.0f}ms "
            f"({len(result['pipeline'])} steps)"
        )

    def _find_matching_sop(self, query: str) -> Optional[dict]:
        """MASFly-style SOP matching — find stored successful patterns."""
        if not self._sop_registry:
            return None

        query_lower = query.lower()
        best_match = None
        best_score = 0

        for sop_id, sop in self._sop_registry.items():
            # Simple keyword overlap scoring
            sop_words = set(sop.get("keywords", []))
            query_words = set(query_lower.split())
            overlap = len(sop_words & query_words)
            if overlap > best_score:
                best_score = overlap
                best_match = sop

        return best_match if best_score >= 3 else None

    def _store_sop(self, query: str, plan, dag):
        """Store successful workflow as SOP for future reuse."""
        import hashlib
        sop_id = hashlib.md5(query.lower().encode()).hexdigest()[:12]

        keywords = [w.lower() for w in query.split() if len(w) > 3]

        self._sop_registry[sop_id] = {
            "name": query[:60],
            "keywords": keywords,
            "operator": plan.operator.value,
            "roles": plan.agent_roles,
            "tools": plan.tool_bindings,
            "dag_topology": dag.stats,
            "timestamp": time.time(),
        }

    def index_knowledge(self, doc_id: str, content: str, metadata: dict = None):
        """Add knowledge to the hybrid retrieval index."""
        self.retriever.index_document(doc_id, content, metadata)

    @property
    def stats(self) -> dict:
        s = {
            "uptime_seconds": round(time.time() - self._init_time, 1),
            "total_queries": self._total_queries,
            "total_blocked": self._total_blocked,
            "sop_registry_size": len(self._sop_registry),
            "router_stats": self.router.stats,
            "factory_stats": self.factory.stats,
            "guard_stats": self.guard.stats,
            "retriever_stats": self.retriever.stats,
            "cognitive": {
                "planner": "online" if self._planner else "offline",
                "reflexion": self._reflexion.stats if self._reflexion else "offline",
                "episodic_memory": self._episodic_memory.get_stats() if self._episodic_memory else "offline",
            },
        }
        return s

    def status_report(self) -> str:
        """Human-readable status report."""
        s = self.stats
        return (
            f"=== Orchestrator Status ===\n"
            f"Uptime: {s['uptime_seconds']:.0f}s\n"
            f"Queries: {s['total_queries']} ({s['total_blocked']} blocked)\n"
            f"SOPs stored: {s['sop_registry_size']}\n"
            f"Active agents: {s['factory_stats']['active']}\n"
            f"BM25 documents: {s['retriever_stats']['bm25_documents']}\n"
            f"Router: {s['router_stats']}\n"
            f"Guard: {s['guard_stats']}\n"
        )
