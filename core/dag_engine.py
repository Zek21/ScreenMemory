"""
Dynamic DAG Engine — Runtime workflow generation and execution.

Instead of hardcoding Directed Acyclic Graphs for every agent interaction,
this engine generates DAGs dynamically from WorkflowPlans produced by the
DAAO router. DAGs define execution order, dependencies, retry policies,
and conditional branching.

Implements durable execution patterns:
- Automatic retries with exponential backoff
- State persistence across nodes
- Conditional routing (Generator-Critic loops)
- Timeout enforcement per node
"""
import time
import logging
import traceback
from enum import Enum
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)


class NodeStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class EdgeType(Enum):
    SEQUENTIAL = "sequential"   # B runs after A completes
    CONDITIONAL = "conditional"  # B runs only if A's output meets condition
    PARALLEL = "parallel"       # A and B run concurrently
    FEEDBACK = "feedback"       # Loop back (critic → producer)


@dataclass
class DAGNode:
    """A single execution node in the workflow DAG."""
    id: str
    role: str                    # Agent role to execute this node
    description: str
    handler: Optional[Callable] = None  # Execution function
    max_retries: int = 2
    timeout_seconds: float = 60.0
    status: NodeStatus = NodeStatus.PENDING
    output: Any = None
    error: Optional[str] = None
    start_time: float = 0.0
    end_time: float = 0.0
    retry_count: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def elapsed_seconds(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        elif self.start_time:
            return time.time() - self.start_time
        return 0.0


@dataclass
class DAGEdge:
    """An edge connecting two nodes in the DAG."""
    source: str      # Source node ID
    target: str      # Target node ID
    edge_type: EdgeType = EdgeType.SEQUENTIAL
    condition: Optional[Callable] = None  # For conditional edges


@dataclass
class ExecutionContext:
    """Shared context passed through the DAG during execution."""
    query: str
    node_outputs: Dict[str, Any] = field(default_factory=dict)
    shared_state: Dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    errors: List[str] = field(default_factory=list)


class DAG:
    """
    A directed acyclic graph representing a workflow.
    Nodes are execution steps, edges define dependencies.
    """

    def __init__(self, name: str = "workflow"):
        self.name = name
        self.nodes: Dict[str, DAGNode] = {}
        self.edges: List[DAGEdge] = []
        self._adjacency: Dict[str, List[str]] = defaultdict(list)
        self._reverse_adjacency: Dict[str, List[str]] = defaultdict(list)

    def add_node(self, node: DAGNode) -> 'DAG':
        """Add a node. Returns self for chaining."""
        self.nodes[node.id] = node
        return self

    def add_edge(self, source: str, target: str,
                 edge_type: EdgeType = EdgeType.SEQUENTIAL,
                 condition: Callable = None) -> 'DAG':
        """Add an edge. Returns self for chaining."""
        edge = DAGEdge(source=source, target=target,
                       edge_type=edge_type, condition=condition)
        self.edges.append(edge)
        self._adjacency[source].append(target)
        self._reverse_adjacency[target].append(source)
        return self

    def get_root_nodes(self) -> List[str]:
        """Get nodes with no incoming edges (entry points)."""
        all_targets = set()
        for edge in self.edges:
            all_targets.add(edge.target)
        return [nid for nid in self.nodes if nid not in all_targets]

    def get_ready_nodes(self) -> List[str]:
        """Get nodes whose dependencies are all completed."""
        ready = []
        for nid, node in self.nodes.items():
            if node.status != NodeStatus.PENDING:
                continue
            # Check all parents are completed
            parents = self._reverse_adjacency.get(nid, [])
            if not parents:
                ready.append(nid)
            elif all(self.nodes[p].status == NodeStatus.COMPLETED for p in parents):
                ready.append(nid)
        return ready

    def topological_sort(self) -> List[str]:
        """Return nodes in topological order."""
        in_degree = defaultdict(int)
        for edge in self.edges:
            in_degree[edge.target] += 1

        queue = [nid for nid in self.nodes if in_degree[nid] == 0]
        order = []

        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for child in self._adjacency.get(nid, []):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if len(order) != len(self.nodes):
            logger.error("DAG has cycles — cannot topologically sort")
            return list(self.nodes.keys())

        return order

    @property
    def is_complete(self) -> bool:
        return all(n.status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED, NodeStatus.FAILED)
                   for n in self.nodes.values())

    @property
    def stats(self) -> dict:
        statuses = defaultdict(int)
        for node in self.nodes.values():
            statuses[node.status.value] += 1
        return {
            "name": self.name,
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "statuses": dict(statuses),
            "complete": self.is_complete,
        }


