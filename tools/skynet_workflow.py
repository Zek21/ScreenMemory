#!/usr/bin/env python3
"""Skynet Distributed Workflow Engine (P2.09).

DAG-based task orchestration that dispatches workflow nodes to Skynet
workers, waits for results, evaluates conditional edges, and retries
failed nodes.  Unlike ``core/dag_engine.py`` (local, in-process), this
engine distributes work across the live worker swarm.

Workflow state is persisted to ``data/workflows/{id}.json`` for crash
recovery.

Built-in workflows:
    plan-code-test-deploy  — 4-stage CI/CD pipeline
    audit-fix-verify       — 3-stage remediation pipeline

Usage:
    python tools/skynet_workflow.py run --workflow plan-code-test-deploy --goal "Add caching"
    python tools/skynet_workflow.py run --nodes nodes.json
    python tools/skynet_workflow.py status WORKFLOW_ID
    python tools/skynet_workflow.py list
    python tools/skynet_workflow.py resume WORKFLOW_ID
    python tools/skynet_workflow.py builtin

Python API:
    from tools.skynet_workflow import DAGEngine, WorkflowNode, WorkflowEdge
    engine = DAGEngine()
    engine.add_node(WorkflowNode("plan", "Design the feature"))
    engine.add_node(WorkflowNode("code", "Implement it"))
    engine.add_edge(WorkflowEdge("plan", "code"))
    results = engine.execute()
"""
# signed: beta

import json
import os
import time
import hashlib
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
WORKFLOWS_DIR = DATA_DIR / "workflows"

# ── Constants ───────────────────────────────────────────────────────
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]
DEFAULT_MAX_RETRIES = 3
DISPATCH_COOLDOWN_S = 2.0
RESULT_POLL_TIMEOUT_S = 120.0


class NodeStatus(str, Enum):
    """Lifecycle states for a workflow node."""
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"
    # signed: beta


# ── Data classes ────────────────────────────────────────────────────

@dataclass
class WorkflowNode:
    """A single unit of work in a workflow DAG.

    Attributes:
        id: Unique node identifier.
        task: Task description dispatched to the worker.
        dependencies: List of node ids that must complete first.
        status: Current lifecycle status.
        retries: Number of retries attempted so far.
        max_retries: Maximum retry attempts before marking FAILED.
        worker: Assigned worker name (auto-assigned if None).
        result: Worker result content (populated on completion).
        error: Error message if failed.
        started_at: ISO timestamp when dispatch occurred.
        completed_at: ISO timestamp when result received.
        dispatch_key: Bus key used for result matching.
    """
    id: str
    task: str
    dependencies: list[str] = field(default_factory=list)
    status: str = NodeStatus.PENDING.value
    retries: int = 0
    max_retries: int = DEFAULT_MAX_RETRIES
    worker: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    dispatch_key: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowNode":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})
    # signed: beta


@dataclass
class WorkflowEdge:
    """A directed edge between two workflow nodes.

    Attributes:
        source: Source node id (must complete before target runs).
        target: Target node id.
        condition: Optional callable ``(source_result: str) -> bool``.
                   If provided, the target only runs when the condition
                   returns True on the source node's result.  Edges
                   without a condition are unconditional.
        condition_expr: Serialisable description of the condition
                        (for persistence; lambdas are not serialisable).
    """
    source: str
    target: str
    condition: Optional[Callable[[str], bool]] = field(default=None, repr=False)
    condition_expr: Optional[str] = None

    def __post_init__(self):  # signed: beta
        """Auto-rebuild condition callable from condition_expr if not provided."""
        if self.condition is None and self.condition_expr:
            self.condition = _rebuild_condition(self.condition_expr)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "condition_expr": self.condition_expr,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkflowEdge":
        edge = cls(source=d["source"], target=d["target"],
                   condition_expr=d.get("condition_expr"))
        # Rebuild simple condition expressions
        expr = d.get("condition_expr")
        if expr:
            edge.condition = _rebuild_condition(expr)
        return edge
    # signed: beta


