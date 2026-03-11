"""
Agent Factory — Dynamic instantiation of specialized agents at runtime.

Implements the Factory Method pattern for multi-agent systems.
Instead of hardcoding agent configurations, the factory receives a WorkflowPlan
from the DAAO router and dynamically instantiates the exact agent team needed.

Design principles:
- Separation of creation from use (Factory Method)
- Agent roles are configured via registry, not hardcoded
- Each agent gets isolated episodic memory + shared semantic access
- Agents are lightweight — created per-task, destroyed after
- The factory tracks agent lifecycle for the ProcessGuardian
"""
import time
import logging
from typing import Dict, List, Any, Optional, Type
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class AgentCapability(Enum):
    """Capabilities an agent can possess."""
    REASONING = "reasoning"
    PLANNING = "planning"
    CODE_EXECUTION = "code_execution"
    WEB_NAVIGATION = "web_navigation"
    DATA_ANALYSIS = "data_analysis"
    VISION = "vision"
    TOOL_USE = "tool_use"
    CRITIQUE = "critique"
    SYNTHESIS = "synthesis"


@dataclass
class AgentSpec:
    """Specification for a single agent type in the registry."""
    role: str
    system_prompt: str
    capabilities: List[AgentCapability]
    preferred_backend: str = "qwen3:8b"
    max_tokens: int = 2048
    temperature: float = 0.7
    tool_bindings: List[str] = field(default_factory=list)
    can_write: bool = False  # Whether agent can modify state


@dataclass
class AgentInstance:
    """A live, instantiated agent ready for execution."""
    id: str
    spec: AgentSpec
    memory_namespace: str
    created_at: float = field(default_factory=time.time)
    execution_count: int = 0
    total_tokens_used: int = 0
    last_output: str = ""
    status: str = "idle"  # idle, running, completed, failed

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "role": self.spec.role,
            "namespace": self.memory_namespace,
            "status": self.status,
            "executions": self.execution_count,
            "tokens_used": self.total_tokens_used,
            "age_s": round(self.age_seconds, 1),
        }


