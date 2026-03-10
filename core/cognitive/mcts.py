"""
Reflective Monte Carlo Tree Search (R-MCTS) for Web Navigation.

Implements a dual-optimization search algorithm for autonomous web navigation:
- Global Optimization: Decompose task into subtasks (limits search depth)
- Local Optimization: MCTS per subtask with contrastive reflection

Key innovations over classical MCTS:
1. Contrastive reflection: Compare failed states vs successful states
2. UCB1 exploration/exploitation balance adapted for web navigation
3. VLM-based state evaluation (perceive screenshot, judge progress)
4. Backtracking via browser state snapshots

Reference: "WebPilot: A Versatile and Autonomous Multi-Agent System for Web
Task Execution" and "R-MCTS: Reflective Monte Carlo Tree Search" (2024-2025)

Architecture:
    ┌────────────────────────────────────────────┐
    │              GLOBAL PLANNER                │
    │  Task -> [Subtask_1, Subtask_2, ...]       │
    └──────────────┬─────────────────────────────┘
                   │
    ┌──────────────▼─────────────────────────────┐
    │         LOCAL MCTS (per subtask)            │
    │                                             │
    │   SELECT ──▶ EXPAND ──▶ SIMULATE ──▶ BACK  │
    │      ▲                                │     │
    │      └────────────────────────────────┘     │
    │                                             │
    │   + Contrastive Reflection on failures      │
    │   + UCB1 exploration/exploitation balance    │
    └─────────────────────────────────────────────┘

UCB1 Formula:
    UCB1(node) = Q(node)/N(node) + C * sqrt(ln(N(parent)) / N(node))

    Q = cumulative reward (sum of simulation scores)
    N = visit count
    C = exploration constant (sqrt(2) by default)
"""
import math
import time
import random
import logging
from typing import List, Optional, Dict, Tuple, Callable, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class NavigationState:
    """Represents a snapshot of the web navigation environment."""
    id: str
    description: str
    screenshot_hash: str = ""
    url: str = ""
    active_app: str = ""
    visible_elements: int = 0
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class NavigationAction:
    """An action that transitions between navigation states."""
    action_type: str  # click, type, scroll, navigate, key, wait
    target: str       # element identifier or coordinates
    value: str = ""   # text to type, URL to visit, etc.
    description: str = ""

    def __str__(self):
        return f"{self.action_type}({self.target}={self.value})"


@dataclass
class MCTSNode:
    """A node in the Monte Carlo search tree."""
    id: str
    state: NavigationState
    action: Optional[NavigationAction] = None  # Action that led to this state
    parent: Optional['MCTSNode'] = None
    children: List['MCTSNode'] = field(default_factory=list)

    # MCTS statistics
    visits: int = 0
    total_reward: float = 0.0
    depth: int = 0

    # Reflection data
    reflections: List[str] = field(default_factory=list)
    is_terminal: bool = False
    is_failure: bool = False

    @property
    def average_reward(self) -> float:
        if self.visits == 0:
            return 0.0
        return self.total_reward / self.visits

    @property
    def ucb1(self) -> float:
        """Upper Confidence Bound for Trees (UCB1)."""
        if self.visits == 0:
            return float('inf')  # Unexplored nodes have infinite priority
        if self.parent is None or self.parent.visits == 0:
            return self.average_reward

        exploitation = self.average_reward
        exploration = math.sqrt(2) * math.sqrt(
            math.log(self.parent.visits) / self.visits
        )
        return exploitation + exploration

    def add_child(self, child: 'MCTSNode'):
        child.parent = self
        child.depth = self.depth + 1
        self.children.append(child)