def _rebuild_condition(expr: str) -> Optional[Callable[[str], bool]]:
    """Rebuild a condition callable from a serialised expression.

    Supports:
        "contains:KEYWORD"  — result must contain KEYWORD (case-insensitive)
        "not_contains:KEYWORD" — result must NOT contain KEYWORD
        "pass"               — result must contain PASS
    """
    if not expr:
        return None
    low = expr.lower().strip()
    if low.startswith("contains:"):
        keyword = expr.split(":", 1)[1].strip()
        return lambda r, kw=keyword: kw.lower() in (r or "").lower()
    if low.startswith("not_contains:"):
        keyword = expr.split(":", 1)[1].strip()
        return lambda r, kw=keyword: kw.lower() not in (r or "").lower()
    if low == "pass":
        return lambda r: "pass" in (r or "").lower()
    return None


# ── Persistence ─────────────────────────────────────────────────────

def _workflow_path(workflow_id: str) -> Path:
    return WORKFLOWS_DIR / f"{workflow_id}.json"


def _save_workflow(workflow_id: str, data: dict) -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _workflow_path(workflow_id).with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(str(tmp), str(_workflow_path(workflow_id)))


def _load_workflow(workflow_id: str) -> Optional[dict]:
    p = _workflow_path(workflow_id)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _list_workflows() -> list[dict]:
    """List all persisted workflows with summary info."""
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for p in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "id": data.get("id", p.stem),
                "name": data.get("name", ""),
                "status": data.get("status", "unknown"),
                "nodes": len(data.get("nodes", [])),
                "created_at": data.get("created_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return results


# ── Bus helpers ─────────────────────────────────────────────────────

def _bus_publish(sender: str, msg_type: str, content: str,
                 metadata: dict | None = None) -> bool:
    try:
        from tools.skynet_spam_guard import guarded_publish
        payload = {
            "sender": sender,
            "topic": "workflow",
            "type": msg_type,
            "content": content,
        }
        if metadata:
            payload["metadata"] = metadata
        return guarded_publish(payload)
    except Exception:
        return False


def _wait_for_result(key: str, timeout: float = RESULT_POLL_TIMEOUT_S) -> Optional[dict]:
    """Wait for a bus result matching key using orch_realtime."""
    try:
        from tools.orch_realtime import wait
        return wait(key, timeout=timeout)
    except Exception:
        return None


def _get_idle_workers() -> list[str]:
    """Return names of workers currently in IDLE state."""
    try:
        rt_path = DATA_DIR / "realtime.json"
        if rt_path.exists():
            with open(rt_path, "r", encoding="utf-8") as f:
                rt = json.load(f)
            workers = rt.get("workers", {})
            return [
                name for name in WORKER_NAMES
                if workers.get(name, {}).get("status", "").upper() == "IDLE"
            ]
    except (json.JSONDecodeError, OSError):
        pass
    return list(WORKER_NAMES)  # assume all available if can't check


# ── DAG Engine ──────────────────────────────────────────────────────

class DAGEngine:
    """Distributed DAG workflow engine for Skynet.

    Builds a directed acyclic graph of workflow nodes, dispatches ready
    nodes to workers in parallel, waits for results, evaluates edge
    conditions, and retries failures.

    Example::

        engine = DAGEngine("my-pipeline")
        engine.add_node(WorkflowNode("plan", "Design the feature"))
        engine.add_node(WorkflowNode("code", "Implement it", dependencies=["plan"]))
        engine.add_edge(WorkflowEdge("plan", "code"))
        results = engine.execute()
    """

    def __init__(self, name: str = "workflow", workflow_id: str | None = None):
        self.name = name
        self.id = workflow_id or self._gen_id(name)
        self.nodes: dict[str, WorkflowNode] = {}
        self.edges: list[WorkflowEdge] = []
        self.status = "created"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: str | None = None
        self._worker_round_robin = 0
        self._lock = threading.Lock()
    # signed: beta

    @staticmethod
    def _gen_id(name: str) -> str:
        digest = hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:8]
        return f"wf_{digest}"

    # ── Graph construction ──────────────────────────────────────────

    def add_node(self, node: WorkflowNode) -> "DAGEngine":
        """Add a node to the workflow graph."""
        self.nodes[node.id] = node
        return self

    def add_edge(self, edge: WorkflowEdge) -> "DAGEngine":
        """Add a directed edge between two nodes.

        Also updates the target node's dependency list if not already
        present.
        """
        self.edges.append(edge)
        target = self.nodes.get(edge.target)
        if target and edge.source not in target.dependencies:
            target.dependencies.append(edge.source)
        return self

    def validate(self) -> list[str]:
        """Validate the DAG structure. Returns list of error strings."""
        errors = []
        node_ids = set(self.nodes.keys())

        # Check edge references
        for edge in self.edges:
            if edge.source not in node_ids:
                errors.append(f"Edge source '{edge.source}' not in nodes")
            if edge.target not in node_ids:
                errors.append(f"Edge target '{edge.target}' not in nodes")

        # Check dependency references
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep not in node_ids:
                    errors.append(f"Node '{node.id}' depends on unknown '{dep}'")

        # Cycle detection via topological sort
        try:
            self._topological_sort()
        except ValueError as e:
            errors.append(str(e))

        return errors

    def _topological_sort(self) -> list[str]:
        """Kahn's algorithm for topological ordering."""
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for edge in self.edges:
            if edge.target in in_degree:
                in_degree[edge.target] += 1

        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        order = []

        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for edge in self.edges:
                if edge.source == nid and edge.target in in_degree:
                    in_degree[edge.target] -= 1
                    if in_degree[edge.target] == 0:
                        queue.append(edge.target)

        if len(order) != len(self.nodes):
            raise ValueError("Cycle detected in workflow DAG")
        return order

    # ── Ready-node detection ────────────────────────────────────────

    def get_ready_nodes(self) -> list[WorkflowNode]:
        """Return nodes whose dependencies are all satisfied.

        A node is ready when:
          - Its status is PENDING or RETRYING
          - All dependency nodes have status COMPLETED
          - All conditional edges from dependencies evaluate to True
        """
        ready = []
        for node in self.nodes.values():
            if node.status not in (NodeStatus.PENDING.value,
                                   NodeStatus.RETRYING.value):
                continue

            deps_met = True
            for dep_id in node.dependencies:
                dep = self.nodes.get(dep_id)
                if not dep or dep.status != NodeStatus.COMPLETED.value:
                    deps_met = False
                    break

            if not deps_met:
                continue

            # Evaluate conditional edges
            cond_ok = True
            for edge in self.edges:
                if edge.target == node.id and edge.condition:
                    src = self.nodes.get(edge.source)
                    src_result = src.result if src else None
                    try:
                        if not edge.condition(src_result or ""):
                            cond_ok = False
                            break
                    except Exception:
                        cond_ok = False
                        break

            if cond_ok:
                ready.append(node)

        return ready

    # ── Worker assignment ───────────────────────────────────────────

    def _assign_worker(self, node: WorkflowNode) -> str:
        """Assign a worker to a node via round-robin over idle workers."""
        if node.worker:
            return node.worker

        idle = _get_idle_workers()
        pool = idle if idle else WORKER_NAMES

        worker = pool[self._worker_round_robin % len(pool)]
        self._worker_round_robin += 1
        node.worker = worker
        return worker

    # ── Dispatch + wait ─────────────────────────────────────────────

    def _dispatch_node(self, node: WorkflowNode) -> bool:
        """Dispatch a single node to its assigned worker."""
        worker = self._assign_worker(node)
        dispatch_key = f"WF-{self.id}-{node.id}"
        node.dispatch_key = dispatch_key
        node.status = NodeStatus.DISPATCHED.value
        node.started_at = datetime.now(timezone.utc).isoformat()

        task_text = (
            f"WORKFLOW {self.id} — Node '{node.id}'\n"
            f"Task: {node.task}\n"
            f"When done, include '{dispatch_key}' in your bus result content.\n"
            f"signed:{{your_name}}"
        )

        try:
            from tools.skynet_dispatch import dispatch_to_worker
            ok = dispatch_to_worker(worker, task_text)
        except ImportError:
            ok = False

        if ok:
            node.status = NodeStatus.RUNNING.value
            _bus_publish(
                sender="workflow_engine",
                msg_type="node_dispatched",
                content=f"[{self.id}] Node '{node.id}' dispatched to {worker}",
                metadata={"workflow_id": self.id, "node_id": node.id,
                           "worker": worker},
            )
        else:
            node.error = f"Dispatch to {worker} failed"
            node.status = NodeStatus.FAILED.value

        self._persist()
        return ok

    def _wait_node(self, node: WorkflowNode,
                   timeout: float = RESULT_POLL_TIMEOUT_S) -> bool:
        """Wait for a dispatched node's result on the bus."""
        if not node.dispatch_key:
            return False

        result = _wait_for_result(node.dispatch_key, timeout=timeout)
        if result:
            node.result = result.get("content", "")
            node.status = NodeStatus.COMPLETED.value
            node.completed_at = datetime.now(timezone.utc).isoformat()
            self._persist()
            return True

        # Timeout — mark for retry or fail
        node.retries += 1
        if node.retries < node.max_retries:
            node.status = NodeStatus.RETRYING.value
            node.error = f"Timeout after {timeout}s (attempt {node.retries}/{node.max_retries})"
        else:
            node.status = NodeStatus.FAILED.value
            node.error = f"Failed after {node.max_retries} attempts"

        self._persist()
        return False

    # ── Condition-based skipping ────────────────────────────────────

    def _skip_downstream(self, node_id: str) -> None:
        """Skip all nodes that depend on a failed/skipped node."""
        for edge in self.edges:
            if edge.source == node_id:
                target = self.nodes.get(edge.target)
                if target and target.status == NodeStatus.PENDING.value:
                    target.status = NodeStatus.SKIPPED.value
                    target.error = f"Skipped: dependency '{node_id}' not satisfied"
                    self._skip_downstream(target.id)

    # ── Main execution loop ─────────────────────────────────────────

    def execute(self, timeout_per_node: float = RESULT_POLL_TIMEOUT_S,
                dry_run: bool = False) -> dict:
        """Execute the entire workflow DAG.

        Finds ready nodes, dispatches them in parallel batches, waits
        for completion, evaluates condition edges, and retries failures.

        Args:
            timeout_per_node: Max seconds to wait for each node's result.
            dry_run: If True, print execution plan without dispatching.

        Returns:
            Summary dict with status, node results, and timing.
        """
        errors = self.validate()
        if errors:
            return {"status": "invalid", "errors": errors}

        if dry_run:
            return self._dry_run()

        self.status = "running"
        self._persist()
        t0 = time.time()

        _bus_publish(
            sender="workflow_engine",
            msg_type="workflow_started",
            content=f"Workflow {self.id} ({self.name}) started with {len(self.nodes)} nodes",
            metadata={"workflow_id": self.id, "name": self.name},
        )

        max_iterations = len(self.nodes) * (DEFAULT_MAX_RETRIES + 1)
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            ready = self.get_ready_nodes()

            if not ready:
                # No more ready nodes — check if we're done or stuck
                if self._all_terminal():
                    break
                # Some nodes are still RUNNING/DISPATCHED — wait briefly
                still_active = [
                    n for n in self.nodes.values()
                    if n.status in (NodeStatus.RUNNING.value,
                                    NodeStatus.DISPATCHED.value)
                ]
                if not still_active:
                    break  # no active, no ready — deadlocked or done
                time.sleep(2.0)
                continue

            # Dispatch all ready nodes in parallel
            dispatched = []
            for node in ready:
                ok = self._dispatch_node(node)
                if ok:
                    dispatched.append(node)
                else:
                    self._skip_downstream(node.id)
                time.sleep(DISPATCH_COOLDOWN_S)

            # Wait for all dispatched nodes
            for node in dispatched:
                success = self._wait_node(node, timeout=timeout_per_node)
                if not success:
                    if node.status == NodeStatus.FAILED.value:
                        self._skip_downstream(node.id)
                    # RETRYING nodes will be picked up in next iteration

        # Determine final status
        elapsed = time.time() - t0
        failed = [n for n in self.nodes.values()
                  if n.status == NodeStatus.FAILED.value]
        skipped = [n for n in self.nodes.values()
                   if n.status == NodeStatus.SKIPPED.value]
        completed = [n for n in self.nodes.values()
                     if n.status == NodeStatus.COMPLETED.value]

        if failed:
            self.status = "failed"
        elif len(completed) == len(self.nodes):
            self.status = "completed"
        elif skipped:
            self.status = "partial"
        else:
            self.status = "completed"

        self.completed_at = datetime.now(timezone.utc).isoformat()
        self._persist()

        summary = {
            "workflow_id": self.id,
            "name": self.name,
            "status": self.status,
            "elapsed_s": round(elapsed, 1),
            "total_nodes": len(self.nodes),
            "completed": len(completed),
            "failed": len(failed),
            "skipped": len(skipped),
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
        }

        _bus_publish(
            sender="workflow_engine",
            msg_type="workflow_completed",
            content=(f"Workflow {self.id} {self.status}: "
                     f"{len(completed)}/{len(self.nodes)} nodes completed "
                     f"in {elapsed:.1f}s"),
            metadata={"workflow_id": self.id, "status": self.status},
        )

        return summary

    def _all_terminal(self) -> bool:
        """Check if all nodes are in a terminal state."""
        terminal = {NodeStatus.COMPLETED.value, NodeStatus.FAILED.value,
                    NodeStatus.SKIPPED.value}
        return all(n.status in terminal for n in self.nodes.values())

    def _dry_run(self) -> dict:
        """Print execution plan without dispatching."""
        order = self._topological_sort()
        plan = []
        for i, nid in enumerate(order, 1):
            node = self.nodes[nid]
            deps = ", ".join(node.dependencies) if node.dependencies else "none"
            plan.append({
                "step": i,
                "node_id": nid,
                "task": node.task[:120],
                "dependencies": deps,
                "worker": node.worker or "auto-assign",
            })
        return {"status": "dry_run", "workflow_id": self.id,
                "name": self.name, "plan": plan}

    # ── Persistence ─────────────────────────────────────────────────

    def _persist(self) -> None:
        """Save workflow state for crash recovery."""
        _save_workflow(self.id, self.to_dict())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DAGEngine":
        """Reconstruct a DAGEngine from persisted state."""
        engine = cls(name=d.get("name", "workflow"),
                     workflow_id=d.get("id"))
        engine.status = d.get("status", "created")
        engine.created_at = d.get("created_at", "")
        engine.completed_at = d.get("completed_at")

        for nd in d.get("nodes", []):
            engine.add_node(WorkflowNode.from_dict(nd))
        for ed in d.get("edges", []):
            edge = WorkflowEdge.from_dict(ed)
            engine.edges.append(edge)
            # Don't call add_edge (avoids re-adding deps)
            target = engine.nodes.get(edge.target)
            if target and edge.source not in target.dependencies:
                target.dependencies.append(edge.source)

        return engine

    def resume(self, timeout_per_node: float = RESULT_POLL_TIMEOUT_S) -> dict:
        """Resume a crashed/incomplete workflow.

        Resets DISPATCHED/RUNNING nodes back to PENDING (they were
        in-flight when the crash occurred), then calls execute().
        """
        for node in self.nodes.values():
            if node.status in (NodeStatus.DISPATCHED.value,
                               NodeStatus.RUNNING.value):
                node.status = NodeStatus.PENDING.value
                node.error = None
        self.status = "running"
        self._persist()
        return self.execute(timeout_per_node=timeout_per_node)
    # signed: beta


