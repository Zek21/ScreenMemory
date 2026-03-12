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
CONSULTANT_NAMES = ["consultant", "gemini_consultant"]  # signed: delta
ALL_AGENT_NAMES = WORKER_NAMES + CONSULTANT_NAMES + ["orchestrator"]  # signed: delta

# Consultant state files and bridge ports
CONSULTANT_STATE_FILES = {
    "consultant": DATA / "consultant_state.json",
    "gemini_consultant": DATA / "gemini_consultant_state.json",
}  # signed: delta
CONSULTANT_BRIDGE_PORTS = {
    "consultant": 8422,
    "gemini_consultant": 8425,
}  # signed: delta


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
            "consultants": CONSULTANT_NAMES,
            "all_agents": ALL_AGENT_NAMES,
            "updated": datetime.now().isoformat(),
        }, indent=2))
        # signed: delta

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

    def validate_agent_completeness(self) -> List[dict]:
        """Scan all identity data files and verify completeness.

        Checks:
          - data/workers.json: all workers present, HWNDs non-zero and alive, models correct
          - data/consultant_state.json / gemini_consultant_state.json: required fields, transport set
          - data/orchestrator.json: HWND non-zero and alive, model correct

        Returns a list of identity gaps (empty list = all complete).
        """
        gaps = []

        # --- Workers ---
        workers_file = DATA / "workers.json"
        if workers_file.exists():
            try:
                raw = json.loads(workers_file.read_text())
                # workers.json may be a dict with 'workers' key or a list
                if isinstance(raw, dict):
                    workers = raw.get("workers", [])
                elif isinstance(raw, list):
                    workers = raw
                else:
                    workers = []
                worker_names_found = set()
                for w in workers:
                    if isinstance(w, dict):
                        worker_names_found.add(w.get("name"))
                    elif isinstance(w, str):
                        worker_names_found.add(w)
                for expected in WORKER_NAMES:
                    if expected not in worker_names_found:
                        gaps.append({"entity": expected, "type": "worker", "gap": "missing_from_workers_json"})
                for w in workers:
                    name = w.get("name", "unknown")
                    hwnd = w.get("hwnd", 0)
                    if not hwnd:
                        gaps.append({"entity": name, "type": "worker", "gap": "hwnd_is_zero"})
                    elif hwnd:
                        try:
                            import ctypes
                            if not ctypes.windll.user32.IsWindow(int(hwnd)):
                                gaps.append({"entity": name, "type": "worker", "gap": "hwnd_dead",
                                             "hwnd": hwnd})
                        except Exception:
                            pass
                    model = w.get("model", "")
                    if model and "opus" not in model.lower() and "fast" not in model.lower():
                        gaps.append({"entity": name, "type": "worker", "gap": "wrong_model",
                                     "model": model})
            except Exception as e:
                gaps.append({"entity": "workers.json", "type": "file", "gap": f"parse_error: {e}"})
        else:
            gaps.append({"entity": "workers.json", "type": "file", "gap": "file_not_found"})

        # --- Consultants ---
        required_consultant_fields = ["id", "transport", "role"]
        for cname, state_file in CONSULTANT_STATE_FILES.items():
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    for field in required_consultant_fields:
                        if not state.get(field):
                            gaps.append({"entity": cname, "type": "consultant",
                                         "gap": f"missing_field:{field}"})
                    # Check HWND if present
                    hwnd = int(state.get("hwnd", 0))
                    if hwnd:
                        try:
                            import ctypes
                            if not ctypes.windll.user32.IsWindow(hwnd):
                                gaps.append({"entity": cname, "type": "consultant",
                                             "gap": "hwnd_dead", "hwnd": hwnd})
                        except Exception:
                            pass
                    # Check transport is set
                    transport = state.get("transport", "")
                    if not transport or transport == "unknown":
                        gaps.append({"entity": cname, "type": "consultant",
                                     "gap": "transport_not_set"})
                except Exception as e:
                    gaps.append({"entity": cname, "type": "consultant",
                                 "gap": f"parse_error: {e}"})
            else:
                gaps.append({"entity": cname, "type": "consultant",
                             "gap": "state_file_not_found"})

        # --- Orchestrator ---
        orch_file = DATA / "orchestrator.json"
        if orch_file.exists():
            try:
                orch = json.loads(orch_file.read_text())
                hwnd = int(orch.get("hwnd", 0))
                if not hwnd:
                    gaps.append({"entity": "orchestrator", "type": "orchestrator",
                                 "gap": "hwnd_is_zero"})
                elif hwnd:
                    try:
                        import ctypes
                        if not ctypes.windll.user32.IsWindow(hwnd):
                            gaps.append({"entity": "orchestrator", "type": "orchestrator",
                                         "gap": "hwnd_dead", "hwnd": hwnd})
                    except Exception:
                        pass
                model = orch.get("model", "")
                if model and "opus" not in model.lower() and "fast" not in model.lower():
                    gaps.append({"entity": "orchestrator", "type": "orchestrator",
                                 "gap": "wrong_model", "model": model})
            except Exception as e:
                gaps.append({"entity": "orchestrator.json", "type": "file",
                             "gap": f"parse_error: {e}"})
        else:
            gaps.append({"entity": "orchestrator.json", "type": "file",
                         "gap": "file_not_found"})

        return gaps
    # signed: delta

    def get_consultant_status(self) -> Dict[str, dict]:
        """Check consultant health: state files, HWND alive, bridge HTTP health.

        Returns dict keyed by consultant name with status details.
        """
        result = {}
        for name, state_file in CONSULTANT_STATE_FILES.items():
            port = CONSULTANT_BRIDGE_PORTS.get(name, 0)
            entry = {
                "name": name,
                "state_file_exists": state_file.exists(),
                "hwnd": 0,
                "hwnd_alive": False,
                "bridge_port": port,
                "bridge_alive": False,
                "transport": "unknown",
                "status": "UNKNOWN",
            }
            # Read state file for HWND and transport info
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    entry["hwnd"] = int(state.get("hwnd", 0))
                    entry["transport"] = state.get("transport",
                                                   state.get("prompt_transport", "unknown"))
                    entry["model"] = state.get("model", "unknown")
                    entry["sender_id"] = state.get("sender_id", name)
                except Exception:
                    pass
            # Check HWND alive via Win32 IsWindow
            if entry["hwnd"]:
                try:
                    import ctypes
                    entry["hwnd_alive"] = bool(
                        ctypes.windll.user32.IsWindow(entry["hwnd"])
                    )
                except Exception:
                    entry["hwnd_alive"] = False
            # Check bridge HTTP health
            if port:
                try:
                    from urllib.request import urlopen
                    resp = urlopen(f"http://localhost:{port}/health", timeout=2)
                    entry["bridge_alive"] = resp.getcode() == 200
                except Exception:
                    entry["bridge_alive"] = False
            # Determine overall status
            if entry["bridge_alive"] and entry["hwnd_alive"]:
                entry["status"] = "ONLINE"
            elif entry["bridge_alive"]:
                entry["status"] = "BRIDGE_ONLY"
            elif entry["hwnd_alive"]:
                entry["status"] = "WINDOW_ONLY"
            elif entry["state_file_exists"]:
                entry["status"] = "REGISTERED"
            else:
                entry["status"] = "ABSENT"
            result[name] = entry
        return result
        # signed: delta

    def report(self) -> dict:
        agents = self.agents()
        consultants = self.get_consultant_status()
        alive = sum(1 for a in agents.values() if a["status"] != "DEAD")
        consultants_online = sum(
            1 for c in consultants.values()
            if c["status"] in ("ONLINE", "BRIDGE_ONLY", "WINDOW_ONLY")
        )  # signed: delta
        return {
            "identity": {
                "name": self.name,
                "version": self.version,
                "role": self.role,
                "model": self.model,
                "born": self.born,
            },
            "agents": agents,
            "consultants": consultants,
            "agent_count": len(agents),
            "alive_count": alive,
            "consultant_count": len(consultants),
            "consultants_online": consultants_online,
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
        """Quick health check -- all critical systems."""
        checks = {}
        self._check_backend(checks)
        self._check_workers(checks)
        self._check_consultants(checks)  # signed: delta
        self._check_bus(checks)
        self._check_sse_daemon(checks)
        self._check_intelligence_engines(checks)
        self._check_collective_iq(checks)
        self._check_knowledge_base(checks)
        self._check_windows(checks)

        critical_up = all(
            checks.get(k, {}).get("status", checks.get(k, {}).get("all_healthy", False))
            in ("UP", True) for k in ["backend", "workers"]
        )
        return {"timestamp": datetime.now().isoformat(),
                "overall": "HEALTHY" if critical_up else "DEGRADED", "checks": checks}

    @staticmethod
    def _check_backend(checks):
        status = _http_get("/status")
        checks["backend"] = {
            "status": "UP" if status else "DOWN",
            "uptime_s": status.get("uptime_s", 0) if status else 0,
            "version": status.get("version", "?") if status else "?",
        }

    @staticmethod
    def _check_workers(checks):
        status = _http_get("/status")
        if status:
            agents = status.get("agents", {})
            workers = {n: agents.get(n, {}) for n in WORKER_NAMES}
            alive = sum(1 for w in workers.values() if w.get("status") != "DEAD")
            idle = sum(1 for w in workers.values() if w.get("status") == "IDLE")
            checks["workers"] = {"total": len(WORKER_NAMES), "alive": alive, "idle": idle,
                                 "working": alive - idle, "all_healthy": alive == len(WORKER_NAMES)}
        else:
            checks["workers"] = {"total": 0, "alive": 0, "all_healthy": False}

    @staticmethod
    def _check_consultants(checks):
        """Check consultant bridge health and HWND liveness."""
        consultant_results = {}
        total = len(CONSULTANT_NAMES)
        online = 0
        for name in CONSULTANT_NAMES:
            state_file = CONSULTANT_STATE_FILES.get(name)
            port = CONSULTANT_BRIDGE_PORTS.get(name, 0)
            c = {"bridge_alive": False, "hwnd_alive": False, "status": "ABSENT"}
            # Check bridge HTTP
            if port:
                try:
                    from urllib.request import urlopen
                    resp = urlopen(f"http://localhost:{port}/health", timeout=2)
                    c["bridge_alive"] = resp.getcode() == 200
                except Exception:
                    pass
            # Check HWND from state file
            if state_file and state_file.exists():
                try:
                    state = json.loads(state_file.read_text())
                    hwnd = int(state.get("hwnd", 0))
                    if hwnd:
                        import ctypes
                        c["hwnd_alive"] = bool(
                            ctypes.windll.user32.IsWindow(hwnd)
                        )
                except Exception:
                    pass
            if c["bridge_alive"] and c["hwnd_alive"]:
                c["status"] = "ONLINE"
                online += 1
            elif c["bridge_alive"]:
                c["status"] = "BRIDGE_ONLY"
                online += 1
            elif c["hwnd_alive"]:
                c["status"] = "WINDOW_ONLY"
            consultant_results[name] = c
        checks["consultants"] = {
            "total": total,
            "online": online,
            "details": consultant_results,
        }
        # signed: delta

    @staticmethod
    def _check_bus(checks):
        bus = _http_get("/bus/messages?limit=1")
        checks["bus"] = {"status": "UP" if bus is not None else "DOWN"}

    @staticmethod
    def _check_sse_daemon(checks):
        realtime_file = DATA / "realtime.json"
        if not realtime_file.exists():
            checks["sse_daemon"] = {"status": "DOWN"}
            return
        try:
            rt = json.loads(realtime_file.read_text())
            age = time.time() - rt.get("last_update", 0)
            checks["sse_daemon"] = {"status": "UP" if age < 5 else "STALE",
                                    "age_s": round(age, 1), "update_count": rt.get("update_count", 0)}
        except Exception:
            checks["sse_daemon"] = {"status": "ERROR"}

    @staticmethod
    def _check_intelligence_engines(checks):
        try:
            from tools.engine_metrics import collect_engine_metrics
            metrics = collect_engine_metrics()
            summary = metrics.get("summary", {})
            online = summary.get("online", 0)
            total = summary.get("total", 0)
            checks["intelligence"] = {"engines_online": online, "engines_total": total,
                                      "ratio": round(online / max(1, total), 2)}
        except Exception:
            checks["intelligence"] = {"engines_online": 0, "engines_total": 0}

    @staticmethod
    def _check_collective_iq(checks):
        try:
            from tools.skynet_collective import intelligence_score
            score_data = intelligence_score()
            val = score_data.get("intelligence_score", 0) if isinstance(score_data, dict) else float(score_data)
            checks["collective_iq"] = round(val, 3)
        except Exception:
            checks["collective_iq"] = 0.0

    @staticmethod
    def _check_knowledge_base(checks):
        try:
            from core.learning_store import LearningStore
            ls = LearningStore(str(DATA / "learning.db"))
            stats = ls.stats() if hasattr(ls, "stats") else {}
            checks["knowledge"] = {"facts": stats.get("total_facts", 0) if stats else 0, "status": "UP"}
        except Exception:
            checks["knowledge"] = {"facts": 0, "status": "UNAVAILABLE"}

    @staticmethod
    def _check_windows(checks):
        try:
            from tools.skynet_windows import get_window_summary
            checks["windows"] = get_window_summary()
        except Exception:
            checks["windows"] = {"total_windows": 0, "error": "scan_failed"}


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

        observations, recommendations, strengths, weaknesses = [], [], [], []
        checks = health.get("checks", {})

        self._reflect_on_backend(checks, strengths, weaknesses, recommendations)
        self._reflect_on_workers(checks, strengths, weaknesses, recommendations)
        self._reflect_on_consultants(checks, strengths, weaknesses, recommendations)  # signed: delta

        cap_ratio = capabilities.get("capability_ratio", 0)
        self._reflect_on_capabilities(cap_ratio, strengths, weaknesses, observations, recommendations)

        iq = checks.get("collective_iq", 0)
        self._reflect_on_iq(iq, strengths, weaknesses, observations, recommendations)
        self._reflect_on_sse(checks, strengths, weaknesses, recommendations)

        facts = checks.get("knowledge", {}).get("facts", 0)
        self._reflect_on_knowledge(facts, strengths, observations, recommendations)
        self._reflect_on_evolution(weaknesses, recommendations)

        # Detect recurring incident patterns  # signed: delta
        incident_patterns = self._detect_incident_patterns()
        for p in incident_patterns:
            if p["severity"] in ("CRITICAL", "HIGH"):
                weaknesses.append(f"Recurring incident pattern: {p['description']}")
                recommendations.append(p["recommendation"])
            else:
                observations.append(f"Incident pattern: {p['description']}")

        workers = checks.get("workers", {})
        consultants_info = checks.get("consultants", {})  # signed: delta
        return {
            "timestamp": datetime.now().isoformat(),
            "overall_health": health.get("overall", "UNKNOWN"),
            "strengths": strengths, "weaknesses": weaknesses,
            "observations": observations, "recommendations": recommendations,
            "incident_patterns": incident_patterns,  # signed: delta
            "metrics": {
                "workers_alive": workers.get("alive", 0),
                "workers_total": workers.get("total", 0),
                "consultants_online": consultants_info.get("online", 0),
                "consultants_total": consultants_info.get("total", 0),
                "capability_ratio": cap_ratio, "collective_iq": iq,
                "knowledge_facts": facts,
                "uptime_s": checks.get("backend", {}).get("uptime_s", 0),
            },
        }

    @staticmethod
    def _reflect_on_backend(checks, strengths, weaknesses, recommendations):
        if checks.get("backend", {}).get("status") == "UP":
            strengths.append("Skynet backend is online and responsive")
        else:
            weaknesses.append("Backend is DOWN -- all operations degraded")
            recommendations.append("Restart Skynet backend: cd Skynet && skynet.exe")

    @staticmethod
    def _reflect_on_workers(checks, strengths, weaknesses, recommendations):
        workers = checks.get("workers", {})
        if workers.get("all_healthy"):
            strengths.append(f"All {workers['total']} workers alive and connected")
        else:
            dead = workers.get("total", 0) - workers.get("alive", 0)
            if dead > 0:
                weaknesses.append(f"{dead} worker(s) are DEAD")
                recommendations.append("Run skynet_start.py --reconnect to recover dead workers")

    @staticmethod
    def _reflect_on_consultants(checks, strengths, weaknesses, recommendations):
        """Reflect on consultant bridge/HWND health."""
        consultants = checks.get("consultants", {})
        total = consultants.get("total", 0)
        online = consultants.get("online", 0)
        if total == 0:
            return
        details = consultants.get("details", {})
        if online == total:
            strengths.append(f"All {total} consultants online (bridge + HWND)")
        elif online > 0:
            offline_names = [n for n, d in details.items()
                            if d.get("status") not in ("ONLINE", "BRIDGE_ONLY")]
            weaknesses.append(
                f"{total - online}/{total} consultant(s) offline: {', '.join(offline_names)}"
            )
            recommendations.append("Run CC-Start.ps1 / GC-Start.ps1 to recover offline consultants")
        else:
            weaknesses.append(f"All {total} consultants are offline")
            recommendations.append("Start consultant bridges: CC-Start.ps1 and GC-Start.ps1")
        # Check for WINDOW_ONLY status (bridge dead but HWND alive)
        for n, d in details.items():
            if d.get("status") == "WINDOW_ONLY":
                weaknesses.append(f"Consultant {n} has live HWND but dead bridge -- bridge restart needed")
        # signed: delta

    @staticmethod
    def _reflect_on_capabilities(cap_ratio, strengths, weaknesses, observations, recommendations):
        if cap_ratio >= 0.8:
            strengths.append(f"High capability coverage: {cap_ratio*100:.0f}% engines/tools available")
        elif cap_ratio >= 0.5:
            observations.append(f"Moderate capability coverage: {cap_ratio*100:.0f}%")
        else:
            weaknesses.append(f"Low capability coverage: {cap_ratio*100:.0f}%")
            recommendations.append("Check engine dependencies -- some may need pip install")

    @staticmethod
    def _reflect_on_iq(iq, strengths, weaknesses, observations, recommendations):
        if iq > 0.5:
            strengths.append(f"Collective IQ is strong: {iq:.3f}")
        elif iq > 0.2:
            observations.append(f"Collective IQ growing: {iq:.3f}")
            recommendations.append("Run more collaborative tasks to boost diversity and fitness scores")
        else:
            weaknesses.append(f"Collective IQ is low: {iq:.3f}")
            recommendations.append("Workers need to share strategies: python skynet_collective.py --sync")

    @staticmethod
    def _reflect_on_sse(checks, strengths, weaknesses, recommendations):
        sse = checks.get("sse_daemon", {})
        if sse.get("status") == "UP":
            strengths.append("Real-time SSE daemon active")
        elif sse.get("status") == "STALE":
            weaknesses.append(f"SSE daemon stale ({sse.get('age_s', '?')}s old)")
            recommendations.append("Restart SSE daemon: python tools/skynet_sse_daemon.py &")

    @staticmethod
    def _reflect_on_knowledge(facts, strengths, observations, recommendations):
        if facts > 100:
            strengths.append(f"Rich knowledge base: {facts} facts")
        elif facts > 0:
            observations.append(f"Knowledge base growing: {facts} facts")
        else:
            recommendations.append("Seed knowledge base with initial learnings")

    @staticmethod
    def _reflect_on_evolution(weaknesses, recommendations):
        try:
            from core.self_evolution import SelfEvolutionSystem
            evo = SelfEvolutionSystem()
            evo_status = evo.get_status()
            for b in (evo_status.get("bottlenecks") or [])[:3]:
                weaknesses.append(f"Bottleneck: {b}")
            for h in (evo_status.get("hypotheses") or [])[:3]:
                recommendations.append(f"Hypothesis: {h}")
        except Exception:
            pass

    @staticmethod
    def _detect_incident_patterns() -> List[dict]:
        """Analyze data/incidents.json for recurring failure patterns.

        Looks for:
          - Repeated HWND failures
          - Repeated delivery failures
          - Repeated self-awareness gaps
          - Repeated process termination incidents

        Returns list of detected patterns with severity and recommendation.
        Posts warnings to bus if critical patterns are detected.
        """
        incidents_file = DATA / "incidents.json"
        if not incidents_file.exists():
            return []

        try:
            incidents = json.loads(incidents_file.read_text())
        except Exception:
            return []

        if not isinstance(incidents, list) or not incidents:
            return []

        patterns = []
        # Category counters
        hwnd_failures = 0
        delivery_failures = 0
        awareness_gaps = 0
        process_kills = 0
        boot_failures = 0

        hwnd_keywords = ["hwnd", "window", "iswindow", "dead window", "window handle"]
        delivery_keywords = ["delivery", "ghost_type", "dispatch", "clipboard", "postmessage",
                             "wm_paste", "ghost-type"]
        awareness_keywords = ["self-awareness", "identity", "consciousness", "blind",
                              "enumerat", "consultant_names", "worker_names"]
        process_keywords = ["stop-process", "taskkill", "kill", "terminate", "process"]
        boot_keywords = ["boot", "startup", "start", "skipworkers", "orch-start"]

        for inc in incidents:
            text = " ".join([
                str(inc.get("what_happened", "")),
                str(inc.get("root_cause", "")),
                str(inc.get("fix_applied", "")),
                str(inc.get("title", "")),
            ]).lower()

            if any(kw in text for kw in hwnd_keywords):
                hwnd_failures += 1
            if any(kw in text for kw in delivery_keywords):
                delivery_failures += 1
            if any(kw in text for kw in awareness_keywords):
                awareness_gaps += 1
            if any(kw in text for kw in process_keywords):
                process_kills += 1
            if any(kw in text for kw in boot_keywords):
                boot_failures += 1

        # Detect recurring patterns (threshold: 2+ incidents in same category)
        if hwnd_failures >= 2:
            patterns.append({
                "pattern": "recurring_hwnd_failures",
                "count": hwnd_failures,
                "severity": "HIGH",
                "description": f"HWND/window failures recurring ({hwnd_failures} incidents)",
                "recommendation": "Strengthen HWND monitoring in skynet_monitor.py; "
                                  "add heartbeat-based liveness instead of IsWindow-only checks",
            })

        if delivery_failures >= 2:
            patterns.append({
                "pattern": "recurring_delivery_failures",
                "count": delivery_failures,
                "severity": "HIGH",
                "description": f"Delivery mechanism failures recurring ({delivery_failures} incidents)",
                "recommendation": "Add delivery confirmation protocol; verify WM_PASTE receipt "
                                  "via UIA state change detection after ghost_type",
            })

        if awareness_gaps >= 2:
            patterns.append({
                "pattern": "recurring_self_awareness_gaps",
                "count": awareness_gaps,
                "severity": "CRITICAL",
                "description": f"Self-awareness/identity gaps recurring ({awareness_gaps} incidents)",
                "recommendation": "Run skynet_arch_verify.py on every boot; "
                                  "add ALL_AGENT_NAMES completeness assertion to Phase 0",
            })

        if process_kills >= 2:
            patterns.append({
                "pattern": "recurring_process_termination",
                "count": process_kills,
                "severity": "CRITICAL",
                "description": f"Unauthorized process termination recurring ({process_kills} incidents)",
                "recommendation": "Verify guard_process_kill() is enforced on all code paths; "
                                  "audit worker permissions",
            })

        if boot_failures >= 2:
            patterns.append({
                "pattern": "recurring_boot_failures",
                "count": boot_failures,
                "severity": "MEDIUM",
                "description": f"Boot/startup failures recurring ({boot_failures} incidents)",
                "recommendation": "Add boot sequence integrity checks; verify Orch-Start.ps1 "
                                  "defaults are immutable",
            })

        # Post warnings to bus for critical patterns
        critical_patterns = [p for p in patterns if p["severity"] in ("CRITICAL", "HIGH")]
        if critical_patterns:
            try:
                from tools.skynet_spam_guard import guarded_publish
                summary = "; ".join(
                    f"{p['pattern']}({p['count']})" for p in critical_patterns
                )
                guarded_publish({
                    "sender": "introspection",
                    "topic": "orchestrator",
                    "type": "alert",
                    "content": f"INCIDENT_PATTERN_WARNING: {summary}",
                })
            except Exception:
                pass

        return patterns
    # signed: delta

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
        """Fast heartbeat for monitoring — minimal overhead.

        Includes architecture/consultant/bus awareness flags (Level 3.4).
        """
        pulse = self._cached_health_pulse()
        agents = self.identity.agents()
        consultants = self.identity.get_consultant_status()  # signed: delta
        alive = sum(1 for a in agents.values() if a["status"] != "DEAD")
        consultants_online = sum(
            1 for c in consultants.values()
            if c["status"] in ("ONLINE", "BRIDGE_ONLY", "WINDOW_ONLY")
        )  # signed: delta
        iq_data = self.compute_iq(pulse, agents)

        # Awareness flags — does the agent truly understand the system?
        architecture_knowledge_ok = self._check_architecture_knowledge()
        consultant_awareness = self._check_consultant_awareness(consultants)
        bus_awareness = self._check_bus_awareness(pulse)

        return {
            "name": "SKYNET",
            "version": self.identity.version,
            "level": self.identity.level,
            "ts": datetime.now().isoformat(),
            "health": pulse["overall"],
            "iq": iq_data["score"],
            "iq_trend": iq_data["trend"],
            "agents": {n: a["status"] for n, a in agents.items()},
            "consultants": {n: c["status"] for n, c in consultants.items()},
            "alive": alive,
            "total": len(agents),
            "consultants_online": consultants_online,
            "consultants_total": len(consultants),
            "architecture_knowledge_ok": architecture_knowledge_ok,
            "consultant_awareness": consultant_awareness,
            "bus_awareness": bus_awareness,
        }
        # signed: delta

    @staticmethod
    def _check_architecture_knowledge() -> bool:
        """Check if agent knows ghost_type delivery and entity constants."""
        try:
            # Verify dispatch module has ghost_type_to_worker
            import importlib
            dispatch = importlib.import_module("tools.skynet_dispatch")
            if not hasattr(dispatch, "ghost_type_to_worker"):
                return False
            # Verify consciousness kernel constants are populated
            if not CONSULTANT_NAMES or not ALL_AGENT_NAMES:
                return False
            if len(ALL_AGENT_NAMES) < 7:
                return False
            return True
        except Exception:
            return False
    # signed: delta

    @staticmethod
    def _check_consultant_awareness(consultants: dict) -> bool:
        """Check if CONSULTANT_NAMES is populated and consultant states are readable."""
        if not CONSULTANT_NAMES or len(CONSULTANT_NAMES) < 2:
            return False
        if not consultants:
            return False
        # At least we can enumerate them (even if offline)
        return len(consultants) == len(CONSULTANT_NAMES)
    # signed: delta

    @staticmethod
    def _check_bus_awareness(pulse: dict) -> bool:
        """Check bus health and ring buffer status."""
        checks = pulse.get("checks", {})
        bus = checks.get("bus", {})
        return bus.get("status") == "UP"
    # signed: delta

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
        """Calculate a real composite IQ score from live system metrics."""
        if pulse is None:
            pulse = self._cached_health_pulse()
        checks = pulse.get("checks", {})

        scores = self._compute_iq_components(checks)
        iq = sum(score * weight for score, weight in scores)
        trend = self._update_iq_history(iq)
        return {"score": round(iq, 4), "trend": trend}

    @staticmethod
    def _compute_iq_components(checks):
        """Return list of (score, weight) tuples for IQ components."""
        w = checks.get("workers", {})
        total_workers = max(w.get("total", len(WORKER_NAMES)), 1)
        intel = checks.get("intelligence", {})
        engines_total = max(intel.get("engines_total", 1), 1)
        facts = checks.get("knowledge", {}).get("facts", 0)
        uptime_s = checks.get("backend", {}).get("uptime_s", 0)

        return [
            (w.get("alive", 0) / total_workers, 0.25),                             # workers alive
            (intel.get("engines_online", 0) / engines_total, 0.25),                 # engines online
            (1.0 if checks.get("bus", {}).get("status") == "UP" else 0.0, 0.10),    # bus healthy
            (min(facts / 500, 1.0), 0.15),                                          # knowledge facts
            (min(uptime_s / 86400, 1.0), 0.10),                                     # uptime hours
            (min(intel.get("ratio", 0), 1.0), 0.15),                                # capability ratio
        ]

    def _update_iq_history(self, current_iq: float) -> str:
        """Append current IQ to data/iq_history.json, return trend vs last 5 readings."""
        try:
            from tools.skynet_atomic import safe_read_json, atomic_write_json
        except ModuleNotFoundError:
            from skynet_atomic import safe_read_json, atomic_write_json

        history_file = DATA / "iq_history.json"
        history = safe_read_json(history_file, default=[])
        if not isinstance(history, list):
            history = []

        history.append({"iq": round(current_iq, 4), "ts": time.time()})
        history = history[-100:]

        atomic_write_json(history_file, history)

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

        consultants_online = metrics.get("consultants_online", 0)
        consultants_total = metrics.get("consultants_total", 0)
        if consultants_total > 0:
            lines.append(f"Consultants: {consultants_online}/{consultants_total} online.")
        # signed: delta

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
        msg = {
            "sender": "skynet_self",
            "topic": "awareness",
            "type": "pulse",
            "content": json.dumps(pulse),
        }
        try:
            from tools.skynet_spam_guard import guarded_publish
            guarded_publish(msg)
        except ImportError:
            _http_post("/bus/publish", msg)
        # signed: gamma
        return pulse


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage: skynet_self.py <command>")
        print("Commands: status, identity, capabilities, health, introspect, goals, pulse, validate, patterns")
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
    elif cmd == "validate":
        gaps = skynet.identity.validate_agent_completeness()
        if gaps:
            print(f"Identity Gaps Found ({len(gaps)}):")
            print(json.dumps(gaps, indent=2))
        else:
            print("Identity completeness: ALL PASS (no gaps)")
    elif cmd == "patterns":
        patterns = SkynetIntrospection._detect_incident_patterns()
        if patterns:
            print(f"Incident Patterns Detected ({len(patterns)}):")
            print(json.dumps(patterns, indent=2))
        else:
            print("No recurring incident patterns detected")
    # signed: delta
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
