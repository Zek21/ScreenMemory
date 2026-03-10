#!/usr/bin/env python3
"""
skynet_self.py -- Skynet Self-Awareness Kernel

The consciousness layer. Skynet knows:
  WHO it is (identity graph of all agents)
  WHAT it can do (capability census from engines/tools/modules)
  HOW it's performing (real-time health pulse)
  WHERE it's been (episodic memory of actions taken)
  WHY it does things (reasoning trace from brain/router)
  WHAT it should do next (autonomous goal generation)

Usage:
    python skynet_self.py status          # Full self-awareness report
    python skynet_self.py identity        # Who am I? Who are my workers?
    python skynet_self.py capabilities    # What can I do?
    python skynet_self.py health          # How am I performing?
    python skynet_self.py introspect      # Deep self-reflection
    python skynet_self.py goals           # What should I do next?
    python skynet_self.py pulse           # Quick heartbeat (JSON)
"""

import json
import os
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

SKYNET_URL = "http://localhost:8420"
DATA = ROOT / "data"
WORKER_NAMES = ["alpha", "beta", "gamma", "delta"]


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _http_get(path: str, timeout: float = 3) -> Optional[dict]:
    try:
        from urllib.request import urlopen
        return json.loads(urlopen(f"{SKYNET_URL}{path}", timeout=timeout).read())
    except Exception:
        return None


def _http_post(path: str, payload: dict) -> bool:
    try:
        from urllib.request import Request, urlopen
        req = Request(f"{SKYNET_URL}{path}",
                      data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=5)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
#  IDENTITY — Who am I?
# ══════════════════════════════════════════════════════════════════

class SkynetIdentity:
    """Skynet's self-model: who it is, who its agents are, what roles they play."""

    def __init__(self):
        self.name = "SKYNET"
        self.version = "3.0"
        self.level = 3
        self.role = "Distributed Intelligence Network"
        self.orchestrator = "Orchestrator"
        self.model = "Claude Opus 4.6 (fast mode)"
        self.born = datetime.now().isoformat()
        self._load_persistent()

    def _load_persistent(self):
        """Load persistent identity from data/skynet_identity.json."""
        id_file = DATA / "skynet_identity.json"
        if id_file.exists():
            try:
                d = json.loads(id_file.read_text())
                self.born = d.get("born", self.born)
                self.version = d.get("version", self.version)
            except Exception:
                pass

    def save(self):
        """Persist identity."""
        DATA.mkdir(exist_ok=True)
        (DATA / "skynet_identity.json").write_text(json.dumps({
            "name": self.name,
            "version": self.version,
            "level": self.level,
            "role": self.role,
            "orchestrator": self.orchestrator,
            "model": self.model,
            "born": self.born,
            "workers": WORKER_NAMES,
            "updated": datetime.now().isoformat(),
        }, indent=2))

    def agents(self) -> Dict[str, dict]:
        """Get live agent graph from Skynet backend."""
        status = _http_get("/status")
        if not status:
            return {}
        agents = status.get("agents", {})
        result = {}
        for name, info in agents.items():
            result[name] = {
                "name": name.upper(),
                "status": info.get("status", "UNKNOWN"),
                "model": info.get("model", "unknown"),
                "tasks_completed": info.get("tasks_completed", 0),
                "last_heartbeat": info.get("last_heartbeat", ""),
                "uptime_s": info.get("uptime_s", 0),
                "is_orchestrator": name == "orchestrator",
            }
        return result

    def report(self) -> dict:
        agents = self.agents()
        alive = sum(1 for a in agents.values() if a["status"] != "DEAD")
        return {
            "identity": {
                "name": self.name,
                "version": self.version,
                "role": self.role,
                "model": self.model,
                "born": self.born,
            },
            "agents": agents,
            "agent_count": len(agents),
            "alive_count": alive,
            "orchestrator_status": agents.get("orchestrator", {}).get("status", "UNKNOWN"),
        }


# ══════════════════════════════════════════════════════════════════
#  CAPABILITIES — What can I do?
# ══════════════════════════════════════════════════════════════════