# ── Built-in workflows ──────────────────────────────────────────────

def builtin_plan_code_test_deploy(goal: str,
                                  workers: list[str] | None = None) -> DAGEngine:
    """4-stage CI/CD pipeline: plan → code → test → deploy.

    Stage 1 (plan): Design architecture and implementation plan.
    Stage 2 (code): Implement the plan. Depends on plan completing.
    Stage 3 (test): Run tests and validation. Depends on code completing.
                    Conditional: only proceeds if code result contains no errors.
    Stage 4 (deploy): Final integration. Depends on test passing.
    """
    pool = workers or WORKER_NAMES
    engine = DAGEngine(name="plan-code-test-deploy")

    engine.add_node(WorkflowNode(
        id="plan",
        task=f"PLAN: Design the architecture and implementation steps for: {goal}",
        worker=pool[0 % len(pool)],
    ))
    engine.add_node(WorkflowNode(
        id="code",
        task=f"CODE: Implement the plan from the previous stage for: {goal}. "
             f"Read the plan from the bus (key=WF-{{workflow_id}}-plan).",
        dependencies=["plan"],
        worker=pool[1 % len(pool)],
    ))
    engine.add_node(WorkflowNode(
        id="test",
        task=f"TEST: Run comprehensive tests and validation for: {goal}. "
             f"Verify the implementation from the code stage.",
        dependencies=["code"],
        worker=pool[2 % len(pool)],
    ))
    engine.add_node(WorkflowNode(
        id="deploy",
        task=f"DEPLOY: Final integration, documentation, and cleanup for: {goal}. "
             f"Ensure all tests passed before deploying.",
        dependencies=["test"],
        worker=pool[3 % len(pool)],
    ))

    engine.add_edge(WorkflowEdge("plan", "code"))
    engine.add_edge(WorkflowEdge("code", "test",
                                 condition_expr="not_contains:FAIL"))
    engine.add_edge(WorkflowEdge("test", "deploy",
                                 condition_expr="contains:PASS"))

    return engine


