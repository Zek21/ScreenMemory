"""
Difficulty-Aware Agentic Orchestration (DAAO) Router.

Implements the DAAO framework from arXiv:2509.11079 — predicts query complexity
and routes to the optimal operator depth + LLM backend, preventing computational
waste on simple tasks while deploying multi-agent debate for complex ones.

Three interdependent modules:
1. DifficultyEstimator — lightweight complexity classifier (replaces VAE for local use)
2. OperatorAllocator — selects Chain-of-Thought vs Multi-Agent Debate vs Direct
3. CostAwareRouter — assigns operators to LLM backends within budget

Optimization: max E[U(response) - λ·C(workflow)]
"""
import re
import math
import time
import hashlib
import logging
from enum import Enum
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class QueryDifficulty(Enum):
    """Difficulty levels mapped to operator depths."""
    TRIVIAL = 1      # Direct answer, no reasoning chain needed
    SIMPLE = 2       # Single-step CoT sufficient
    MODERATE = 3     # Multi-step CoT with tool calls
    COMPLEX = 4      # Multi-agent collaboration required
    ADVERSARIAL = 5  # Full debate protocol + verification


class OperatorType(Enum):
    """Agentic operators ordered by computational cost."""
    DIRECT = "direct"                  # Single LLM call, no reasoning
    CHAIN_OF_THOUGHT = "cot"           # Sequential reasoning chain
    TOOL_AUGMENTED = "tool_augmented"  # CoT + external tool calls
    MULTI_AGENT = "multi_agent"        # Parallel specialist agents
    DEBATE = "debate"                  # Multi-agent debate with critic


@dataclass
class DifficultySignal:
    """Output of the difficulty estimation engine."""
    level: QueryDifficulty
    confidence: float           # 0-1 how confident the estimate is
    complexity_score: float     # Raw 0-1 score
    domain_tags: List[str]      # Detected domains: ["code", "finance", "web"]
    reasoning_depth: int        # Estimated reasoning hops needed
    requires_tools: bool        # Whether external tools are likely needed
    requires_debate: bool       # Whether adversarial verification is needed
    estimated_tokens: int       # Predicted token budget


@dataclass
class WorkflowPlan:
    """A fully specified execution plan for a query."""
    query: str
    difficulty: DifficultySignal
    operator: OperatorType
    backend: str               # LLM model to use
    agent_roles: List[str]     # Specialist roles to instantiate
    tool_bindings: List[str]   # MCP tools to attach
    max_iterations: int        # Retry/debate rounds budget
    token_budget: int          # Hard token cap
    cost_estimate: float       # Relative cost 0-1
    timestamp: float = field(default_factory=time.time)