class AgentRegistry:
    """
    Registry of all available agent types and their specifications.
    New agent types are registered here — the factory uses this to instantiate them.
    """

    def __init__(self):
        self._specs: Dict[str, AgentSpec] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register the built-in agent types."""
        for spec in self._default_core_specs():
            self.register(spec)
        for spec in self._default_domain_specs():
            self.register(spec)

    @staticmethod
    def _default_core_specs() -> list:
        """Return the built-in core agent specs."""
        defs = [
            ("reasoner",
             "You are a deep reasoning agent. Analyze problems step-by-step using "
             "Graph of Thoughts methodology. Consider multiple perspectives before "
             "reaching conclusions. Identify assumptions and potential failure modes.",
             [AgentCapability.REASONING, AgentCapability.SYNTHESIS], 0.3, False),
            ("planner",
             "You are a hierarchical task planner. Decompose complex goals into "
             "ordered subtasks with clear dependencies. Each subtask must be atomic "
             "and independently verifiable. Output structured plans.",
             [AgentCapability.PLANNING, AgentCapability.REASONING], 0.2, False),
            ("specialist",
             "You are a domain specialist agent. Apply deep expertise to solve "
             "specific technical problems. Use precise terminology and cite "
             "relevant data. Focus on accuracy over breadth.",
             [AgentCapability.REASONING, AgentCapability.DATA_ANALYSIS], 0.5, False),
            ("validator",
             "You are a validation agent. Verify outputs against requirements. "
             "Check for: factual accuracy, logical consistency, completeness, "
             "format compliance. Flag specific issues with evidence.",
             [AgentCapability.CRITIQUE, AgentCapability.REASONING], 0.1, False),
            ("tool_executor",
             "You are a tool execution agent. Translate high-level requests into "
             "specific tool calls. Parse tool outputs and extract relevant data. "
             "Handle errors gracefully with retries and fallbacks.",
             [AgentCapability.TOOL_USE, AgentCapability.CODE_EXECUTION], 0.1, True),
            ("proposer",
             "You are a proposal agent in a debate protocol. Generate well-reasoned "
             "arguments with supporting evidence. Present clear, structured positions. "
             "Anticipate counterarguments.",
             [AgentCapability.REASONING, AgentCapability.SYNTHESIS], 0.7, False),
            ("critic",
             "You are a critic agent in a debate protocol. Challenge proposals with "
             "rigorous counterarguments. Identify logical fallacies, missing evidence, "
             "and unstated assumptions. Be adversarial but constructive.",
             [AgentCapability.CRITIQUE, AgentCapability.REASONING], 0.4, False),
            ("judge",
             "You are the judge agent in a debate protocol. Evaluate arguments from "
             "proposer and critic. Determine the strongest position based on evidence "
             "quality, logical coherence, and practical applicability. Render verdicts.",
             [AgentCapability.CRITIQUE, AgentCapability.SYNTHESIS], 0.2, False),
        ]
        return [
            AgentSpec(role=r, system_prompt=p, capabilities=c, temperature=t, can_write=w)
            for r, p, c, t, w in defs
        ]

    @staticmethod
    def _default_domain_specs() -> list:
        """Return domain specialist agent specs."""
        domains = [
            ("code_specialist", "software engineering, debugging, and code architecture"),
            ("finance_specialist", "financial markets, crypto, and economic analysis"),
            ("web_specialist", "web technologies, browser automation, and web scraping"),
            ("system_specialist", "system administration, DevOps, and infrastructure"),
            ("analysis_specialist", "data analysis, statistics, and research methodology"),
        ]
        return [
            AgentSpec(
                role=domain,
                system_prompt=f"You are a domain specialist in {prompt_suffix}. "
                              f"Apply deep technical knowledge to solve domain-specific problems.",
                capabilities=[AgentCapability.REASONING, AgentCapability.DATA_ANALYSIS],
                temperature=0.4,
            )
            for domain, prompt_suffix in domains
        ]

    def register(self, spec: AgentSpec):
        """Register a new agent type."""
        self._specs[spec.role] = spec
        logger.debug(f"Registered agent type: {spec.role}")

    def get(self, role: str) -> Optional[AgentSpec]:
        """Get spec for a role."""
        return self._specs.get(role)

    def list_roles(self) -> List[str]:
        """List all registered roles."""
        return list(self._specs.keys())

    def has_role(self, role: str) -> bool:
        return role in self._specs


class AgentFactory:
    """
    Factory for dynamically instantiating agent teams at runtime.

    Usage:
        factory = AgentFactory()
        agents = factory.create_team(workflow_plan)
        for agent in agents:
            result = factory.execute_agent(agent, context)
        factory.destroy_team(agents)
    """

    def __init__(self, registry: AgentRegistry = None):
        self.registry = registry or AgentRegistry()
        self._active_agents: Dict[str, AgentInstance] = {}
        self._agent_counter = 0
        self._total_created = 0
        self._total_destroyed = 0

    def _next_id(self) -> str:
        self._agent_counter += 1
        return f"agent_{self._agent_counter:04d}"

    def create_agent(self, role: str, memory_namespace: str = None,
                     backend_override: str = None) -> AgentInstance:
        """Create a single agent instance from role name."""
        spec = self.registry.get(role)
        if not spec:
            # Create a generic agent for unknown roles
            logger.warning(f"Unknown role '{role}', creating generic agent")
            spec = AgentSpec(
                role=role,
                system_prompt=f"You are a {role} agent. Execute tasks related to {role}.",
                capabilities=[AgentCapability.REASONING],
            )

        if backend_override:
            spec = AgentSpec(
                role=spec.role,
                system_prompt=spec.system_prompt,
                capabilities=spec.capabilities,
                preferred_backend=backend_override,
                max_tokens=spec.max_tokens,
                temperature=spec.temperature,
                tool_bindings=spec.tool_bindings,
                can_write=spec.can_write,
            )

        agent_id = self._next_id()
        namespace = memory_namespace or f"ns_{role}_{agent_id}"

        instance = AgentInstance(
            id=agent_id,
            spec=spec,
            memory_namespace=namespace,
        )

        self._active_agents[agent_id] = instance
        self._total_created += 1

        logger.info(f"Created agent: {agent_id} (role={role}, ns={namespace})")
        return instance

    def create_team(self, roles: List[str], backend: str = None,
                    shared_namespace: str = None) -> List[AgentInstance]:
        """
        Create a team of agents for a workflow.
        Each agent gets isolated episodic memory but shares semantic namespace.
        """
        team = []
        shared_ns = shared_namespace or f"team_{self._agent_counter + 1}"

        for role in roles:
            agent = self.create_agent(
                role=role,
                memory_namespace=f"{shared_ns}/{role}",
                backend_override=backend,
            )
            team.append(agent)

        logger.info(f"Created team of {len(team)} agents: {[a.spec.role for a in team]}")
        return team

    def destroy_agent(self, agent: AgentInstance):
        """Destroy an agent instance and free resources."""
        agent.status = "destroyed"
        if agent.id in self._active_agents:
            del self._active_agents[agent.id]
            self._total_destroyed += 1
            logger.debug(f"Destroyed agent: {agent.id}")

    def destroy_team(self, team: List[AgentInstance]):
        """Destroy all agents in a team."""
        for agent in team:
            self.destroy_agent(agent)

    def get_active_agents(self) -> List[AgentInstance]:
        """List all currently active agents."""
        return list(self._active_agents.values())

    @property
    def stats(self) -> dict:
        return {
            "active": len(self._active_agents),
            "total_created": self._total_created,
            "total_destroyed": self._total_destroyed,
            "active_roles": [a.spec.role for a in self._active_agents.values()],
        }