class ReflectiveMCTS:
    """
    R-MCTS: Reflective Monte Carlo Tree Search for web navigation.

    Combines classical MCTS with contrastive reflection to navigate
    complex, stochastic web environments.

    The key insight: when a navigation path fails, we don't just backtrack.
    We analyze WHY it failed by comparing the failed state against
    previously successful states, generating actionable reflections
    that prevent the same failure pattern.

    Usage:
        mcts = ReflectiveMCTS(state_evaluator=my_vlm_scorer)
        
        # Initialize with current state
        root = mcts.create_root(current_state)
        
        # Run search iterations
        for _ in range(num_iterations):
            mcts.iterate()
        
        # Get best action
        best_action = mcts.get_best_action()

    LOG FORMAT:
        [R-MCTS] select     -- depth=3, ucb1=1.42, node=n_007
        [R-MCTS] expand     -- 4 new actions from n_007
        [R-MCTS] simulate   -- rollout score=0.73 (3 steps)
        [R-MCTS] backprop   -- updated 4 ancestors
        [R-MCTS] reflect    -- n_012 failed: "page returned 404, wrong URL pattern"
        [R-MCTS] contrast   -- comparing n_012 (fail) vs n_005 (success)
        [R-MCTS] best_action -- click(search_button) with score=0.81
    """

    def __init__(self, state_evaluator: Optional[Callable] = None,
                 action_generator: Optional[Callable] = None,
                 max_depth: int = 10, max_iterations: int = 50,
                 exploration_constant: float = 1.414):
        """
        Args:
            state_evaluator: Function that scores a NavigationState (0.0-1.0)
            action_generator: Function that returns possible actions from a state
            max_depth: Maximum search tree depth
            max_iterations: Maximum MCTS iterations per decision
            exploration_constant: C in UCB1 formula (higher = more exploration)
        """
        self.state_evaluator = state_evaluator or self._default_evaluator
        self.action_generator = action_generator or self._default_actions
        self.max_depth = max_depth
        self.max_iterations = max_iterations
        self.exploration_constant = exploration_constant

        self._root: Optional[MCTSNode] = None
        self._node_counter = 0
        self._successful_states: List[NavigationState] = []
        self._failed_states: List[Tuple[NavigationState, str]] = []
        self._reflections: List[dict] = []

    def _next_id(self) -> str:
        self._node_counter += 1
        return f"n_{self._node_counter:04d}"

    # ── MCTS Core Loop ──

    def create_root(self, state: NavigationState) -> MCTSNode:
        """Initialize the search tree with the current state."""
        self._root = MCTSNode(
            id=self._next_id(),
            state=state,
            depth=0,
        )
        logger.info(f"[R-MCTS] root created: {state.description[:60]}")
        return self._root

    def iterate(self) -> float:
        """
        Run one MCTS iteration: Select -> Expand -> Simulate -> Backpropagate.
        Returns the simulation score.
        """
        if self._root is None:
            raise ValueError("Must call create_root() first")

        # 1. SELECT: Traverse tree using UCB1 to find promising leaf
        selected = self._select(self._root)

        # 2. EXPAND: Generate child nodes for possible actions
        if not selected.is_terminal and selected.depth < self.max_depth:
            expanded = self._expand(selected)
            if expanded:
                selected = expanded

        # 3. SIMULATE: Estimate value via rollout
        score = self._simulate(selected)

        # 4. BACKPROPAGATE: Update statistics up the tree
        self._backpropagate(selected, score)

        return score

    def search(self, state: NavigationState, iterations: int = None) -> NavigationAction:
        """
        Run full MCTS search and return the best action.

        Args:
            state: Current navigation state
            iterations: Number of search iterations (default: max_iterations)

        Returns:
            Best action to take
        """
        iterations = iterations or self.max_iterations
        self.create_root(state)

        start = time.perf_counter()

        for i in range(iterations):
            score = self.iterate()

            # Early termination if we find a high-confidence action
            best = self.get_best_action()
            if best and self._root.children:
                best_child = max(self._root.children, key=lambda c: c.average_reward)
                if best_child.visits > iterations // 3 and best_child.average_reward > 0.85:
                    logger.info(f"[R-MCTS] early termination at iteration {i+1} "
                                f"(score={best_child.average_reward:.2f})")
                    break

        elapsed = (time.perf_counter() - start) * 1000

        best_action = self.get_best_action()
        logger.info(f"[R-MCTS] search complete: {iterations} iterations, {elapsed:.0f}ms, "
                     f"best={best_action}")

        return best_action

    def _select(self, node: MCTSNode) -> MCTSNode:
        """SELECT phase: Traverse tree using UCB1 to find a leaf."""
        current = node

        while current.children and not current.is_terminal:
            # Pick child with highest UCB1 score
            current = max(current.children, key=lambda c: c.ucb1)

        logger.debug(f"[R-MCTS] select: depth={current.depth}, node={current.id}")
        return current

    def _expand(self, node: MCTSNode) -> Optional[MCTSNode]:
        """EXPAND phase: Generate possible actions and create child nodes."""
        actions = self.action_generator(node.state)

        if not actions:
            node.is_terminal = True
            return None

        # Create child nodes for each possible action
        children_created = 0
        for action in actions:
            # Create hypothetical next state
            next_state = NavigationState(
                id=self._next_id(),
                description=f"After: {action.description or str(action)}",
                url=node.state.url,
                active_app=node.state.active_app,
            )

            child = MCTSNode(
                id=self._next_id(),
                state=next_state,
                action=action,
            )
            node.add_child(child)
            children_created += 1

        logger.debug(f"[R-MCTS] expand: {children_created} actions from {node.id}")

        # Return a random unexplored child for simulation
        unexplored = [c for c in node.children if c.visits == 0]
        if unexplored:
            return random.choice(unexplored)
        return node.children[0] if node.children else None

    def _simulate(self, node: MCTSNode) -> float:
        """
        SIMULATE phase: Estimate the value of a node.
        Uses state evaluator (VLM if available) to score the state.
        """
        score = self.state_evaluator(node.state)

        # Apply depth penalty (deeper searches are less certain)
        depth_penalty = 0.95 ** node.depth
        adjusted_score = score * depth_penalty

        logger.debug(f"[R-MCTS] simulate: node={node.id}, raw={score:.2f}, adjusted={adjusted_score:.2f}")

        # Track for contrastive reflection
        if adjusted_score < 0.3:
            node.is_failure = True
            self._failed_states.append((node.state, f"Low score: {adjusted_score:.2f}"))
            self._reflect(node)
        elif adjusted_score > 0.7:
            self._successful_states.append(node.state)

        return adjusted_score

    def _backpropagate(self, node: MCTSNode, score: float):
        """BACKPROPAGATE phase: Update statistics from leaf to root."""
        current = node
        updates = 0

        while current is not None:
            current.visits += 1
            current.total_reward += score
            current = current.parent
            updates += 1

        logger.debug(f"[R-MCTS] backprop: updated {updates} ancestors with score={score:.2f}")

    # ── Contrastive Reflection ──

    def _reflect(self, failed_node: MCTSNode):
        """
        CONTRASTIVE REFLECTION: Analyze why a node failed by comparing
        it against previously successful states.

        This is the key innovation of R-MCTS:
        - Failed state: "Page showed 404 error"
        - Successful state: "Search results page loaded correctly"
        - Reflection: "The URL pattern was wrong. Use /search?q= instead of /find/"
        """
        if not self._successful_states:
            reflection = {
                "failed_node": failed_node.id,
                "failed_state": failed_node.state.description,
                "reflection": "No successful states to contrast against. "
                              "Need more exploration before meaningful reflection.",
                "timestamp": time.time(),
            }
        else:
            # Find most similar successful state
            best_match = self._successful_states[-1]  # Most recent success

            reflection = {
                "failed_node": failed_node.id,
                "failed_state": failed_node.state.description,
                "success_state": best_match.description,
                "reflection": (
                    f"Failed at: {failed_node.state.description[:80]}. "
                    f"Previously succeeded at: {best_match.description[:80]}. "
                    f"Action that led to failure: {failed_node.action if failed_node.action else 'unknown'}. "
                    f"Avoid this action pattern in similar states."
                ),
                "timestamp": time.time(),
            }

        self._reflections.append(reflection)
        failed_node.reflections.append(reflection["reflection"])

        logger.info(f"[R-MCTS] reflect: {reflection['reflection'][:100]}")

    # ── Action Selection ──

    def get_best_action(self) -> Optional[NavigationAction]:
        """
        Get the best action from the root based on visit count.
        Visit count is more stable than average reward for final selection.
        """
        if not self._root or not self._root.children:
            return None

        # Select child with most visits (most explored = most confident)
        best_child = max(self._root.children, key=lambda c: c.visits)
        return best_child.action

    def get_action_rankings(self) -> List[Tuple[NavigationAction, float, int]]:
        """Get all root actions ranked by visit count."""
        if not self._root or not self._root.children:
            return []

        rankings = []
        for child in sorted(self._root.children, key=lambda c: c.visits, reverse=True):
            if child.action:
                rankings.append((child.action, child.average_reward, child.visits))

        return rankings

    # ── Default Implementations ──

    def _default_evaluator(self, state: NavigationState) -> float:
        """Default state evaluator using simple heuristics."""
        score = 0.5

        # Reward states with more visible elements (richer pages)
        if state.visible_elements > 10:
            score += 0.1
        if state.visible_elements > 20:
            score += 0.1

        # Reward non-empty URLs (actually navigated somewhere)
        if state.url:
            score += 0.1

        # Penalize error-like descriptions
        desc_lower = state.description.lower()
        if any(w in desc_lower for w in ["error", "404", "failed", "timeout", "blocked"]):
            score -= 0.3
        if any(w in desc_lower for w in ["success", "results", "found", "loaded"]):
            score += 0.2

        return max(0.0, min(1.0, score))

    def _default_actions(self, state: NavigationState) -> List[NavigationAction]:
        """Default action generator producing common web navigation actions."""
        actions = [
            NavigationAction("click", "search_button", description="Click search button"),
            NavigationAction("click", "next_link", description="Click next result link"),
            NavigationAction("type", "search_field", "query", description="Type in search field"),
            NavigationAction("scroll", "page", "down", description="Scroll down"),
            NavigationAction("key", "keyboard", "Enter", description="Press Enter"),
        ]
        # Return a random subset to simulate action space filtering
        k = min(len(actions), random.randint(2, 4))
        return random.sample(actions, k)

    # ── Stats ──

    @property
    def stats(self) -> dict:
        if not self._root:
            return {"status": "not_initialized"}

        def count_nodes(node):
            total = 1
            for c in node.children:
                total += count_nodes(c)
            return total

        total = count_nodes(self._root)

        return {
            "total_nodes": total,
            "root_visits": self._root.visits,
            "root_children": len(self._root.children),
            "max_depth_reached": self._get_max_depth(self._root),
            "successful_states": len(self._successful_states),
            "failed_states": len(self._failed_states),
            "reflections": len(self._reflections),
        }

    def _get_max_depth(self, node: MCTSNode) -> int:
        if not node.children:
            return node.depth
        return max(self._get_max_depth(c) for c in node.children)