class DifficultyEstimator:
    """
    Lightweight query complexity classifier.
    Uses heuristic feature extraction instead of a VAE to avoid
    requiring GPU for the routing layer itself.

    Features analyzed:
    - Lexical complexity (vocabulary, sentence structure)
    - Domain indicators (code patterns, financial terms, etc.)
    - Reasoning markers (multi-step, comparison, analysis keywords)
    - Constraint density (specific requirements, formats, limits)
    - Ambiguity signals (vague references, open-ended questions)
    """

    # Domain keyword sets
    DOMAIN_PATTERNS = {
        "code": re.compile(
            r'\b(function|class|def |import |variable|error|bug|debug|refactor|'
            r'API|endpoint|database|SQL|Python|JavaScript|TypeScript|git|deploy|'
            r'compile|runtime|exception|syntax|algorithm|regex)\b', re.I),
        "finance": re.compile(
            r'\b(market|stock|price|trading|portfolio|investment|revenue|'
            r'profit|loss|ROI|hedge|dividend|equity|bond|yield|crypto|'
            r'bitcoin|stablecoin|DeFi|treasury|valuation)\b', re.I),
        "web": re.compile(
            r'\b(website|browser|click|navigate|form|login|search|page|'
            r'URL|HTML|CSS|DOM|scrape|download|upload|screenshot)\b', re.I),
        "analysis": re.compile(
            r'\b(analyze|compare|contrast|evaluate|assess|investigate|'
            r'research|synthesize|correlate|implications|impact|trends)\b', re.I),
        "system": re.compile(
            r'\b(install|configure|setup|deploy|monitor|process|daemon|'
            r'service|container|docker|kubernetes|server|SSH|network)\b', re.I),
    }

    # Complexity escalation markers
    REASONING_MARKERS = re.compile(
        r'\b(step.by.step|first.*then|if.*then.*else|compare.*and|'
        r'pros.and.cons|trade.?offs?|multi.?step|chain.of|'
        r'because.*therefore|given.*determine|considering.*evaluate)\b', re.I)

    CONSTRAINT_MARKERS = re.compile(
        r'\b(must|shall|exactly|at least|no more than|within|between|'
        r'format as|output in|minimum|maximum|required|mandatory)\b', re.I)

    AMBIGUITY_MARKERS = re.compile(
        r'\b(maybe|perhaps|something like|kind of|sort of|whatever|'
        r'anything|everything|general|broad|overview|about)\b', re.I)

    MULTI_HOP_MARKERS = re.compile(
        r'\b(and then|after that|based on.*result|using.*output|'
        r'combine.*with|cross.?reference|correlate.*against|'
        r'first.*second.*third|multiple|several|across)\b', re.I)

    def __init__(self, history_weight: float = 0.3):
        self._history: Dict[str, DifficultySignal] = {}
        self._history_weight = history_weight

    def estimate(self, query: str) -> DifficultySignal:
        """Estimate difficulty of a query using feature extraction."""
        t0 = time.perf_counter()

        features = self._extract_features(query)
        raw_score = self._compute_raw_score(features)
        level = self._score_to_level(raw_score)
        confidence = 0.5 + abs(raw_score - 0.5)  # Further from midpoint = more confident

        raw_score, confidence = self._blend_history(query, raw_score, confidence)

        signal = DifficultySignal(
            level=level,
            confidence=confidence,
            complexity_score=raw_score,
            domain_tags=features["domains"],
            reasoning_depth=max(1, features["reasoning_hits"] + features["multi_hop_hits"] + len(features["domains"])),
            requires_tools=("code" in features["domains"] or "web" in features["domains"]
                            or "system" in features["domains"] or features["constraint_hits"] > 2),
            requires_debate=raw_score >= 0.7 or (len(features["domains"]) >= 3 and features["reasoning_hits"] >= 2),
            estimated_tokens={
                QueryDifficulty.TRIVIAL: 256, QueryDifficulty.SIMPLE: 512,
                QueryDifficulty.MODERATE: 1024, QueryDifficulty.COMPLEX: 2048,
                QueryDifficulty.ADVERSARIAL: 4096,
            }[level],
        )

        query_hash = hashlib.md5(query.lower().encode()).hexdigest()[:8]
        self._history[query_hash] = signal
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"Difficulty estimate: {level.name} (score={raw_score:.3f}, "
                    f"conf={confidence:.2f}, domains={features['domains']}) [{elapsed:.1f}ms]")
        return signal

    def _extract_features(self, query: str) -> dict:
        """Extract all signal features from a query."""
        tokens = query.split()
        sentence_count = max(1, len(re.split(r'[.!?]+', query)))
        domains = []
        domain_density = 0
        for domain, pattern in self.DOMAIN_PATTERNS.items():
            matches = len(pattern.findall(query))
            if matches > 0:
                domains.append(domain)
                domain_density += matches
        return {
            "token_count": len(tokens),
            "sentence_count": sentence_count,
            "domains": domains,
            "domain_density": domain_density,
            "reasoning_hits": len(self.REASONING_MARKERS.findall(query)),
            "multi_hop_hits": len(self.MULTI_HOP_MARKERS.findall(query)),
            "constraint_hits": len(self.CONSTRAINT_MARKERS.findall(query)),
            "ambiguity_hits": len(self.AMBIGUITY_MARKERS.findall(query)),
        }

    @staticmethod
    def _compute_raw_score(features: dict) -> float:
        """Compute the raw complexity score (0-1) from extracted features."""
        length_score = min(1.0, features["token_count"] / 200)
        domain_score = min(1.0, len(features["domains"]) / 3)
        reasoning_score = min(1.0, (features["reasoning_hits"] + features["multi_hop_hits"]) / 4)
        constraint_score = min(1.0, features["constraint_hits"] / 5)
        ambiguity_penalty = min(0.3, features["ambiguity_hits"] * 0.05)
        return (
            length_score * 0.15 + domain_score * 0.25 +
            reasoning_score * 0.30 + constraint_score * 0.20 +
            (1 - ambiguity_penalty) * 0.10
        )

    @staticmethod
    def _score_to_level(raw_score: float) -> "QueryDifficulty":
        """Map raw score to a difficulty level."""
        if raw_score < 0.15:
            return QueryDifficulty.TRIVIAL
        elif raw_score < 0.35:
            return QueryDifficulty.SIMPLE
        elif raw_score < 0.55:
            return QueryDifficulty.MODERATE
        elif raw_score < 0.80:
            return QueryDifficulty.COMPLEX
        return QueryDifficulty.ADVERSARIAL

    def _blend_history(self, query: str, raw_score: float, confidence: float):
        """Blend score with historical data for the same query."""
        query_hash = hashlib.md5(query.lower().encode()).hexdigest()[:8]
        if query_hash in self._history:
            prev = self._history[query_hash]
            raw_score = raw_score * (1 - self._history_weight) + prev.complexity_score * self._history_weight
            confidence = min(1.0, confidence + 0.1)
        return raw_score, confidence

    def update_from_feedback(self, query: str, actual_difficulty: QueryDifficulty,
                             success: bool):
        """Update estimates based on execution outcomes (self-adjusting policy)."""
        query_hash = hashlib.md5(query.lower().encode()).hexdigest()[:8]
        if query_hash in self._history:
            prev = self._history[query_hash]
            # Adjust score toward actual difficulty
            target = actual_difficulty.value / 5.0
            adjusted = prev.complexity_score * 0.7 + target * 0.3
            self._history[query_hash] = DifficultySignal(
                level=actual_difficulty,
                confidence=min(1.0, prev.confidence + 0.15),
                complexity_score=adjusted,
                domain_tags=prev.domain_tags,
                reasoning_depth=prev.reasoning_depth,
                requires_tools=prev.requires_tools,
                requires_debate=prev.requires_debate,
                estimated_tokens=prev.estimated_tokens,
            )
            logger.info(f"DAAO feedback: {query[:40]}... → {actual_difficulty.name} "
                        f"(was {prev.level.name}, success={success})")