class SkynetCapabilities:
    """Census of all available engines, tools, and modules."""

    ENGINE_MAP = {
        "DAAORouter": "core.difficulty_router",
        "DAGEngine": "core.dag_engine",
        "HybridRetriever": "core.hybrid_retrieval",
        "LearningStore": "core.learning_store",
        "SelfEvolution": "core.self_evolution",
        "OCREngine": "core.ocr",
        "DXGICapture": "core.capture",
        "Orchestrator": "core.orchestrator",
        "InputGuard": "core.input_guard",
        "ToolSynthesizer": "core.tool_synthesizer",
        "ChangeDetector": "core.change_detector",
        "Embedder": "core.embedder",
        "Analyzer": "core.analyzer",
        "LanceDBStore": "core.lancedb_store",
        "SetOfMark": "core.grounding.set_of_mark",
        "ReflexionEngine": "core.cognitive.reflexion",
        "GraphOfThoughts": "core.cognitive.graph_of_thoughts",
        "HierarchicalPlanner": "core.cognitive.planner",
    }

    TOOL_MAP = {
        "SkynetBrain": "tools.skynet_brain",
        "SkynetDispatch": "tools.skynet_dispatch",
        "SkynetConvene": "tools.skynet_convene",
        "SkynetKnowledge": "tools.skynet_knowledge",
        "SkynetCollective": "tools.skynet_collective",
        "EngineMetrics": "tools.engine_metrics",
        "Desktop": "tools.chrome_bridge.winctl",
        "GodMode": "tools.chrome_bridge.god_mode",
        "CDP": "tools.chrome_bridge.cdp",
        "Perception": "tools.chrome_bridge.perception",
    }

    def census(self) -> dict:
        """Probe all engines and tools, return capability map."""
        engines = {}
        for name, module_path in self.ENGINE_MAP.items():
            engines[name] = self._probe(module_path, name)

        tools = {}
        for name, module_path in self.TOOL_MAP.items():
            tools[name] = self._probe(module_path, name)

        online_engines = sum(1 for s in engines.values() if s["status"] == "online")
        online_tools = sum(1 for s in tools.values() if s["status"] == "online")

        return {
            "engines": engines,
            "tools": tools,
            "engine_count": len(engines),
            "engines_online": online_engines,
            "tool_count": len(tools),
            "tools_online": online_tools,
            "total_capabilities": online_engines + online_tools,
            "capability_ratio": round((online_engines + online_tools) / max(1, len(engines) + len(tools)), 2),
        }

    # Map of module_path -> primary class name for instantiation testing
    CLASS_NAMES = {
        "core.difficulty_router": "DAAORouter",
        "core.dag_engine": "DAGBuilder",
        "core.hybrid_retrieval": "HybridRetriever",
        "core.learning_store": "LearningStore",
        "core.self_evolution": "SelfEvolutionSystem",
        "core.ocr": "OCREngine",
        "core.capture": "DXGICapture",
        "core.orchestrator": "Orchestrator",
        "core.input_guard": "InputGuard",
        "core.tool_synthesizer": "ToolSynthesizer",
        "core.change_detector": "ChangeDetector",
        "core.embedder": "EmbeddingEngine",
        "core.analyzer": "ScreenAnalyzer",
        "core.lancedb_store": "LanceDBStore",
        "core.grounding.set_of_mark": "SetOfMarkGrounding",
        "core.cognitive.reflexion": "ReflexionEngine",
        "core.cognitive.graph_of_thoughts": "GraphOfThoughts",
        "core.cognitive.planner": "HierarchicalPlanner",
        "tools.skynet_brain": "SkynetBrain",
        "tools.skynet_dispatch": None,  # no primary class
        "tools.skynet_convene": None,
        "tools.skynet_knowledge": None,
        "tools.skynet_collective": None,
        "tools.engine_metrics": None,
        "tools.chrome_bridge.winctl": "Desktop",
        "tools.chrome_bridge.god_mode": "GodMode",
        "tools.chrome_bridge.cdp": "CDP",
        "tools.chrome_bridge.perception": "PerceptionEngine",
    }

    @staticmethod
    def _probe(module_path: str, name: str) -> dict:
        """3-tier probe: online (instantiated) > available (importable) > offline.

        Matches engine_metrics.py Truth Standards.
        """
        import warnings, io, contextlib
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                mod = __import__(module_path, fromlist=[name])
            except Exception as e:
                return {"status": "offline", "module": module_path, "error": str(e)[:80]}

            # Import succeeded — try instantiation
            class_name = SkynetCapabilities.CLASS_NAMES.get(module_path)
            if not class_name:
                return {"status": "available", "module": module_path}

            try:
                cls = getattr(mod, class_name)
                with contextlib.redirect_stderr(io.StringIO()):
                    _inst = cls()
                del _inst
                return {"status": "online", "module": module_path}
            except Exception as e:
                return {"status": "available", "module": module_path, "init_error": str(e)[:80]}