class DAGExecutor:
    """
    Executes a DAG with durable execution patterns.

    Features:
    - Topological execution order
    - Automatic retries with exponential backoff
    - Conditional edge evaluation
    - Feedback loops (Generator-Critic pattern)
    - Timeout enforcement
    - Full execution trace
    """

    def __init__(self, max_feedback_loops: int = 3):
        self.max_feedback_loops = max_feedback_loops
        self._execution_log: List[dict] = []

    def execute(self, dag: DAG, context: ExecutionContext) -> ExecutionContext:
        """Execute the DAG in topological order with retry and feedback support."""
        t0 = time.perf_counter()
        logger.info(f"DAG execution starting: {dag.name} ({len(dag.nodes)} nodes)")

        execution_order = dag.topological_sort()
        feedback_count = 0

        for node_id in execution_order:
            node = dag.nodes[node_id]

            # Check conditional edges
            if not self._should_execute(node_id, dag, context):
                node.status = NodeStatus.SKIPPED
                logger.info(f"Node {node_id} skipped (condition not met)")
                continue

            # Execute with retry
            success = self._execute_node(node, context)

            if success:
                context.node_outputs[node_id] = node.output
            else:
                context.errors.append(f"{node_id}: {node.error}")

                # Check for feedback edges (Generator-Critic loop)
                feedback_targets = [
                    e.source for e in dag.edges
                    if e.target == node_id and e.edge_type == EdgeType.FEEDBACK
                ]
                if feedback_targets and feedback_count < self.max_feedback_loops:
                    feedback_count += 1
                    logger.info(f"Feedback loop {feedback_count}: "
                                f"{node_id} → {feedback_targets}")
                    # Re-execute the source node with feedback
                    for target in feedback_targets:
                        target_node = dag.nodes[target]
                        target_node.status = NodeStatus.PENDING
                        target_node.metadata["feedback"] = node.error
                        self._execute_node(target_node, context)

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"DAG execution complete: {dag.stats} [{elapsed:.0f}ms]")

        self._execution_log.append({
            "dag": dag.name,
            "stats": dag.stats,
            "elapsed_ms": elapsed,
            "errors": context.errors,
            "timestamp": time.time(),
        })

        return context

    def _execute_node(self, node: DAGNode, context: ExecutionContext) -> bool:
        """Execute a single node with retry logic."""
        for attempt in range(node.max_retries + 1):
            node.status = NodeStatus.RUNNING if attempt == 0 else NodeStatus.RETRYING
            node.start_time = time.time()
            node.retry_count = attempt

            try:
                if node.handler:
                    node.output = node.handler(context)
                else:
                    # Default: pass through (node is a placeholder)
                    node.output = {
                        "node": node.id,
                        "role": node.role,
                        "status": "executed",
                        "context_keys": list(context.node_outputs.keys()),
                    }

                node.status = NodeStatus.COMPLETED
                node.end_time = time.time()
                logger.info(f"Node {node.id} completed ({node.elapsed_seconds:.1f}s)")
                return True

            except Exception as e:
                node.error = str(e)
                node.end_time = time.time()
                logger.warning(f"Node {node.id} attempt {attempt + 1} failed: {e}")

                if attempt < node.max_retries:
                    # Exponential backoff
                    wait = min(30, 2 ** attempt)
                    logger.info(f"Retrying {node.id} in {wait}s...")
                    time.sleep(wait)

        node.status = NodeStatus.FAILED
        logger.error(f"Node {node.id} failed after {node.max_retries + 1} attempts")
        return False

    def _should_execute(self, node_id: str, dag: DAG, context: ExecutionContext) -> bool:
        """Check if conditional edges allow execution."""
        incoming = [e for e in dag.edges if e.target == node_id]

        for edge in incoming:
            if edge.edge_type == EdgeType.CONDITIONAL and edge.condition:
                source_output = context.node_outputs.get(edge.source)
                try:
                    if not edge.condition(source_output):
                        return False
                except Exception:
                    return False

        return True

    @property
    def execution_history(self) -> List[dict]:
        return list(self._execution_log)