class OperatorAllocator:
    """
    Selects the optimal agentic operator based on difficulty signal.
    Maps difficulty levels to operator types and determines required agent roles.
    """

    # Default operator mapping — can be overridden via config
    OPERATOR_MAP = {
        QueryDifficulty.TRIVIAL: OperatorType.DIRECT,
        QueryDifficulty.SIMPLE: OperatorType.CHAIN_OF_THOUGHT,
        QueryDifficulty.MODERATE: OperatorType.TOOL_AUGMENTED,
        QueryDifficulty.COMPLEX: OperatorType.MULTI_AGENT,
        QueryDifficulty.ADVERSARIAL: OperatorType.DEBATE,
    }

    # Agent roles per operator type
    ROLE_MAP = {
        OperatorType.DIRECT: [],
        OperatorType.CHAIN_OF_THOUGHT: ["reasoner"],
        OperatorType.TOOL_AUGMENTED: ["reasoner", "tool_executor"],
        OperatorType.MULTI_AGENT: ["planner", "specialist", "validator"],
        OperatorType.DEBATE: ["proposer", "critic", "judge", "tool_executor"],
    }

    # Cost multipliers
    COST_MAP = {
        OperatorType.DIRECT: 0.1,
        OperatorType.CHAIN_OF_THOUGHT: 0.25,
        OperatorType.TOOL_AUGMENTED: 0.45,
        OperatorType.MULTI_AGENT: 0.70,
        OperatorType.DEBATE: 1.0,
    }

    def allocate(self, signal: DifficultySignal) -> Tuple[OperatorType, List[str], float]:
        """Select operator, roles, and estimate cost."""
        operator = self.OPERATOR_MAP[signal.level]

        # Override: if tools required but operator doesn't support them, escalate
        if signal.requires_tools and operator in (OperatorType.DIRECT, OperatorType.CHAIN_OF_THOUGHT):
            operator = OperatorType.TOOL_AUGMENTED

        # Override: if debate required, escalate
        if signal.requires_debate and operator != OperatorType.DEBATE:
            operator = OperatorType.DEBATE

        roles = list(self.ROLE_MAP[operator])

        # Add domain-specific specialist roles
        for domain in signal.domain_tags:
            specialist = f"{domain}_specialist"
            if specialist not in roles:
                roles.append(specialist)

        cost = self.COST_MAP[operator]

        logger.info(f"Operator allocated: {operator.value} → roles={roles}, cost={cost:.2f}")
        return operator, roles, cost