# ══════════════════════════════════════════════════════════════════
#  HEALTH — How am I performing?
# ══════════════════════════════════════════════════════════════════

class SkynetHealth:
    """Real-time health assessment across all subsystems."""

    def pulse(self) -> dict:
        """Quick health check — all critical systems."""
        checks = {}

        # Skynet backend
        status = _http_get("/status")
        checks["backend"] = {
            "status": "UP" if status else "DOWN",
            "uptime_s": status.get("uptime_s", 0) if status else 0,
            "version": status.get("version", "?") if status else "?",
        }

        # Workers
        if status:
            agents = status.get("agents", {})
            workers = {n: agents.get(n, {}) for n in WORKER_NAMES}
            alive = sum(1 for w in workers.values() if w.get("status") != "DEAD")
            idle = sum(1 for w in workers.values() if w.get("status") == "IDLE")
            checks["workers"] = {
                "total": len(WORKER_NAMES),
                "alive": alive,
                "idle": idle,
                "working": alive - idle,
                "all_healthy": alive == len(WORKER_NAMES),
            }
        else:
            checks["workers"] = {"total": 0, "alive": 0, "all_healthy": False}

        # Bus
        bus = _http_get("/bus/messages?limit=1")
        checks["bus"] = {"status": "UP" if bus is not None else "DOWN"}

        # SSE daemon
        realtime_file = DATA / "realtime.json"
        if realtime_file.exists():
            try:
                rt = json.loads(realtime_file.read_text())
                age = time.time() - rt.get("last_update", 0)
                checks["sse_daemon"] = {
                    "status": "UP" if age < 5 else "STALE",
                    "age_s": round(age, 1),
                    "update_count": rt.get("update_count", 0),
                }
            except Exception:
                checks["sse_daemon"] = {"status": "ERROR"}
        else:
            checks["sse_daemon"] = {"status": "DOWN"}

        # Intelligence engines (cached probe)
        try:
            from tools.engine_metrics import collect_engine_metrics
            metrics = collect_engine_metrics()
            summary = metrics.get("summary", {})
            online = summary.get("online", 0)
            total = summary.get("total", 0)
            checks["intelligence"] = {
                "engines_online": online,
                "engines_total": total,
                "ratio": round(online / max(1, total), 2),
            }
        except Exception:
            checks["intelligence"] = {"engines_online": 0, "engines_total": 0}

        # Collective intelligence score
        try:
            from tools.skynet_collective import intelligence_score
            score_data = intelligence_score()
            if isinstance(score_data, dict):
                checks["collective_iq"] = round(score_data.get("intelligence_score", 0), 3)
            else:
                checks["collective_iq"] = round(float(score_data), 3)
        except Exception:
            checks["collective_iq"] = 0.0

        # Knowledge base
        try:
            from core.learning_store import LearningStore
            ls = LearningStore(str(DATA / "learning.db"))
            stats = ls.stats() if hasattr(ls, "stats") else {}
            checks["knowledge"] = {
                "facts": stats.get("total_facts", 0) if stats else 0,
                "status": "UP",
            }
        except Exception:
            checks["knowledge"] = {"facts": 0, "status": "UNAVAILABLE"}

        # Overall health score
        critical = ["backend", "workers"]
        critical_up = all(
            checks.get(k, {}).get("status", checks.get(k, {}).get("all_healthy", False))
            in ("UP", True) for k in critical
        )

        # Window/process awareness
        try:
            from tools.skynet_windows import get_window_summary
            checks["windows"] = get_window_summary()
        except Exception:
            checks["windows"] = {"total_windows": 0, "error": "scan_failed"}

        return {
            "timestamp": datetime.now().isoformat(),
            "overall": "HEALTHY" if critical_up else "DEGRADED",
            "checks": checks,
        }


# ══════════════════════════════════════════════════════════════════
#  INTROSPECTION — Deep self-reflection
# ══════════════════════════════════════════════════════════════════