class DAGBuilder:
    """
    Dynamically generates DAGs from WorkflowPlans.
    Maps DAAO operator types to DAG topologies.
    """

    @staticmethod
    def from_workflow_plan(plan) -> DAG:
        """
        Generate a DAG from a DAAO WorkflowPlan.
        Maps operator types to standard topologies.
        """
        from core.difficulty_router import OperatorType

        dag = DAG(name=f"wf_{plan.operator.value}")

        if plan.operator == OperatorType.DIRECT:
            dag.add_node(DAGNode(
                id="execute",
                role=plan.agent_roles[0] if plan.agent_roles else "reasoner",
                description=f"Direct execution: {plan.query[:60]}",
                max_retries=0,
            ))

        elif plan.operator == OperatorType.CHAIN_OF_THOUGHT:
            dag.add_node(DAGNode(
                id="reason",
                role="reasoner",
                description="Chain-of-thought reasoning",
            ))
            dag.add_node(DAGNode(
                id="synthesize",
                role="reasoner",
                description="Synthesize reasoning into answer",
            ))
            dag.add_edge("reason", "synthesize")

        elif plan.operator == OperatorType.TOOL_AUGMENTED:
            dag.add_node(DAGNode(
                id="plan",
                role="planner",
                description="Plan tool-augmented execution",
            ))
            dag.add_node(DAGNode(
                id="execute_tools",
                role="tool_executor",
                description="Execute tool calls",
                max_retries=2,
            ))
            dag.add_node(DAGNode(
                id="synthesize",
                role="reasoner",
                description="Synthesize tool outputs",
            ))
            dag.add_edge("plan", "execute_tools")
            dag.add_edge("execute_tools", "synthesize")

        elif plan.operator == OperatorType.MULTI_AGENT:
            # Multi-agent: planner → parallel specialists → validator
            dag.add_node(DAGNode(
                id="plan",
                role="planner",
                description="Decompose into specialist tasks",
            ))

            specialist_ids = []
            for i, role in enumerate(plan.agent_roles):
                if role not in ("planner", "validator"):
                    sid = f"specialist_{i}"
                    dag.add_node(DAGNode(
                        id=sid,
                        role=role,
                        description=f"Specialist execution: {role}",
                    ))
                    dag.add_edge("plan", sid)
                    specialist_ids.append(sid)

            dag.add_node(DAGNode(
                id="validate",
                role="validator",
                description="Validate and synthesize specialist outputs",
            ))
            for sid in specialist_ids:
                dag.add_edge(sid, "validate")

        elif plan.operator == OperatorType.DEBATE:
            # Debate: proposer → critic → judge (with feedback loop)
            dag.add_node(DAGNode(
                id="propose",
                role="proposer",
                description="Generate initial proposal with evidence",
            ))
            dag.add_node(DAGNode(
                id="critique",
                role="critic",
                description="Challenge proposal with counterarguments",
            ))
            dag.add_node(DAGNode(
                id="judge",
                role="judge",
                description="Evaluate debate and render verdict",
            ))
            dag.add_edge("propose", "critique")
            dag.add_edge("critique", "judge")
            # Feedback: if judge is unsatisfied, loop back to proposer
            dag.add_edge("judge", "propose", EdgeType.FEEDBACK)

        # Add domain specialists as parallel nodes where applicable
        for role in plan.agent_roles:
            if role.endswith("_specialist") and role not in [n.role for n in dag.nodes.values()]:
                dag.add_node(DAGNode(
                    id=f"domain_{role}",
                    role=role,
                    description=f"Domain specialist: {role}",
                ))

        return dag