def builtin_audit_fix_verify(target: str,
                             workers: list[str] | None = None) -> DAGEngine:
    """3-stage remediation pipeline: audit → fix → verify.

    Stage 1 (audit): Scan and identify issues in the target.
    Stage 2 (fix): Apply fixes for all issues found. Depends on audit.
    Stage 3 (verify): Verify fixes resolved the issues. Depends on fix.
    """
    pool = workers or WORKER_NAMES
    engine = DAGEngine(name="audit-fix-verify")

    engine.add_node(WorkflowNode(
        id="audit",
        task=f"AUDIT: Scan and identify all issues in: {target}. "
             f"List every issue with severity and location.",
        worker=pool[0 % len(pool)],
    ))
    engine.add_node(WorkflowNode(
        id="fix",
        task=f"FIX: Apply fixes for all issues found in the audit of: {target}. "
             f"Read audit results from the bus.",
        dependencies=["audit"],
        worker=pool[1 % len(pool)],
    ))
    engine.add_node(WorkflowNode(
        id="verify",
        task=f"VERIFY: Re-scan {target} and confirm all audit issues are resolved. "
             f"Run py_compile and tests. Report PASS or FAIL.",
        dependencies=["fix"],
        worker=pool[2 % len(pool)],
    ))

    engine.add_edge(WorkflowEdge("audit", "fix"))
    engine.add_edge(WorkflowEdge("fix", "verify"))

    return engine