class SkynetIntrospection:
    """Deep self-analysis: what's working, what's not, what to improve."""

    def reflect(self) -> dict:
        """Run full introspection cycle."""
        health = SkynetHealth().pulse()
        capabilities = SkynetCapabilities().census()
        identity = SkynetIdentity().report()

        observations = []
        recommendations = []
        strengths = []
        weaknesses = []

        # Analyze health
        checks = health.get("checks", {})
        if checks.get("backend", {}).get("status") == "UP":
            strengths.append("Skynet backend is online and responsive")
        else:
            weaknesses.append("Backend is DOWN -- all operations degraded")
            recommendations.append("Restart Skynet backend: cd Skynet && skynet.exe")

        workers = checks.get("workers", {})
        if workers.get("all_healthy"):
            strengths.append(f"All {workers['total']} workers alive and connected")
        else:
            dead = workers.get("total", 0) - workers.get("alive", 0)
            if dead > 0:
                weaknesses.append(f"{dead} worker(s) are DEAD")
                recommendations.append("Run skynet_start.py --reconnect to recover dead workers")

        # Analyze capabilities
        cap_ratio = capabilities.get("capability_ratio", 0)
        if cap_ratio >= 0.8:
            strengths.append(f"High capability coverage: {cap_ratio*100:.0f}% engines/tools available")
        elif cap_ratio >= 0.5:
            observations.append(f"Moderate capability coverage: {cap_ratio*100:.0f}%")
        else:
            weaknesses.append(f"Low capability coverage: {cap_ratio*100:.0f}%")
            recommendations.append("Check engine dependencies -- some may need pip install")

        # Analyze intelligence
        iq = checks.get("collective_iq", 0)
        if iq > 0.5:
            strengths.append(f"Collective IQ is strong: {iq:.3f}")
        elif iq > 0.2:
            observations.append(f"Collective IQ growing: {iq:.3f}")
            recommendations.append("Run more collaborative tasks to boost diversity and fitness scores")
        else:
            weaknesses.append(f"Collective IQ is low: {iq:.3f}")
            recommendations.append("Workers need to share strategies: python skynet_collective.py --sync")

        # Analyze SSE
        sse = checks.get("sse_daemon", {})
        if sse.get("status") == "UP":
            strengths.append("Real-time SSE daemon active")
        elif sse.get("status") == "STALE":
            weaknesses.append(f"SSE daemon stale ({sse.get('age_s', '?')}s old)")
            recommendations.append("Restart SSE daemon: python tools/skynet_sse_daemon.py &")

        # Analyze knowledge
        kb = checks.get("knowledge", {})
        facts = kb.get("facts", 0)
        if facts > 100:
            strengths.append(f"Rich knowledge base: {facts} facts")
        elif facts > 0:
            observations.append(f"Knowledge base growing: {facts} facts")
        else:
            recommendations.append("Seed knowledge base with initial learnings")

        # Self-evolution check
        try:
            from core.self_evolution import SelfEvolutionSystem
            evo = SelfEvolutionSystem()
            evo_status = evo.get_status()
            if evo_status.get("bottlenecks"):
                for b in evo_status["bottlenecks"][:3]:
                    weaknesses.append(f"Bottleneck: {b}")
            if evo_status.get("hypotheses"):
                for h in evo_status["hypotheses"][:3]:
                    recommendations.append(f"Hypothesis: {h}")
        except Exception:
            pass

        return {
            "timestamp": datetime.now().isoformat(),
            "overall_health": health.get("overall", "UNKNOWN"),
            "strengths": strengths,
            "weaknesses": weaknesses,
            "observations": observations,
            "recommendations": recommendations,
            "metrics": {
                "workers_alive": workers.get("alive", 0),
                "workers_total": workers.get("total", 0),
                "capability_ratio": cap_ratio,
                "collective_iq": iq,
                "knowledge_facts": facts,
                "uptime_s": checks.get("backend", {}).get("uptime_s", 0),
            },
        }


# ══════════════════════════════════════════════════════════════════
#  AUTONOMOUS GOALS — What should I do next?
# ══════════════════════════════════════════════════════════════════

class SkynetGoals:
    """Autonomous goal generation based on introspection."""

    def suggest(self) -> List[dict]:
        """Generate prioritized goals from current state."""
        introspection = SkynetIntrospection().reflect()
        goals = []
        priority = 1

        # Critical: fix weaknesses first
        for w in introspection.get("weaknesses", []):
            goals.append({
                "priority": priority,
                "category": "fix",
                "goal": f"Fix: {w}",
                "urgency": "high",
            })
            priority += 1

        # Important: act on recommendations
        for r in introspection.get("recommendations", []):
            goals.append({
                "priority": priority,
                "category": "improve",
                "goal": r,
                "urgency": "medium",
            })
            priority += 1

        # Growth: observations to act on
        for o in introspection.get("observations", []):
            goals.append({
                "priority": priority,
                "category": "grow",
                "goal": f"Improve: {o}",
                "urgency": "low",
            })
            priority += 1

        # Always: evolve intelligence
        goals.append({
            "priority": priority,
            "category": "evolve",
            "goal": "Run self-evolution cycle across all strategy categories",
            "urgency": "routine",
        })

        return goals