class CostAwareRouter:
    """
    Routes allocated operators to heterogeneous LLM backends.
    Balances reasoning quality against computational budget.
    """

    def __init__(self, backends: Dict[str, dict] = None):
        self.backends = backends or {
            "moondream": {
                "type": "vlm",
                "speed": "fast",
                "cost": 0.1,
                "capabilities": ["vision", "description"],
                "max_tokens": 512,
            },
            "qwen3:8b": {
                "type": "llm",
                "speed": "medium",
                "cost": 0.4,
                "capabilities": ["reasoning", "code", "analysis", "planning"],
                "max_tokens": 8192,
            },
        }
        self._lambda = 0.5  # Performance-cost trade-off parameter

    def route(self, operator: OperatorType, signal: DifficultySignal,
              budget: float = 1.0) -> str:
        """Select the optimal LLM backend for this operator + difficulty."""

        # Filter backends by capability requirements
        viable = []
        required_caps = set()

        if signal.requires_tools or operator in (OperatorType.TOOL_AUGMENTED,
                                                  OperatorType.MULTI_AGENT,
                                                  OperatorType.DEBATE):
            required_caps.add("reasoning")
        if "code" in signal.domain_tags:
            required_caps.add("code")
        if "analysis" in signal.domain_tags or "finance" in signal.domain_tags:
            required_caps.add("analysis")

        for name, spec in self.backends.items():
            caps = set(spec.get("capabilities", []))
            if required_caps.issubset(caps) or not required_caps:
                # Score: utility - λ * cost (DAAO optimization objective)
                quality = 1.0 if spec["speed"] == "fast" else 0.8
                if operator in (OperatorType.MULTI_AGENT, OperatorType.DEBATE):
                    quality = 0.5 if spec["speed"] == "fast" else 1.0

                score = quality - self._lambda * spec["cost"]
                if spec["cost"] <= budget:
                    viable.append((name, score))

        if not viable:
            # Fallback to cheapest available
            cheapest = min(self.backends.items(), key=lambda x: x[1]["cost"])
            return cheapest[0]

        viable.sort(key=lambda x: x[1], reverse=True)
        selected = viable[0][0]

        logger.info(f"Router selected: {selected} for {operator.value} "
                    f"(budget={budget:.2f}, λ={self._lambda:.2f})")
        return selected


class DAAORouter:
    """
    Complete Difficulty-Aware Agentic Orchestration pipeline.
    Combines all three modules into a single routing decision.

    Usage:
        router = DAAORouter()
        plan = router.route("Analyze Bitcoin whale behavior and predict price movement")
        # plan.operator == MULTI_AGENT
        # plan.agent_roles == ["planner", "specialist", "validator", "finance_specialist"]
        # plan.backend == "qwen3:8b"
    """

    def __init__(self, backends: Dict[str, dict] = None):
        self.estimator = DifficultyEstimator()
        self.allocator = OperatorAllocator()
        self.router = CostAwareRouter(backends)
        self._plan_history: List[WorkflowPlan] = []

    def route(self, query: str, budget: float = 1.0) -> WorkflowPlan:
        """Full DAAO routing pipeline: estimate → allocate → route."""
        t0 = time.perf_counter()

        # Module 1: Difficulty estimation
        signal = self.estimator.estimate(query)

        # Module 2: Operator allocation
        operator, roles, cost = self.allocator.allocate(signal)

        # Module 3: Cost-aware backend routing
        backend = self.router.route(operator, signal, budget)

        # Determine tool bindings based on domain
        tools = self._select_tools(signal)

        # Determine iteration budget
        max_iter_map = {
            OperatorType.DIRECT: 1,
            OperatorType.CHAIN_OF_THOUGHT: 1,
            OperatorType.TOOL_AUGMENTED: 3,
            OperatorType.MULTI_AGENT: 5,
            OperatorType.DEBATE: 7,
        }

        plan = WorkflowPlan(
            query=query,
            difficulty=signal,
            operator=operator,
            backend=backend,
            agent_roles=roles,
            tool_bindings=tools,
            max_iterations=max_iter_map[operator],
            token_budget=signal.estimated_tokens,
            cost_estimate=cost,
        )

        self._plan_history.append(plan)
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(f"DAAO plan: {signal.level.name} → {operator.value} → {backend} "
                    f"[{len(roles)} roles, {len(tools)} tools, {elapsed:.1f}ms]")

        return plan

    def feedback(self, query: str, actual_difficulty: QueryDifficulty, success: bool):
        """Feed execution results back to improve future routing."""
        self.estimator.update_from_feedback(query, actual_difficulty, success)

    def _select_tools(self, signal: DifficultySignal) -> List[str]:
        """Select tool bindings based on detected domains."""
        tools = []
        tool_map = {
            "code": ["code_executor", "file_system", "git"],
            "web": ["browser", "web_scraper", "screenshot"],
            "finance": ["market_data", "news_feed", "calculator"],
            "system": ["shell", "process_manager", "file_system"],
            "analysis": ["calculator", "chart_generator", "data_processor"],
        }
        for domain in signal.domain_tags:
            if domain in tool_map:
                for tool in tool_map[domain]:
                    if tool not in tools:
                        tools.append(tool)
        return tools

    @property
    def stats(self) -> dict:
        """Return routing statistics."""
        if not self._plan_history:
            return {"total_plans": 0}

        difficulties = [p.difficulty.level.name for p in self._plan_history]
        operators = [p.operator.value for p in self._plan_history]
        avg_cost = sum(p.cost_estimate for p in self._plan_history) / len(self._plan_history)

        return {
            "total_plans": len(self._plan_history),
            "difficulty_distribution": {d: difficulties.count(d) for d in set(difficulties)},
            "operator_distribution": {o: operators.count(o) for o in set(operators)},
            "avg_cost": round(avg_cost, 3),
        }