BUILTIN_WORKFLOWS = {
    "plan-code-test-deploy": builtin_plan_code_test_deploy,
    "audit-fix-verify": builtin_audit_fix_verify,
}
# signed: beta


# ── CLI ─────────────────────────────────────────────────────────────

def _cli():
    import argparse
    parser = argparse.ArgumentParser(
        description="Skynet Distributed Workflow Engine",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Execute a workflow")
    run_p.add_argument("--workflow", type=str,
                       help="Built-in workflow name (plan-code-test-deploy, audit-fix-verify)")
    run_p.add_argument("--goal", type=str, help="Goal/target for the workflow")
    run_p.add_argument("--nodes", type=str,
                       help="JSON file defining custom nodes and edges")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Print plan without executing")
    run_p.add_argument("--timeout", type=float, default=RESULT_POLL_TIMEOUT_S,
                       help=f"Timeout per node in seconds (default {RESULT_POLL_TIMEOUT_S})")
    run_p.add_argument("--workers", type=str,
                       help="Comma-separated worker list override")

    # status
    st_p = sub.add_parser("status", help="Show workflow status")
    st_p.add_argument("workflow_id", type=str)

    # list
    sub.add_parser("list", help="List all workflows")

    # resume
    res_p = sub.add_parser("resume", help="Resume a crashed workflow")
    res_p.add_argument("workflow_id", type=str)
    res_p.add_argument("--timeout", type=float, default=RESULT_POLL_TIMEOUT_S)

    # builtin
    sub.add_parser("builtin", help="List built-in workflows")

    args = parser.parse_args()

    if args.command == "run":
        workers = args.workers.split(",") if args.workers else None

        if args.workflow:
            factory = BUILTIN_WORKFLOWS.get(args.workflow)
            if not factory:
                print(f"Unknown workflow: {args.workflow}")
                print(f"Available: {', '.join(BUILTIN_WORKFLOWS.keys())}")
                return
            goal = args.goal or "unspecified goal"
            engine = factory(goal, workers=workers)
        elif args.nodes:
            with open(args.nodes, "r", encoding="utf-8") as f:
                spec = json.load(f)
            engine = DAGEngine(name=spec.get("name", "custom"))
            for nd in spec.get("nodes", []):
                engine.add_node(WorkflowNode.from_dict(nd))
            for ed in spec.get("edges", []):
                engine.add_edge(WorkflowEdge.from_dict(ed))
        else:
            run_p.print_help()
            return

        result = engine.execute(timeout_per_node=args.timeout,
                                dry_run=args.dry_run)
        print(json.dumps(result, indent=2))

    elif args.command == "status":
        data = _load_workflow(args.workflow_id)
        if not data:
            print(f"Workflow {args.workflow_id} not found.")
            return
        print(f"Workflow: {data.get('id')} ({data.get('name', '')})")
        print(f"Status:   {data.get('status', 'unknown')}")
        print(f"Created:  {data.get('created_at', '')}")
        print(f"Nodes:")
        for nd in data.get("nodes", []):
            status = nd.get("status", "?")
            worker = nd.get("worker", "?")
            retries = nd.get("retries", 0)
            r_str = f" (retries={retries})" if retries > 0 else ""
            print(f"  {nd['id']:>12}  [{status:>10}]  worker={worker}{r_str}")
            if nd.get("error"):
                print(f"               error: {nd['error'][:80]}")

    elif args.command == "list":
        workflows = _list_workflows()
        if not workflows:
            print("No workflows found.")
            return
        for w in workflows:
            print(f"  {w['id']}  [{w['status']:>9}]  nodes={w['nodes']}  {w['name']}")

    elif args.command == "resume":
        data = _load_workflow(args.workflow_id)
        if not data:
            print(f"Workflow {args.workflow_id} not found.")
            return
        engine = DAGEngine.from_dict(data)
        result = engine.resume(timeout_per_node=args.timeout)
        print(json.dumps(result, indent=2))

    elif args.command == "builtin":
        print("Built-in workflows:")
        for name in BUILTIN_WORKFLOWS:
            print(f"  {name}")

    else:
        parser.print_help()


if __name__ == "__main__":
    _cli()
# signed: beta