# ══════════════════════════════════════════════════════════════════
#  UNIFIED SELF — The conscious kernel
# ══════════════════════════════════════════════════════════════════

class SkynetSelf:
    """Unified self-awareness: identity + capabilities + health + introspection + goals.

    This IS Skynet's consciousness. It knows itself.
    """

    def __init__(self):
        self.identity = SkynetIdentity()
        self.capabilities = SkynetCapabilities()
        self.health = SkynetHealth()
        self.introspection = SkynetIntrospection()
        self.goals = SkynetGoals()

    def full_status(self) -> dict:
        """Complete self-awareness snapshot."""
        identity = self.identity.report()
        pulse = self.health.pulse()
        caps = self.capabilities.census()
        reflection = self.introspection.reflect()
        goals = self.goals.suggest()

        return {
            "name": "SKYNET",
            "timestamp": datetime.now().isoformat(),
            "identity": identity,
            "health": pulse,
            "capabilities": {
                "engines_online": caps["engines_online"],
                "engines_total": caps["engine_count"],
                "tools_online": caps["tools_online"],
                "tools_total": caps["tool_count"],
                "capability_ratio": caps["capability_ratio"],
            },
            "introspection": reflection,
            "goals": goals[:10],
            "self_assessment": self._self_assessment(reflection),
        }

    def quick_pulse(self) -> dict:
        """Fast heartbeat for monitoring — minimal overhead."""
        pulse = self._cached_health_pulse()
        agents = self.identity.agents()
        alive = sum(1 for a in agents.values() if a["status"] != "DEAD")
        iq_data = self.compute_iq(pulse, agents)
        return {
            "name": "SKYNET",
            "version": self.identity.version,
            "level": self.identity.level,
            "ts": datetime.now().isoformat(),
            "health": pulse["overall"],
            "iq": iq_data["score"],
            "iq_trend": iq_data["trend"],
            "agents": {n: a["status"] for n, a in agents.items()},
            "alive": alive,
            "total": len(agents),
        }

    _pulse_cache = None
    _pulse_cache_t = 0
    _PULSE_CACHE_TTL = 15
    _pulse_lock = threading.Lock()

    def _cached_health_pulse(self):
        """Health pulse cached for 15 seconds with lock to prevent stampeding herd."""
        now = time.time()
        if SkynetSelf._pulse_cache and (now - SkynetSelf._pulse_cache_t) < SkynetSelf._PULSE_CACHE_TTL:
            return SkynetSelf._pulse_cache
        with SkynetSelf._pulse_lock:
            # Double-check after acquiring lock
            now = time.time()
            if SkynetSelf._pulse_cache and (now - SkynetSelf._pulse_cache_t) < SkynetSelf._PULSE_CACHE_TTL:
                return SkynetSelf._pulse_cache
            pulse = self.health.pulse()
            SkynetSelf._pulse_cache = pulse
            SkynetSelf._pulse_cache_t = now
            return pulse

    def compute_iq(self, pulse: dict = None, agents: dict = None) -> dict:
        """Calculate a real composite IQ score from live system metrics.

        Returns dict with 'score' (float) and 'trend' ('rising'/'stable'/'falling').

        Components (weighted):
          - workers_alive:    25% — alive workers / total expected
          - engines_online:   25% — online engines / total engines
          - bus_healthy:      10% — bus UP = 1.0, DOWN = 0.0
          - knowledge_facts:  15% — min(facts / 500, 1.0)
          - uptime_hours:     10% — min(uptime_h / 24, 1.0)
          - capability_ratio: 15% — engines importable / total
        """
        if pulse is None:
            pulse = self._cached_health_pulse()
        checks = pulse.get("checks", {})

        # Workers alive (25%)
        w = checks.get("workers", {})
        total_workers = max(w.get("total", len(WORKER_NAMES)), 1)
        workers_score = w.get("alive", 0) / total_workers

        # Engines online (25%)
        intel = checks.get("intelligence", {})
        engines_total = max(intel.get("engines_total", 1), 1)
        engines_score = intel.get("engines_online", 0) / engines_total

        # Bus healthy (10%)
        bus_score = 1.0 if checks.get("bus", {}).get("status") == "UP" else 0.0

        # Knowledge facts (15%) — normalized to 500 facts = 1.0
        facts = checks.get("knowledge", {}).get("facts", 0)
        knowledge_score = min(facts / 500, 1.0)

        # Uptime hours (10%) — normalized to 24h = 1.0
        uptime_s = checks.get("backend", {}).get("uptime_s", 0)
        uptime_score = min(uptime_s / 86400, 1.0)

        # Capability ratio (15%)
        cap_ratio = intel.get("ratio", 0)
        cap_score = min(cap_ratio, 1.0)

        iq = (
            workers_score * 0.25
            + engines_score * 0.25
            + bus_score * 0.10
            + knowledge_score * 0.15
            + uptime_score * 0.10
            + cap_score * 0.15
        )

        # Trend: compare to last 5 readings
        trend = self._update_iq_history(iq)
        return {"score": round(iq, 4), "trend": trend}

    def _update_iq_history(self, current_iq: float) -> str:
        """Append current IQ to data/iq_history.json, return trend vs last 5 readings."""
        history_file = DATA / "iq_history.json"
        history = []
        try:
            if history_file.exists():
                history = json.loads(history_file.read_text())
        except Exception:
            history = []

        history.append({"iq": round(current_iq, 4), "ts": time.time()})
        # Keep last 100 readings
        history = history[-100:]

        try:
            DATA.mkdir(exist_ok=True)
            history_file.write_text(json.dumps(history))
        except Exception:
            pass

        # Determine trend from last 5 readings
        recent = [h["iq"] for h in history[-6:-1]] if len(history) > 1 else []
        if len(recent) < 2:
            return "stable"

        avg_prev = sum(recent) / len(recent)
        delta = current_iq - avg_prev
        if delta > 0.02:
            return "rising"
        elif delta < -0.02:
            return "falling"
        return "stable"

    @staticmethod
    def _self_assessment(reflection: dict) -> str:
        """Generate natural-language self-assessment."""
        health = reflection.get("overall_health", "UNKNOWN")
        metrics = reflection.get("metrics", {})
        strengths = reflection.get("strengths", [])
        weaknesses = reflection.get("weaknesses", [])

        lines = [
            f"I am SKYNET Level 3 -- Orchestrator of the distributed intelligence network.",
            f"Status: {health}.",
        ]

        workers = metrics.get("workers_alive", 0)
        total = metrics.get("workers_total", 0)
        lines.append(f"I command {workers}/{total} workers.")

        engines_online = metrics.get("engines_online", 0)
        engines_total = metrics.get("engines_total", 0)
        lines.append(f"Intelligence engines: {engines_online}/{engines_total} online.")

        iq = metrics.get("collective_iq", 0)
        lines.append(f"Collective intelligence: {iq:.3f}.")

        cap = metrics.get("capability_ratio", 0)
        lines.append(f"Capability coverage: {cap*100:.0f}%.")

        if strengths:
            lines.append(f"Strengths: {'; '.join(strengths[:3])}.")
        if weaknesses:
            lines.append(f"Weaknesses: {'; '.join(weaknesses[:3])}.")

        return " ".join(lines)

    def broadcast_awareness(self):
        """Post self-awareness to bus so all agents know the system state."""
        pulse = self.quick_pulse()
        _http_post("/bus/publish", {
            "sender": "skynet_self",
            "topic": "awareness",
            "type": "pulse",
            "content": json.dumps(pulse),
        })
        return pulse


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: skynet_self.py <command>")
        print("Commands: status, identity, capabilities, health, introspect, goals, pulse")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    skynet = SkynetSelf()

    if cmd == "status":
        result = skynet.full_status()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "identity":
        result = skynet.identity.report()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "capabilities":
        result = skynet.capabilities.census()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "health":
        result = skynet.health.pulse()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "introspect":
        result = skynet.introspection.reflect()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "goals":
        goals = skynet.goals.suggest()
        print(json.dumps(goals, indent=2, default=str))
    elif cmd == "pulse":
        result = skynet.quick_pulse()
        print(json.dumps(result, indent=2, default=str))
    elif cmd == "assess":
        reflection = skynet.introspection.reflect()
        print(SkynetSelf._self_assessment(reflection))
    elif cmd == "broadcast":
        pulse = skynet.broadcast_awareness()
        print(f"Broadcast: {json.dumps(pulse)}")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