class DualOptimizationMCTS:
    """
    Dual Optimization MCTS: Global planning + Local search.

    Implements the WebPilot-style two-phase optimization:
    1. GLOBAL: Decompose task into subtasks (reduces search space)
    2. LOCAL: Run R-MCTS on each subtask independently

    This prevents the mathematical explosion of the action space
    that occurs when searching across the full task horizon.
    """

    def __init__(self, planner=None, state_evaluator=None,
                 local_iterations: int = 30):
        self.planner = planner
        self.local_iterations = local_iterations
        self._local_mcts = ReflectiveMCTS(
            state_evaluator=state_evaluator,
            max_iterations=local_iterations,
        )
        self._subtask_results: List[dict] = []

    def solve(self, goal: str, initial_state: NavigationState) -> List[NavigationAction]:
        """
        Solve a complex navigation goal using dual optimization.

        Returns ordered list of actions to execute.
        """
        logger.info(f"[DualMCTS] global: decomposing '{goal[:60]}'")

        # Phase 1: Global optimization (decompose)
        if self.planner:
            plan = self.planner.create_plan(goal)
            subtask_descriptions = [s.description for s in plan.subtasks]
        else:
            subtask_descriptions = [goal]

        logger.info(f"[DualMCTS] global: {len(subtask_descriptions)} subtasks")

        # Phase 2: Local optimization (MCTS per subtask)
        all_actions = []
        current_state = initial_state

        for i, desc in enumerate(subtask_descriptions):
            logger.info(f"[DualMCTS] local: subtask {i+1}/{len(subtask_descriptions)}: {desc[:60]}")

            # Create subtask state
            subtask_state = NavigationState(
                id=f"subtask_{i+1}",
                description=desc,
                url=current_state.url,
                active_app=current_state.active_app,
            )

            # Run local MCTS
            best_action = self._local_mcts.search(subtask_state, self.local_iterations)

            if best_action:
                all_actions.append(best_action)
                self._subtask_results.append({
                    "subtask": desc,
                    "action": str(best_action),
                    "stats": self._local_mcts.stats,
                })

            # Update state for next subtask
            current_state = subtask_state

        logger.info(f"[DualMCTS] complete: {len(all_actions)} actions across "
                     f"{len(subtask_descriptions)} subtasks")
        return all_actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== R-MCTS Navigation Search Test ===\n")

    mcts = ReflectiveMCTS(max_iterations=20)

    # Create initial state
    state = NavigationState(
        id="start",
        description="Chrome browser open on Google homepage",
        url="https://www.google.com",
        active_app="Chrome",
        visible_elements=15,
    )

    # Run search
    best_action = mcts.search(state, iterations=20)

    print(f"\nBest action: {best_action}")
    print(f"Stats: {mcts.stats}")

    # Show action rankings
    rankings = mcts.get_action_rankings()
    print(f"\nAction rankings:")
    for action, reward, visits in rankings:
        print(f"  {action} -- reward={reward:.2f}, visits={visits}")

    # Show reflections
    if mcts._reflections:
        print(f"\nReflections ({len(mcts._reflections)}):")
        for r in mcts._reflections[-3:]:
            print(f"  {r['reflection'][:100]}")
